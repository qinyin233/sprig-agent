"""Terminal entry point; approvals default to no, including non-interactive stdin."""

import argparse
import json
import os
from pathlib import Path
import sys

from . import __version__
from .agent import Agent
from .config import Config, redact, terminal_text
from .provider import Provider, ProviderError
from .session import SessionError, SessionStore
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


def chat(agent, task, messages, emit):
    """Keep context across terminal turns; slash commands never call a model."""
    status = 0
    if task or messages is not None:
        messages, complete = agent.run(task, messages)
        status = 0 if complete else 2
    emit("Chat ready. Enter a task; /help, /status, /context, /compact, /diff, /git or /exit.")
    while True:
        try:
            task = input("sprig> ").strip()
        except EOFError:
            return status
        if not task:
            continue
        if task == "/exit":
            return status
        if task == "/help":
            emit("Enter a task to continue the same conversation. /status shows local context; /context previews the request budget; /compact confirms request-only elision; /diff and /git show Git changes with approval; /exit closes chat.")
        elif task == "/status":
            emit(f"Messages: {len(messages or [])} | model: {agent.config.model} | acceptance checks: {len(agent.config.checks)}")
        elif task == "/context":
            emit(json.dumps(agent.context_preview(messages), ensure_ascii=False, indent=2))
        elif task == "/compact":
            preview = agent.context_preview(messages)
            emit(json.dumps(preview, ensure_ascii=False, indent=2))
            if approve("Enable request-only elision of older successful tool output? User instructions, failures, refusals and the last six messages are preserved; saved history is unchanged."):
                agent.compact_context = True
        elif task in {"/diff", "/git"}:
            argv = ["git", "diff", "--", "."] if task == "/diff" else ["git", "status", "--short", "--", "."]
            result = agent.tools.execute("run", {"argv": argv})
            emit(json.dumps(redact(result, agent.config.api_key), ensure_ascii=False))
        elif task.startswith("/"):
            emit("Unknown chat command. Use /help.")
        else:
            messages, complete = agent.run(task, messages)
            status = 0 if complete else 2


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sprig: a small approval-first coding agent")
    parser.add_argument("task", nargs="?", help="task; optional when resuming")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--model", default=os.environ.get("SPRIG_MODEL", ""))
    parser.add_argument("--base-url", default=os.environ.get("SPRIG_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-output", type=int, default=16000)
    parser.add_argument("--stream", action="store_true", help="display model text as it arrives")
    parser.add_argument("--read-only", action="store_true", help="allow file inspection only; block writes and all commands")
    parser.add_argument("--inherit-env", action="store_true", help="let approved commands inherit additional environment variables")
    parser.add_argument("--max-context-bytes", type=int, default=131072, help="maximum serialized model request size; not a token/cost estimate")
    parser.add_argument("--compact-context", action="store_true", help="explicitly elide older successful tool output from requests; keep saved history")
    interface = parser.add_mutually_exclusive_group()
    interface.add_argument("--chat", action="store_true", help="keep an interactive conversation open")
    interface.add_argument("--tui", action="store_true", help="open the full-screen terminal interface (streams by default)")
    parser.add_argument("--check", action="append", type=check_argv, default=[], metavar="JSON_ARGV",
                        help="fixed acceptance command, repeatable; execution still requires approval")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--save-session", metavar="NAME")
    group.add_argument("--resume", metavar="NAME")
    group.add_argument("--list-sessions", action="store_true", help="list local session metadata without contacting a model")
    group.add_argument("--delete-session", metavar="NAME", help="delete one local session after confirmation")
    parser.add_argument("--session-dir", type=Path, default=Path.home() / ".local/state/sprig-agent/sessions")
    args = parser.parse_args(argv)
    key = os.environ.get("SPRIG_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    store = tools = None
    try:
        manage_sessions = args.list_sessions or args.delete_session
        if os.name not in {"posix", "nt"}:
            raise ValueError("Sprig requires Linux/macOS or Windows 10+")
        if manage_sessions and (args.task or args.chat or args.tui or args.check):
            raise ValueError("session management cannot be combined with tasks or interactive interfaces")
        if not manage_sessions and not args.resume and not args.task and not args.chat and not args.tui:
            raise ValueError("provide a task, --chat, --tui, or --resume NAME")
        if args.chat and not sys.stdin.isatty():
            raise ValueError("--chat requires an interactive terminal")
        if args.tui and (not sys.stdin.isatty() or not sys.stdout.isatty()):
            raise ValueError("--tui requires an interactive terminal (stdin and stdout)")
        config = Config(Path(args.workdir), args.model or ("session-manager" if manage_sessions else ""), args.base_url, key, args.max_steps, args.timeout, args.max_output,
                        stream=args.stream or args.tui, checks=tuple(args.check), read_only=args.read_only, inherit_env=args.inherit_env,
                        max_context_bytes=args.max_context_bytes, compact_context=args.compact_context)
        if manage_sessions:
            store = SessionStore(args.session_dir, config)
            if args.list_sessions:
                print(terminal_text(json.dumps(store.list(), ensure_ascii=False, indent=2)))
            elif approve("DELETE local session " + terminal_text(args.delete_session) + "\nThis deletes its saved transcript; it does not change workspace files."):
                store.delete(args.delete_session)
                print("Local session deleted")
            else:
                return 2
            return 0
        if not key and config.base_url.startswith("https://api.openai.com/"):
            raise ValueError("set SPRIG_API_KEY or OPENAI_API_KEY before using OpenAI")
        if args.tui:
            from .tui import run_tui
            return run_tui(config, args.task, args.session_dir, args.save_session, args.resume)
        emit = lambda text: print(terminal_text(redact(text, key)), flush=True)
        confirm = lambda text: approve(redact(text, key))
        emit_delta = lambda text: print(terminal_text(text), end="", flush=True)
        messages, checkpoint = None, None
        session_name = args.resume or args.save_session
        if session_name:
            store = SessionStore(args.session_dir, config)
            if args.resume:
                messages = store.load(args.resume)
            else:
                store.ensure_new(args.save_session)
            checkpoint = lambda history: store.save(session_name, history)
        emit(f"Sprig | model: {config.model} | endpoint: {config.base_url} | workdir: {config.workdir}")
        emit("Prompts and tool results are sent to this endpoint. Commands are NOT sandboxed and may access outside files/network. Writes and commands require approval.")
        if store:
            emit("Local transcript saving enabled (not encrypted). Resume never replays pending actions.")
        tools = Tools(config, confirm, emit_output=emit_delta)
        agent = Agent(config, Provider(config), tools, emit, checkpoint, emit_delta=emit_delta)
        if args.chat:
            return chat(agent, args.task, messages, emit)
        _, complete = agent.run(args.task, messages)
        return 0 if complete else 2
    except KeyboardInterrupt:
        print("Interrupted; no pending actions will be replayed on resume", file=sys.stderr)
        return 130
    except (OSError, ValueError, ProviderError, SessionError) as exc:
        print("Error: " + terminal_text(redact(str(exc), key)), file=sys.stderr)
        return 1
    finally:
        if tools:
            tools.close()
        if store:
            store.close()
