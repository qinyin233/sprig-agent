"""Pinned Requests task: offline calibration, prepared CLI task, or approved SDK run."""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import platform
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("requests_repository_task", REPO / "benchmarks/repositories/requests/task.py")
task_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_module)
bench = task_module.bench
from sprig_agent.config import Config, environment_api_key, redact, terminal_text
from sprig_agent.tools import ToolError, Tools


def new_directory(target):
    target = Path(target).absolute()
    if target.exists() or target.is_symlink() or not target.parent.is_dir() or target.resolve().is_relative_to(REPO):
        raise ValueError("task output must be a new external directory with an existing parent")
    if os.name == "nt":
        from sprig_agent.windows import private_directory
        private_directory(target)
    else:
        target.mkdir(mode=0o700)
    return target


def prepare(target, adapter):
    target = new_directory(target)
    bench.suite.seed(target / "workspace", adapter.files)
    argv, signature, intact = adapter.bundle(target, adapter.case)
    if not intact():
        raise ValueError("prepared verifier changed")
    (target / "task.txt").write_text(adapter.case["task"], encoding="utf-8")
    return {"scope": "prepared initial source and protected external acceptance; no model or command executed",
            "workspace": str((target / "workspace").resolve()), "acceptance_argv": list(argv),
            "verifier_sha256": signature, "repository_task": adapter.metadata}


def self_test(adapter, fixture):
    spec = importlib.util.spec_from_file_location("requests_fixed_acceptance_contract", REPO / "benchmarks/repositories/requests/oracle.py")
    contract = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(contract)
    rows = []
    for phase in ("base", "reference"):
        with bench.disposable() as parent:
            root = parent / "workspace"
            bench.suite.seed(root, task_module.load(fixture, phase))
            argv, _, intact = adapter.bundle(parent, adapter.case)
            tools = Tools(Config(root, "offline-calibration", timeout=60, max_output=65536, command_output_limit=65536), lambda _: intact())
            try:
                # This flag authorizes only the fixed, disposable calibration.
                # Live/model commands still use the ordinary fresh approvals.
                outcome = tools.run(list(argv), timeout=60)
            finally:
                tools.close()
            if any(outcome.get(name) for name in ("timed_out", "output_limited", "output_truncated", "error", "outcome")):
                raise ValueError("oracle calibration did not complete")
            data = json.loads(outcome.get("output", ""), object_pairs_hook=contract.unique_object)
            passed = phase == "reference"
            failed = [] if passed else list(contract.FAILED_BEFORE)
            expected = {"schema": 1, "valid": True, "passed": passed, "collected": 58,
                "checks_passed": 58 if passed else 12, "skipped": 0,
                "backends": [{"schema": 1, "backend": name, "collected": 29, "checks_passed": 29 - len(failed),
                              "failed": failed, "skipped": 0, "passed": passed} for name in ("stdlib", "simplejson")]}
            if not intact() or data != expected or outcome.get("returncode") != (0 if passed else 1):
                raise ValueError("oracle calibration differs from the fixed contract")
            rows.append({"phase": phase, **data})
    return {"scope": "fixed offline external acceptance calibration; not a model score", "success": True,
            "python": platform.python_version(), "platform": platform.system(), "repository_task": adapter.metadata,
            "results": rows}


def load_mini():
    spec = importlib.util.spec_from_file_location("requests_mini_task", REPO / "benchmarks/clients/mini_task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--fetch", action="store_true")
    modes.add_argument("--prepare", type=Path)
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--run", action="store_true")
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--report", type=Path, help="new numeric report; never overwrite")
    parser.add_argument("--client", choices=("sprig", "mini"), default="sprig")
    parser.add_argument("--model", default=os.environ.get("SPRIG_MODEL", "agnes-3.0-flash"))
    parser.add_argument("--base-url", default=os.environ.get("SPRIG_BASE_URL", "https://apihub.agnes-ai.com/v1"))
    parser.add_argument("--wire-api", choices=("chat_completions", "messages", "responses"), default="chat_completions")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-tokens", type=int, default=2048)
    parser.add_argument("--max-context-bytes", type=int, default=131072)
    parser.add_argument("--request-timeout", type=float, default=120)
    parser.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=False,
                        help="Sprig preset only; false omits the preset, not a server-side disable")
    parser.add_argument("--extra-body", default="{}", help="bounded provider generation JSON; no reserved request/auth fields")
    parser.add_argument("--temperature", type=float)
    parser.add_argument("--top-p", type=float)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--reasoning-effort", choices=("none", "minimal", "low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--token-limit-field", choices=("max_tokens", "max_completion_tokens"), default="max_tokens")
    parser.add_argument("--request-interval", type=float, default=0)
    parser.add_argument("--check-before-task", action="store_true")
    parser.add_argument("--check-after-edit", action="store_true")
    parser.add_argument("--preserve-reasoning", action="store_true")
    parser.add_argument("--check-history-format", choices=("tool_calls", "observations"), default="tool_calls")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--request-usage", action="store_true")
    parser.add_argument("--compact-context", action="store_true")
    args = parser.parse_args()
    try:
        if args.report and (args.report.exists() or args.report.is_symlink() or not args.report.parent.is_dir()):
            parser.error("--report must be new with an existing parent")
        if args.fetch:
            report = task_module.fetch(args.fixture)
        else:
            if sys.version_info[:2] != (3, 12):
                parser.error("use the locked Requests comparison Python 3.12 environment")
            options = {"model": args.model, "base_url": args.base_url, "wire_api": args.wire_api,
                "max_steps": args.max_steps, "max_tokens": args.max_tokens, "max_context_bytes": args.max_context_bytes,
                "thinking": args.thinking, "extra_body": args.extra_body, "temperature": args.temperature,
                "top_p": args.top_p, "seed": args.seed, "reasoning_effort": args.reasoning_effort,
                "token_limit_field": args.token_limit_field, "timeout": 60, "request_timeout": args.request_timeout,
                "check_before_task": args.check_before_task, "check_after_edit": args.check_after_edit,
                "preserve_reasoning": args.preserve_reasoning, "check_history_format": args.check_history_format,
                "stream": args.stream, "request_usage": args.request_usage, "compact_context": args.compact_context}
            Config(REPO, checks=((sys.executable, "-I", "-B", str(REPO / "benchmarks/repositories/requests/oracle.py")),), **options)
            bench.RequestPacer(args.request_interval)
            if args.client == "mini" and args.wire_api != "chat_completions":
                parser.error("the pinned mini comparison supports only Chat Completions")
            if args.client == "mini" and (args.stream or args.request_usage or args.compact_context or args.check_after_edit
                                           or args.preserve_reasoning or args.check_history_format != "tool_calls"):
                parser.error("selected Sprig runtime controls have no validated mini adapter")
            adapter = task_module.RequestsTask(args.fixture)
            if args.prepare:
                report = prepare(args.prepare, adapter)
            elif args.self_test:
                report = self_test(adapter, args.fixture)
            else:
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    parser.error("live run requires an interactive terminal for fresh approvals")
                key = environment_api_key(args.base_url, os.environ)
                if not key:
                    parser.error("live run requires a process environment API key")
                options["api_key"] = key
                entry = load_mini().run_case if args.client == "mini" else bench.run_case
                report = entry(adapter.case, options, case_adapter=adapter, request_interval=args.request_interval,
                    **({"environment_lock": REPO / "benchmarks/repositories/requests/uv.lock"} if args.client == "mini" else {}),
                    emit=lambda text: print(terminal_text(redact(text, key)), file=sys.stderr, flush=True))
        report["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
        rendered = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
        print(rendered, end="")
        if args.report:
            with args.report.open("x", encoding="utf-8", newline="\n") as output:
                output.write(rendered)
        return 0 if args.fetch or args.prepare or report.get("success", report.get("acceptance_passed", False)) else 1
    except KeyboardInterrupt:
        print("Requests task cancelled during setup.", file=sys.stderr)
        return 130
    except (OSError, ValueError, ImportError, ToolError):
        print("Requests task setup or validation failed; raw exception data was not exported.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
