"""Terminal entry point; approvals default to no, including non-interactive stdin."""

import argparse
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Sprig: a small approval-first coding agent (POSIX)")
    parser.add_argument("task", nargs="?", help="task; optional when resuming")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--workdir", default=".")
    parser.add_argument("--model", default=os.environ.get("SPRIG_MODEL", ""))
    parser.add_argument("--base-url", default=os.environ.get("SPRIG_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--max-output", type=int, default=16000)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--save-session", metavar="NAME")
    group.add_argument("--resume", metavar="NAME")
    parser.add_argument("--session-dir", type=Path, default=Path.home() / ".local/state/sprig-agent/sessions")
    args = parser.parse_args(argv)
    key = os.environ.get("SPRIG_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    store = tools = None
    try:
        if os.name != "posix":
            raise ValueError("this MVP requires POSIX (Linux/macOS); Windows is not supported")
        if not args.resume and not args.task:
            raise ValueError("provide a task, or --resume NAME")
        config = Config(Path(args.workdir), args.model, args.base_url, key, args.max_steps, args.timeout, args.max_output)
        if not key and config.base_url.startswith("https://api.openai.com/"):
            raise ValueError("set SPRIG_API_KEY or OPENAI_API_KEY before using OpenAI")
        emit = lambda text: print(terminal_text(redact(text, key)), flush=True)
        confirm = lambda text: approve(redact(text, key))
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
        tools = Tools(config, confirm)
        _, complete = Agent(config, Provider(config), tools, emit, checkpoint).run(args.task, messages)
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
