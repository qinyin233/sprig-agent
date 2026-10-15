"""Terminal entry point; approvals default to no, including non-interactive stdin."""

import argparse
import json
import os
from pathlib import Path
import sys
import uuid

from . import __version__
from .agent import Agent, SYSTEM_PROMPT
from .changes import ChangeError, ChangeHistory
from .config import TOOL_NAMES, redact, terminal_text
from .diagnostics import diagnose
from .provider import Provider, ProviderError
from .session import SessionError, SessionStore, validate_history
from .settings import Settings, default_config_path, initialize_config, load_settings, public_config
from .tools import Tools


def approve(description):
    print(terminal_text(description), file=sys.stderr)
    if not sys.stdin.isatty():
        print("Non-interactive input: action rejected", file=sys.stderr)
        return False
    try:
        return input("Approve this exact action? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def check_argv(text):
    try:
        argv = json.loads(text)
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
            raise ValueError
        return tuple(argv)
    except (ValueError, RecursionError):
        raise argparse.ArgumentTypeError("--check expects a JSON argv array, e.g. '[\"python\",\"-m\",\"unittest\"]'") from None


def chat(agent, task, messages, emit, *, autostart=True):
    """Keep context across turns; only a new task or /continue calls the model."""
    status = 0
    history_ready = True

    def recover(cancelled, error=None):
        nonlocal messages, status, history_ready
        status = 130 if cancelled else 1
        label = "Task cancelled" if cancelled else "Error: " + redact(str(error), agent.config.secrets)
        if messages:
            try:
                messages = validate_history(messages)
            except SessionError as recovery_error:
                history_ready = False
                status = 1
                emit(label + "; history validation failed: " + redact(str(recovery_error), agent.config.secrets)
                     + ". Model requests are blocked; use local controls or /exit and start a new session.")
                return
            try:
                agent.checkpoint(messages)
            except (OSError, ValueError, ChangeError, SessionError) as save_error:
                status = 1
                emit("Conversation retained in memory, but saving failed: " + redact(str(save_error), agent.config.secrets))
        emit(label + "; conversation retained. Pending actions will NOT be replayed. Use /continue or enter a new request when ready.")

    def run(task):
        nonlocal messages, status
        if not history_ready:
            status = 1
            emit("History is not valid; no model request sent. Use local controls or /exit and start a new session.")
            return
        # Own the list before entering the agent so a budget/network/checkpoint
        # error does not throw away the turn and prevent local recovery commands.
        if not messages:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        try:
            messages, complete = agent.run(task, messages)
            status = 0 if complete else 2
        except KeyboardInterrupt:
            recover(True)
        except (OSError, ValueError, ChangeError, ProviderError, SessionError) as error:
            recover(False, error)

    if task or (messages is not None and autostart):
        run(task)
    emit("Chat ready. Enter a task; /continue, /help, /status, /context, /usage, /compact, /changes, /undo, /redo, /diff, /git or /exit.")
    while True:
        try:
            task = input("sprig> ").strip()
        except EOFError:
            return status
        except KeyboardInterrupt:
            emit("Input cancelled; conversation retained. Use /exit to close chat.")
            continue
        if not task:
            continue
        if task == "/exit":
            return status
        try:
            if task == "/help":
                emit("Enter a task to continue the same conversation. /continue explicitly continues retained history without adding a duplicate request; /status shows local context; /context previews the request budget; /usage shows local numeric request measurements and unknown token counts; /compact confirms request-only elision; /changes lists exact edit backups; /undo and /redo require diff approval; /discard-history confirms removal of backups; /diff and /git show Git changes with approval; /exit closes chat.")
            elif task == "/continue":
                if messages and len(messages) >= 2:
                    run(None)
                else:
                    emit("No task to continue. Enter a task first.")
            elif task == "/status":
                emit(f"Messages: {len(messages or [])} | model: {agent.config.model} | acceptance checks: {len(agent.config.checks)} | baseline: {agent.baseline_status} | last status: {status}")
            elif task == "/context":
                emit(json.dumps(agent.context_preview(messages), ensure_ascii=False, indent=2))
            elif task == "/usage":
                emit(json.dumps(agent.usage_report(), ensure_ascii=False, indent=2))
            elif task == "/compact":
                preview = agent.context_preview(messages)
                emit(json.dumps(preview, ensure_ascii=False, indent=2))
                if approve("Enable request-only elision of older successful tool output and completed approved edit arguments? User instructions, failures, refusals and the last six messages are preserved; saved history is unchanged."):
                    agent.compact_context = True
            elif task in {"/changes", "/undo", "/redo", "/discard-history"}:
                result = agent.tools.change_action(task[1:])
                if result.get("changed") or result.get("outcome") == "uncertain":
                    status = 2
                elif ("error" in result or result.get("approved") is False) and status == 0:
                    status = 2
                if history_ready:
                    agent.record_workspace_change(messages, result)
                emit(json.dumps(result, ensure_ascii=False, indent=2))
            elif task in {"/diff", "/git"}:
                argv = ["git", "diff", "--", "."] if task == "/diff" else ["git", "status", "--short", "--", "."]
                result = agent.tools.execute("run", {"argv": argv})
                if result.get("approved") and messages:
                    status = 2
                    if history_ready:
                        agent.record_workspace_change(messages, {"action": "command", "path": ".", "outcome": "uncertain"})
                elif ("error" in result or result.get("approved") is False or result.get("returncode", 0) != 0) and status == 0:
                    status = 2
                emit(json.dumps(redact(result, agent.config.secrets), ensure_ascii=False))
            elif task.startswith("/"):
                emit("Unknown chat command. Use /help.")
            else:
                run(task)
        except KeyboardInterrupt:
            if task in {"/diff", "/git"} and messages and history_ready:
                # An interrupt can occur after an approved command starts.
                # Retain only the uncertainty notice, never local command output.
                messages.append({"role": "user", "content": "A local command was interrupted and may have changed the workspace; prior acceptance is stale. Inspect current files before continuing."})
            recover(True)
        except (OSError, ValueError, ChangeError, ProviderError, SessionError) as error:
            recover(False, error)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sprig: a small approval-first coding agent")
    parser.add_argument("task", nargs="?", help="task; optional when resuming")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--file", action="append", default=[], metavar="RELATIVE_PATH", help="attach a complete workspace text snapshot before the task; repeatable, privacy and context limits apply")
    configuration = parser.add_mutually_exclusive_group()
    configuration.add_argument("--config", type=Path, help="explicit trusted TOML path (or SPRIG_CONFIG); default: ~/.sprig/config.toml")
    configuration.add_argument("--no-config", action="store_true", help="ignore configuration files, including SPRIG_CONFIG")
    parser.add_argument("--provider", help="select a named configured provider (or SPRIG_PROVIDER)")
    parser.add_argument("--profile", help="select a trusted named profile (or SPRIG_PROFILE)")
    parser.add_argument("--project-config", type=Path, help="explicit private project TOML; request/UI preferences and tighter policy only; never auto-discovered")
    parser.add_argument("--model", help="model override (or SPRIG_MODEL)")
    parser.add_argument("--base-url", help="API root override (or SPRIG_BASE_URL); configured credentials cannot follow a changed URL")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-output", type=int)
    parser.add_argument("--max-tokens", type=int, help="request a provider output-token limit per model call (1-65536); not a total billing cap")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--token-limit-field", choices=("max_tokens", "max_completion_tokens"))
    parser.add_argument("--request-timeout", type=float, help="HTTP timeout; separate from approved command timeout")
    parser.add_argument("--proxy", help="system, direct, or credential-free HTTP(S) proxy URL")
    parser.add_argument("--disable-tool", action="append", choices=TOOL_NAMES, help="disable a tool in schemas and execution; repeatable")
    parser.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=None, help="request chat_template_kwargs.enable_thinking; provider support required, off unless selected")
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None, help="display model text as it arrives")
    parser.add_argument("--request-usage", action=argparse.BooleanOptionalAction, default=None, help="request final stream token statistics; requires --stream or --tui and provider support")
    parser.add_argument("--metrics", dest="show_metrics", action=argparse.BooleanOptionalAction, default=None, help="print local numeric request measurements at CLI exit; missing token counts stay unknown")
    parser.add_argument("--read-only", action=argparse.BooleanOptionalAction, default=None, help="allow file inspection only; block writes and all commands")
    parser.add_argument("--inherit-env", action=argparse.BooleanOptionalAction, default=None, help="let approved commands inherit additional environment variables")
    parser.add_argument("--max-context-bytes", type=int, help="maximum serialized model request size; not a token/cost estimate")
    parser.add_argument("--compact-context", action=argparse.BooleanOptionalAction, default=None, help="explicitly elide old successful tool output and completed edit text from requests; keep saved history")
    parser.add_argument("--project-instructions", action=argparse.BooleanOptionalAction, default=None, help="load workspace AGENTS.md guidance; never reads parent/home directories")
    parser.add_argument("--instruction-file", dest="instruction_files", action="append", help="additional workspace-relative guidance file; repeatable, replaces configured files")
    parser.add_argument("--response-language", help="preferred language for explanations; direct task requests take precedence")
    interface = parser.add_mutually_exclusive_group()
    interface.add_argument("--chat", dest="interface", action="store_const", const="chat", help="keep an interactive conversation open")
    interface.add_argument("--tui", dest="interface", action="store_const", const="tui", help="open the full-screen terminal interface (streams by default)")
    interface.add_argument("--cli", dest="interface", action="store_const", const="cli", help="one-shot interface; override the configured interface")
    parser.add_argument("--check", dest="checks", action="append", type=check_argv, metavar="JSON_ARGV",
                        help="fixed acceptance command, repeatable; execution still requires approval")
    parser.add_argument("--check-before-task", action=argparse.BooleanOptionalAction, default=None,
                        help="propose fixed --check commands before every task/continue/resume; fresh approval required; final checks still run")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--save-session", metavar="NAME")
    group.add_argument("--resume", metavar="NAME")
    group.add_argument("--list-sessions", action="store_true", help="list local session metadata without contacting a model")
    group.add_argument("--delete-session", metavar="NAME", help="delete one local session after confirmation")
    group.add_argument("--init-config", action="store_true", help="confirm creation of a private TOML template; never overwrite an existing file")
    group.add_argument("--show-config", action="store_true", help="show effective settings with credential values withheld; no model request")
    group.add_argument("--doctor", action="store_true", help="check configuration, workspace privacy rules and dependencies offline")
    group.add_argument("--models", action="store_true", help="list configured model aliases offline; no provider request")
    group.add_argument("--list-models", action="store_true", help="explicit GET /models on the selected provider; no chat completion")
    group.add_argument("--config-schema", action="store_true", help="print the configuration JSON schema offline")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--change-history-dir", type=Path, help="explicitly persist exact edit backups in a private directory outside the repository (not encrypted)")
    args = parser.parse_args(argv)
    key = ""
    store = tools = history = provider = None
    show_metrics = False
    try:
        manage_sessions = args.list_sessions or args.delete_session
        local_configuration = args.init_config or args.show_config or args.doctor or args.models or args.list_models or args.config_schema
        if os.name not in {"posix", "nt"}:
            raise ValueError("Sprig requires Linux/macOS or Windows 10+")
        if (manage_sessions or local_configuration) and (args.task or args.file or args.interface in {"chat", "tui"} or args.checks or args.resume):
            raise ValueError("local management cannot be combined with tasks or interactive interfaces")
        config_path = args.config or os.environ.get("SPRIG_CONFIG")
        if args.config_schema:
            from .configuration_schema import configuration_schema
            print(json.dumps(configuration_schema(), ensure_ascii=False, indent=2))
            return 0
        if args.init_config:
            if args.no_config:
                raise ValueError("--init-config cannot be combined with --no-config")
            if initialize_config(config_path or default_config_path(), approve):
                print("Private configuration created: " + terminal_text(str(Path(config_path or default_config_path()).expanduser().absolute())))
                print("Set the referenced environment variable, then use --doctor and start sprig.")
                return 0
            return 2
        settings = Settings() if args.no_config or (manage_sessions and args.session_dir) else load_settings(config_path, required=bool(config_path))
        if args.project_config:
            if args.no_config:
                raise ValueError("--project-config cannot be combined with --no-config")
            settings = settings.with_project(args.project_config)
        selected_interface = settings.selection(vars(args), os.environ)[2]
        if selected_interface is None:
            selected_interface = "tui" if not (args.task or args.resume or manage_sessions or local_configuration) and sys.stdin.isatty() and sys.stdout.isatty() else "cli"
        args.chat, args.tui = selected_interface == "chat", selected_interface == "tui"
        if not (manage_sessions or local_configuration or args.resume or args.task or args.chat or args.tui):
            raise ValueError("provide a task, --chat, --tui, or --resume NAME")
        if not local_configuration and args.chat and not sys.stdin.isatty():
            raise ValueError("--chat requires an interactive terminal")
        if not local_configuration and args.tui and (not sys.stdin.isatty() or not sys.stdout.isatty()):
            raise ValueError("--tui requires an interactive terminal (stdin and stdout)")
        options = vars(args).copy()
        if args.tui:
            options["stream"] = True
            options["_tui_stream"] = True
        if manage_sessions or args.models or args.list_models:
            options["_manage_sessions"] = not args.list_models
            if args.list_models and not args.model:
                options["model"] = "model-catalog"
        config, details = settings.resolve(options, os.environ)
        details["interface"] = selected_interface
        key = config.secrets
        show_metrics = config.show_metrics
        session_dir = config.session_dir or Path.home() / ".local/state/sprig-agent/sessions"
        change_dir = config.change_history_dir
        if args.models:
            print(terminal_text(json.dumps(redact({"models": config.model_catalog, "network_contacted": False}, key), ensure_ascii=False, indent=2)))
            return 0
        if args.list_models:
            print(terminal_text(json.dumps(Provider(config).list_models(), ensure_ascii=False, indent=2)))
            return 0
        if local_configuration:
            report = diagnose(config, details) if args.doctor else public_config(config, details)
            print(terminal_text(json.dumps(report, ensure_ascii=False, indent=2)))
            return 0 if report.get("ok", True) else 1
        if manage_sessions:
            store = SessionStore(session_dir, config)
            if args.list_sessions:
                listing = store.scan()
                print(terminal_text(json.dumps(listing["sessions"], ensure_ascii=False, indent=2)))
                if listing["limited"]:
                    print(f"Session listing reached the {listing['limit_reason']} limit; results may be incomplete "
                          f"({listing['scanned_entries']} entries scanned, {listing['read_bytes']} bytes read).", file=sys.stderr)
                    return 2
            elif approve("DELETE local session " + terminal_text(args.delete_session) + "\nThis deletes its saved transcript; it does not change workspace files."):
                store.delete(args.delete_session)
                print("Local session deleted")
            else:
                return 2
            return 0
        if not config.api_key and config.base_url.startswith("https://api.openai.com/"):
            raise ValueError("set SPRIG_API_KEY or OPENAI_API_KEY (AGNES_API_KEY also works at the Agnes HTTPS origin)")
        if args.tui:
            from .tui import run_tui
            save_name = args.save_session or ("auto-" + uuid.uuid4().hex if config.auto_save_session and not args.resume else None)
            return run_tui(config, args.task, session_dir, save_name, args.resume,
                           change_dir=change_dir, show_metrics=show_metrics, files=args.file)
        emit = lambda text: print(terminal_text(redact(text, key)), flush=True)
        confirm = lambda text: approve(redact(text, key))
        emit_delta = lambda text: print(terminal_text(text), end="", flush=True)
        messages, checkpoint = None, None
        session_name = args.resume or args.save_session or ("auto-" + uuid.uuid4().hex if config.auto_save_session else None)
        if session_name:
            store = SessionStore(session_dir, config)
            if args.resume:
                messages = store.load(args.resume)
            else:
                store.ensure_new(session_name)
            checkpoint = lambda history: store.save(session_name, history)
        emit(f"Sprig | model: {config.model} | endpoint: {config.base_url} | workdir: {config.workdir}")
        emit("Prompts and tool results are sent to this endpoint. Commands are NOT sandboxed and may access outside files/network. Writes and commands require approval.")
        if store:
            emit("Local transcript saving enabled (not encrypted). Resume never replays pending actions.")
        history = ChangeHistory(config, change_dir)
        if change_dir:
            emit("Exact edit backups persist locally (not encrypted). Only file-tool edits can be undone; commands are not tracked.")
        tools = Tools(config, confirm, emit_output=emit_delta, history=history)
        provider = Provider(config)
        agent = Agent(config, provider, tools, emit, checkpoint, emit_delta=emit_delta)
        if args.file:
            messages, attached = agent.attach_files(args.file, messages)
            emit("[attached snapshots] " + json.dumps(attached, ensure_ascii=False))
        if args.chat:
            return chat(agent, args.task, messages, emit, autostart=bool(args.task or args.resume))
        _, complete = agent.run(args.task, messages)
        return 0 if complete else 2
    except KeyboardInterrupt:
        print("Interrupted; no pending actions will be replayed on resume", file=sys.stderr)
        return 130
    except (OSError, ValueError, ChangeError, ProviderError, SessionError) as exc:
        print("Error: " + terminal_text(redact(str(exc), key)), file=sys.stderr)
        return 1
    finally:
        if show_metrics and provider is not None:
            print("[usage] " + json.dumps(provider.measurements.report(), ensure_ascii=False), file=sys.stderr, flush=True)
        if tools:
            tools.close()
        elif history:
            history.close()
        if store:
            store.close()
