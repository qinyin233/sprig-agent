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
from .runtime import runtime_label
from .session import SessionError, SessionStore, validate_history
from .settings import Settings, default_config_path, initialize_config, load_settings, public_config
from .tools import Tools
from .questions import terminal_questions
from .skills import local_arguments as skill_arguments
from .mcp import local_arguments as mcp_arguments
from .web_fetch import local_arguments as web_arguments
from .jobs import local_arguments as job_arguments
from .lsp import local_arguments as lsp_arguments


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


def chat(agent, task, messages, emit, *, autostart=True, model_selection=None, switch_model=None, fork_conversation=None):
    """Keep context across turns; only a new task or /continue calls the model."""
    status = 0
    history_ready = True
    agent.restore_plan(messages)

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
    emit("Chat ready. Enter a task; /continue, /help, /status, /ref PATH, /refs, /unref PATH, /context, /changes or /exit.")
    while True:
        try:
            task = input("sprig> ").strip()
        except EOFError:
            return status
        except KeyboardInterrupt:
            had_jobs = agent.tools.jobs.unsettled()
            agent.tools.jobs.request_cancel_all()
            emit("Input cancelled; owned background commands are stopping; conversation retained. Use /exit to close chat."
                 if had_jobs else "Input cancelled; conversation retained. Use /exit to close chat.")
            continue
        if not task:
            continue
        if task == "/exit":
            return status
        try:
            if task == "/help":
                emit('/lsp shows configured servers without startup; /lsp definition|references|implementation|hover SERVER PATH LINE COLUMN or symbols|diagnostics SERVER PATH asks fresh approval; /lsp result ID [OFFSET] reads a local page. Positions are 1-based Unicode columns. Servers cannot apply edits or commands through this client.')
                emit('/jobs lists owned tasks; /job start {"argv":["program","arg"]} approves a background command; /job status|output|wait|cancel ID observes or freshly confirms stopping it. Enable execution.background or --background. Exit/reset stops jobs; restore never restarts them.')
                emit("/web shows local snapshot status; /web URL approves one public HTTP(S) GET; /web result ID [OFFSET] pages retained text without refetch. Enable with [web] enabled=true or --web. No auth, redirects, proxy, scripts or automatic replay.")
                emit("/timeline [OFFSET] lists local user turns; /fork current or /fork MESSAGE confirms a new branch through that whole turn. Workspace files are not rolled back and actions are not replayed. Only the next task or /continue requests the model.")
                emit("/models lists the configured snapshot; /model KEY explicitly confirms a local model change. @startup restores startup model settings. It retains conversation text, removes old reasoning metadata and forks enabled private saves; no request is sent until the next task.")
                emit("/mcp shows local server state; /mcp connect|disconnect|tools|resources|templates|prompts SERVER asks fresh approval for remote requests; /mcp result ID [OFFSET] reads retained local data without replay.")
                emit("/skills lists configured skill metadata; /skill NAME [PATH [OFFSET PAGE_SHA256]] previews local UTF-8 guidance without a model call or script execution. Quote resource paths containing spaces.")
                emit("/summary previews an optional lossy checkpoint; /summary create [END] makes one extra model request after approval; /summary clear restores the full request view. /history MESSAGE [OFFSET] locally pages original data.")
                emit("/logs lists this conversation's retained command output; /log ID [OFFSET] pages historical text; /logs clear requires fresh deletion approval. Retention is opt-in via --command-log-dir or storage.command_log_dir.")
                emit("/ref PATH adds a live file reference; /unref PATH removes it; /refs lists metadata; /refs clear removes all. No model request is sent. Small complete files refresh before requests; larger files provide path/size only.")
                emit("/worktrees lists local checkouts; /worktree PATH creates a detached checkout after fresh approvals. All remaining text is the path; the current conversation stays in this workspace.")
                emit("Enter a task to continue the same conversation. /continue explicitly continues retained history without adding a duplicate request; /status shows local context; /plan shows model-reported progress, never approval or acceptance; /context previews the request budget; /usage shows local numeric request measurements and unknown token counts; /compact confirms request-only elision; /changes lists exact edit backups; /undo and /redo require diff approval; /discard-history confirms removal of backups; /diff and /git show Git changes with approval; /exit closes chat.")
            elif task == "/models" or task == "/model":
                emit(json.dumps({"models": model_selection.catalog() if model_selection else agent.config.model_catalog,
                                 "network_contacted": False}, ensure_ascii=False))
            elif task.startswith("/model "):
                if switch_model is None:
                    emit("Model selection is unavailable for this startup; no model request sent.")
                else:
                    agent, messages, result = switch_model(task[7:].strip(), messages)
                    if result.get("model_changed"):
                        history_ready, status = True, 2
                    emit(json.dumps(result, ensure_ascii=False))
            elif task == "/timeline" or task.startswith("/timeline ") or task == "/fork":
                from .conversation_fork import timeline_page
                offset = int(task[10:].strip()) if task.startswith("/timeline ") else 0
                emit(json.dumps(timeline_page(messages, offset, secrets=agent.config.secrets,
                                             budget=min(agent.config.max_output, 8192)), ensure_ascii=False))
            elif task.startswith("/fork "):
                if fork_conversation is None:
                    emit("Conversation branching is unavailable for this startup; no model request sent.")
                else:
                    agent, messages, result = fork_conversation(task[6:].strip(), messages)
                    if result.get("forked"):
                        history_ready, status = True, 2
                    emit(json.dumps(result, ensure_ascii=False))
            elif task == "/lsp" or task.startswith("/lsp "):
                approved = False
                def observe_lsp(phase):
                    nonlocal approved
                    approved |= phase == "approved"
                with agent.tools.observe_actions(observe_lsp):
                    try:
                        result = agent.tools.execute("lsp", lsp_arguments(task[5:].strip() if task.startswith("/lsp ") else ""))
                    finally:
                        # A failed/cancelled server may already have acted.
                        if approved:
                            status = 2
                            if history_ready:
                                agent.record_workspace_change(messages, {"action": "command", "path": ".", "outcome": "uncertain"})
                emit(json.dumps(result, ensure_ascii=False))
            elif task == "/jobs" or task == "/job" or task.startswith("/job "):
                name, arguments = job_arguments(task[5:].strip() if task.startswith("/job ") else "")
                result = agent.tools.execute(name, arguments)
                emit(json.dumps(result, ensure_ascii=False))
                if name == "run" and result.get("approved") is True:
                    status = 2
                    agent.record_workspace_change(messages, {"action": "command", "path": ".", "outcome": "uncertain"})
            elif task == "/web":
                emit(json.dumps(agent.tools.web.status(), ensure_ascii=False))
            elif task.startswith("/web "):
                emit(json.dumps(agent.tools.web_fetch(**web_arguments(task[5:])), ensure_ascii=False))
            elif task == "/mcp":
                emit(json.dumps(agent.tools.mcp_manager.status(), ensure_ascii=False))
            elif task.startswith("/mcp "):
                result = agent.tools.mcp(**mcp_arguments(task.partition(" ")[2]))
                if result.get("approved") is True:
                    status = 2
                    if history_ready:
                        agent.record_workspace_change(messages, {"action": "external MCP operation", "path": ".", "outcome": "uncertain"})
                emit(json.dumps(result, ensure_ascii=False))
            elif task == "/skills":
                emit(json.dumps(agent.tools.skills.catalog(), ensure_ascii=False))
            elif task.startswith("/skill "):
                emit(json.dumps(agent.tools.skill(**skill_arguments(task.partition(" ")[2])), ensure_ascii=False))
            elif task == "/refs":
                emit(json.dumps(redact(agent.reference_state(), agent.config.secrets), ensure_ascii=False, indent=2))
            elif task == "/refs clear" or task.startswith(("/ref ", "/unref ")):
                operation = "clear" if task == "/refs clear" else "remove" if task.startswith("/unref ") else "add"
                paths = () if operation == "clear" else [task.partition(" ")[2].strip()]
                messages, result = agent.update_references(paths, operation=operation, messages=messages)
                emit(json.dumps(redact(result, agent.config.secrets), ensure_ascii=False))
            elif task == "/continue":
                if messages and len(messages) >= 2:
                    run(None)
                else:
                    emit("No task to continue. Enter a task first.")
            elif task == "/worktrees" or task == "/worktree" or task.startswith("/worktree "):
                if task == "/worktree":
                    emit("Use /worktree PATH to create a detached checkout, or /worktrees to list. The current conversation stays in this workspace.")
                    continue
                from .worktrees import worktree_action
                result = worktree_action(agent.config, agent.tools.approve,
                                         path=task[len("/worktree "):].strip() if task.startswith("/worktree ") else None,
                                         cancel_event=agent.cancel_event)
                if result.get("commands_approved") and messages:
                    status = 2
                    if history_ready:
                        agent.record_workspace_change(messages, {"action": "command", "path": ".", "outcome": "uncertain"})
                elif "error" in result and status == 0:
                    status = 2
                emit(json.dumps(redact(result, agent.config.secrets), ensure_ascii=False, indent=2))
            elif task == "/status":
                emit(f"Messages: {len(messages or [])} | model: {agent.config.model} | acceptance checks: {len(agent.config.checks)} | baseline: {agent.baseline_status} | edit checks: {agent.edit_check_status} | last status: {status}")
                emit("Runtime (current process only): " + json.dumps(agent.runtime_snapshot(), ensure_ascii=False))
            elif task == "/context":
                emit(json.dumps(agent.context_preview(messages), ensure_ascii=False, indent=2))
            elif task == "/summary" or task.startswith("/summary "):
                parts = task.split()
                if len(parts) == 1:
                    result = agent.summary_preview(messages)
                elif parts == ["/summary", "clear"]:
                    result = agent.clear_summary(messages)
                elif len(parts) in {2, 3} and parts[1] == "create":
                    result = agent.summarize(messages, int(parts[2]) if len(parts) == 3 else None)
                else:
                    raise ValueError("Use /summary, /summary create [END], or /summary clear")
                emit(json.dumps(redact(result, agent.config.secrets), ensure_ascii=False, indent=2))
            elif task.startswith("/history "):
                from .summary import history_page
                parts = task.split()
                if len(parts) not in {2, 3}:
                    raise ValueError("Use /history MESSAGE [OFFSET]")
                emit(json.dumps(history_page(messages or [], int(parts[1]), int(parts[2]) if len(parts) == 3 else 0,
                                             budget=min(agent.config.max_output, 8192), secrets=agent.config.secrets), ensure_ascii=False))
            elif task == "/plan":
                emit("Task plan (model-reported; not approval or acceptance): " + json.dumps(agent.plan_snapshot(), ensure_ascii=False))
            elif task == "/usage":
                emit(json.dumps(agent.usage_report(), ensure_ascii=False, indent=2))
            elif task == "/compact":
                preview = agent.context_preview(messages)
                emit(json.dumps(preview, ensure_ascii=False, indent=2))
                if approve(f"Enable configured request-only compaction? Complete historical reads may survive within the byte caps; closed old inspections may be folded if still oversized. User instructions, failures, refusals and the last {agent.config.context_keep_recent_messages} messages are preserved; saved history is unchanged."):
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
            elif task == "/logs" or task.startswith("/logs ") or task.startswith("/log "):
                parts = task.split()
                if parts == ["/logs"]:
                    result = agent.tools.log_control()
                elif parts == ["/logs", "clear"]:
                    result = agent.tools.log_control("clear")
                elif parts[0] == "/log" and len(parts) in (2, 3):
                    result = agent.tools.log_control("read", log_id=parts[1], offset=int(parts[2]) if len(parts) == 3 else 0)
                else:
                    raise ValueError("Use /logs, /logs clear or /log ID [OFFSET]")
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
            if (task in {"/diff", "/git", "/worktrees"} or task.startswith("/worktree ")) and messages and history_ready:
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
    parser.add_argument("--reference-file", "--ref-file", dest="reference_files", action="append", metavar="PATH",
                        help="explicit live workspace file reference; refresh small complete files, attach path/size only for larger files")
    configuration = parser.add_mutually_exclusive_group()
    configuration.add_argument("--config", type=Path, help="explicit trusted TOML path (or SPRIG_CONFIG); default: ~/.sprig/config.toml")
    configuration.add_argument("--no-config", action="store_true", help="ignore configuration files, including SPRIG_CONFIG")
    parser.add_argument("--provider", help="select a named configured provider (or SPRIG_PROVIDER)")
    parser.add_argument("--profile", help="select a trusted named profile (or SPRIG_PROFILE)")
    parser.add_argument("--project-config", type=Path, help="explicit private project TOML; request/UI preferences and tighter policy only; never auto-discovered")
    parser.add_argument("--model", help="model override (or SPRIG_MODEL)")
    parser.add_argument("--wire-api", choices=("chat_completions", "messages", "responses"), help="provider HTTP protocol")
    parser.add_argument("--base-url", help="API root override (or SPRIG_BASE_URL); configured credentials cannot follow a changed URL")
    parser.add_argument("--max-steps", type=int)
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--max-output", type=int)
    parser.add_argument("--command-output-limit", type=int,
                        help="raw command output kill limit (256..8388608 bytes); above max-output uses bounded head/tail display")
    parser.add_argument("--max-tokens", type=int, help="request a provider output-token limit per model call (1-65536); not a total billing cap")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--token-limit-field", choices=("max_tokens", "max_completion_tokens"))
    parser.add_argument("--request-timeout", type=float, help="HTTP idle timeout; separate from approved command timeout")
    parser.add_argument("--request-deadline", type=float, help="total HTTP request limit in seconds (default: 300; maximum: 3600)")
    parser.add_argument("--command-log-dir", type=Path, help="opt in to private bounded command log retention outside all repositories")
    parser.add_argument("--proxy", help="system, direct, or credential-free HTTP(S) proxy URL")
    parser.add_argument("--disable-tool", action="append", choices=TOOL_NAMES, help="disable a tool in schemas and execution; repeatable")
    parser.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=None, help="request chat_template_kwargs.enable_thinking; provider support required, off unless selected")
    parser.add_argument("--preserve-reasoning", action=argparse.BooleanOptionalAction, default=None,
                        help="opt in to retaining reasoning_content for provider tool continuity; hidden from terminal, included in enabled private session saves")
    parser.add_argument("--check-history-format", choices=("tool_calls", "observations"),
                        help="encode application fixed checks as tool calls (default) or labeled user observations for compatible providers")
    parser.add_argument("--stream", action=argparse.BooleanOptionalAction, default=None, help="display model text as it arrives")
    parser.add_argument("--request-usage", action=argparse.BooleanOptionalAction, default=None, help="request final stream token statistics; requires --stream or --tui and provider support")
    parser.add_argument("--metrics", dest="show_metrics", action=argparse.BooleanOptionalAction, default=None, help="print local numeric request measurements at CLI exit; missing token counts stay unknown")
    parser.add_argument("--read-only", action=argparse.BooleanOptionalAction, default=None, help="allow file inspection only; block writes and all commands")
    parser.add_argument("--inherit-env", action=argparse.BooleanOptionalAction, default=None, help="let approved commands inherit additional environment variables")
    parser.add_argument("--max-context-bytes", type=int, help="maximum serialized model request size; not a token/cost estimate")
    parser.add_argument("--compact-context", action=argparse.BooleanOptionalAction, default=None, help="compact old output/edit text; fold closed inspections if still oversized; keep full saved history")
    parser.add_argument("--context-trigger-ratio", type=float, help="compact at this fraction of the request byte cap (0..1), only when compaction is enabled")
    parser.add_argument("--context-keep-recent-messages", type=int, help="protect 6..1000 recent messages from compaction")
    parser.add_argument("--context-keep-read-bytes", type=int, help="retain up to 0..262144 encoded bytes of historical complete reads within the request cap; 0 disables retention")
    parser.add_argument("--project-instructions", action=argparse.BooleanOptionalAction, default=None, help="load workspace AGENTS.md guidance; never reads parent/home directories")
    parser.add_argument("--instruction-file", dest="instruction_files", action="append", help="additional workspace-relative guidance file; repeatable, replaces configured files")
    parser.add_argument("--skill-dir", dest="skill_dirs", action="append", help="explicit skill collection directory; repeatable, replaces configured roots; no automatic execution")
    parser.add_argument("--web", dest="web_enabled", action=argparse.BooleanOptionalAction, default=None, help="opt in to approved public HTTP(S) document fetches; no auth/redirects/proxy/scripts")
    parser.add_argument("--background", dest="background_enabled", action=argparse.BooleanOptionalAction, default=None, help="opt in to approved, owned noninteractive background commands; no restart on restore")
    parser.add_argument("--background-timeout", type=float, default=None, help="background command timeout cap, seconds (0,3600]; foreground timeout is unchanged")
    parser.add_argument("--response-language", help="preferred language for explanations; direct task requests take precedence")
    interface = parser.add_mutually_exclusive_group()
    interface.add_argument("--chat", dest="interface", action="store_const", const="chat", help="keep an interactive conversation open")
    interface.add_argument("--tui", dest="interface", action="store_const", const="tui", help="open the full-screen terminal interface (streams by default)")
    interface.add_argument("--cli", dest="interface", action="store_const", const="cli", help="one-shot interface; override the configured interface")
    parser.add_argument("--check", dest="checks", action="append", type=check_argv, metavar="JSON_ARGV",
                        help="fixed acceptance command, repeatable; execution still requires approval")
    parser.add_argument("--check-before-task", action=argparse.BooleanOptionalAction, default=None,
                        help="propose fixed --check commands before every task/continue/resume; fresh approval required; final checks still run")
    parser.add_argument("--check-after-edit", action=argparse.BooleanOptionalAction, default=None,
                        help="propose fixed checks after successful file-edit batches; fresh approval and final acceptance still required")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--save-session", metavar="NAME")
    group.add_argument("--resume", metavar="NAME")
    group.add_argument("--list-sessions", action="store_true", help="list local session metadata without contacting a model")
    group.add_argument("--delete-session", metavar="NAME", help="delete one local session after confirmation")
    group.add_argument("--prune-sessions", action="store_true", help="apply configured retention to idle sessions, each deletion requires confirmation")
    group.add_argument("--init-config", action="store_true", help="confirm creation of a private TOML template; never overwrite an existing file")
    group.add_argument("--show-config", action="store_true", help="show effective settings with credential values withheld; no model request")
    group.add_argument("--doctor", action="store_true", help="check configuration, workspace privacy rules and dependencies offline")
    group.add_argument("--models", action="store_true", help="list configured model aliases offline; no provider request")
    group.add_argument("--list-skills", action="store_true", help="inspect explicitly configured skill metadata offline; no model or script execution")
    group.add_argument("--list-mcp", action="store_true", help="show configured MCP endpoints offline; no connection or model credentials required")
    group.add_argument("--list-models", action="store_true", help="explicit GET /models on the selected provider; no chat completion")
    group.add_argument("--config-schema", action="store_true", help="print the configuration JSON schema offline")
    group.add_argument("--list-worktrees", action="store_true", help="list local Git worktrees after command approval; no model request")
    group.add_argument("--create-worktree", type=Path, metavar="PATH", help="approve a detached checkout in a new external directory; keep this conversation unchanged")
    parser.add_argument("--worktree-ref", metavar="REF", help="local revision for --create-worktree (default HEAD); never fetch")
    parser.add_argument("--session-dir", type=Path)
    parser.add_argument("--session-max-age-days", type=int, help="retention age for explicit --prune-sessions; never auto-delete")
    parser.add_argument("--session-max-count", type=int, help="retain newest 1..256 valid sessions during explicit pruning")
    parser.add_argument("--dry-run", action="store_true", help="preview --prune-sessions candidates without deletion or approval")
    parser.add_argument("--change-history-dir", type=Path, help="explicitly persist exact edit backups in a private directory outside the repository (not encrypted)")
    args = parser.parse_args(argv)
    key = ""
    store = tools = history = provider = None
    show_metrics = False
    try:
        manage_sessions = args.list_sessions or args.delete_session or args.prune_sessions
        manage_worktrees = args.list_worktrees or args.create_worktree is not None
        if args.worktree_ref is not None and args.create_worktree is None:
            raise ValueError("--worktree-ref requires --create-worktree")
        if args.dry_run and not args.prune_sessions:
            raise ValueError("--dry-run requires --prune-sessions")
        local_configuration = args.init_config or args.show_config or args.doctor or args.models or args.list_models or args.config_schema or args.list_skills or args.list_mcp or manage_worktrees
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
        if manage_sessions or args.models or args.list_models or args.list_skills or args.list_mcp or manage_worktrees:
            options["_manage_sessions"] = not args.list_models
            if args.list_models and not args.model:
                options["model"] = "model-catalog"
        config, details = settings.resolve(options, os.environ)
        details["interface"] = selected_interface
        key = config.secrets
        show_metrics = config.show_metrics
        session_dir = config.session_dir or Path.home() / ".local/state/sprig-agent/sessions"
        change_dir = config.change_history_dir
        if manage_worktrees:
            from .worktrees import worktree_action
            result = worktree_action(config, approve, path=args.create_worktree,
                                     ref="HEAD" if args.worktree_ref is None else args.worktree_ref)
            print(terminal_text(json.dumps(redact(result, key), ensure_ascii=False, indent=2)))
            return 2 if "error" in result or result.get("approved") is False else 0
        if args.models:
            print(terminal_text(json.dumps(redact({"models": config.model_catalog, "network_contacted": False}, key), ensure_ascii=False, indent=2)))
            return 0
        if args.list_mcp:
            print(terminal_text(json.dumps(redact({"servers": [server.public() for server in config.mcp_servers], "network_contacted": False}, key), ensure_ascii=False, indent=2)))
            return 0
        if args.list_skills:
            from .filesystem import Directory
            from .policy import FilePolicy
            from .skills import Skills
            with Directory(config.workdir) as root:
                policy = FilePolicy(root, (config.config_path, *config.config_paths), config.excluded_paths)
                result = Skills(config, root.ensure_open, lambda: None, policy).catalog()
            print(terminal_text(json.dumps(redact({**result, "network_contacted": False}, key), ensure_ascii=False, indent=2)))
            return 2 if result["issues"] or result["limited"] else 0
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
            elif args.prune_sessions:
                result = store.prune(lambda prompt: approve(terminal_text(prompt)), dry_run=args.dry_run)
                print(terminal_text(json.dumps(result, ensure_ascii=False, indent=2)))
                return 2 if "error" in result or result.get("declined") or result.get("skipped") else 0
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
            from .model_selection import ModelSelection
            model_selection = ModelSelection(settings, options, os.environ, config)
            save_name = args.save_session or ("auto-" + uuid.uuid4().hex if config.auto_save_session and not args.resume else None)
            return run_tui(config, args.task, session_dir, save_name, args.resume,
                           change_dir=change_dir, show_metrics=show_metrics, files=args.file, model_selection=model_selection)
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
            checkpoint = lambda history: store.save(session_name, history, **agent.reference_save_options())
        emit(f"Sprig | model: {config.model} | endpoint: {config.base_url} | workdir: {config.workdir}")
        emit("Prompts and tool results are sent to this endpoint. Commands are NOT sandboxed and may access outside files/network. Writes and commands require approval.")
        if store:
            emit("Local transcript saving enabled (not encrypted). Resume never replays pending actions.")
        if config.preserve_reasoning or config.wire_api != "chat_completions":
            emit("Provider reasoning metadata is retained for subsequent requests and enabled local saves (not encrypted); its text is not displayed.")
        history = ChangeHistory(config, change_dir)
        if change_dir:
            emit("Exact edit backups persist locally (not encrypted). Only file-tool edits can be undone; commands are not tracked.")
        ask = (lambda questions: terminal_questions(questions, budget=config.max_output, secrets=key)) if sys.stdin.isatty() else None
        tools = Tools(config, confirm, emit_output=emit_delta, history=history, ask=ask)
        provider = Provider(config)
        def runtime_event(event):
            if event.phase in {"model", "check", "finished"}:
                emit("[runtime] " + runtime_label(event))

        agent = Agent(config, provider, tools, emit, checkpoint, emit_delta=emit_delta, on_event=runtime_event)
        if args.resume:
            state = getattr(store, "loaded_references", None)
            agent.restore_references(state if isinstance(state, dict) else None)
            agent.restore_summary(store.loaded_summary, messages)
            agent.restore_logs(getattr(store, "loaded_logs", None))
            if args.reference_files:
                messages, _ = agent.update_references(config.reference_files, messages=messages)
        if args.file:
            messages, attached = agent.attach_files(args.file, messages)
            emit("[attached snapshots] " + json.dumps(attached, ensure_ascii=False))
        if args.chat:
            from .model_selection import ModelSelection, ModelSwitch
            model_selection = ModelSelection(settings, options, os.environ, config)
            def fork_conversation(target, messages):
                from .conversation_fork import ConversationFork
                nonlocal agent, store, session_name
                candidate = ConversationFork(agent, messages, target,
                                             saving=bool(session_name) or config.auto_save_session, source_name=session_name)
                if not candidate.preflight(confirm, session_dir):
                    return agent, messages, {"forked": False, "approved": False, "network_contacted": False}
                old_store = store
                agent, messages, store, session_name = candidate.commit(session_dir)
                result = {"forked": True, "session": session_name, "through_message": candidate.through,
                          "workspace_rolled_back": False, "network_contacted": False}
                if old_store is not None:
                    try:
                        old_store.close()
                    except OSError:
                        result["warning"] = "branch active, but old session lock cleanup failed; exit before resuming that file elsewhere"
                return agent, messages, result
            def switch_model(target, messages):
                nonlocal agent, provider, config, key, store, session_name
                candidate = ModelSwitch(model_selection, agent, messages, target,
                                        saving=bool(session_name) or config.auto_save_session)
                if not candidate.changed:
                    return agent, messages, {"model_changed": False, "network_contacted": False}
                if not candidate.preflight(confirm, session_dir):
                    return agent, messages, {"approved": False, "model_changed": False, "network_contacted": False}
                old_store, previous_usage = store, agent.usage_report()
                agent, messages, store, session_name = candidate.commit(session_dir)
                provider, config = agent.provider, agent.config
                key = config.secrets
                result = {"model_changed": True, "model": config.model, "provider": config.provider_name,
                          "wire_api": config.wire_api, "session": session_name, "network_contacted": False,
                          "old_runtime_metadata_removed": candidate.dropped, "previous_usage": previous_usage}
                if old_store is not None:
                    try:
                        old_store.close()
                    except OSError:
                        result["warning"] = "model changed, but old session lock cleanup failed; exit before resuming that file elsewhere"
                return agent, messages, result
            return chat(agent, args.task, messages, emit, autostart=bool(args.task or args.resume),
                        model_selection=model_selection, switch_model=switch_model, fork_conversation=fork_conversation)
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
