"""Pinned Click model-task entry; both clients keep their own approved action loops."""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
spec = importlib.util.spec_from_file_location("click_repository_task", REPO / "benchmarks/repositories/click/task.py")
task_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(task_module)
bench = task_module.fixture.bench

from sprig_agent.config import Config, environment_api_key, redact, terminal_text
from sprig_agent.tools import Tools


def prepare(target, adapter):
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise ValueError("prepared task must be new with an existing parent")
    target.mkdir(mode=0o700)
    bench.suite.seed(target / "workspace", adapter.files)
    argv, signature, intact = adapter.bundle(target, adapter.case)
    if not intact():
        raise ValueError("prepared verifier changed")
    (target / "task.txt").write_text(adapter.case["task"], encoding="utf-8")
    return {"scope": "prepared identical repository inputs; no model or candidate commands executed",
            "case": adapter.case["id"], "workspace": str((target / "workspace").resolve()),
            "acceptance_argv": list(argv), "fixture_sha256": bench.suite.digest(adapter.files),
            "verifier_sha256": signature, "repository_task": adapter.metadata}


def self_test(adapter, fixture):
    rows = []
    for phase in ("base", "reference"):
        with bench.disposable() as parent:
            root = parent / "workspace"
            bench.suite.seed(root, task_module.fixture.load(fixture, phase))
            argv, _, intact = adapter.bundle(parent, adapter.case)
            # --self-test authorizes this exact fixed calibration, not model
            # actions. No approval bypass is exposed by the live task entry.
            tools = Tools(Config(root, "offline-calibration", max_output=65536), lambda _: intact())
            try:
                outcome = tools.run(list(argv), timeout=30)
            finally:
                tools.close()
            if outcome.get("timed_out") or outcome.get("output_limited") or outcome.get("error"):
                raise ValueError("oracle calibration did not complete")
            result = json.loads(outcome.get("output", ""))
            expected = {"collected": 642, "checks_passed": 635 if phase == "base" else 642,
                        "skipped": 0, "pytest_returncode": 1 if phase == "base" else 0,
                        "failed": sorted(task_module.fixture.FAILED_BEFORE) if phase == "base" else [],
                        "public_exception_contract": phase == "reference", "other_failures": 0,
                        "schema": 1, "passed": phase == "reference"}
            if result != expected or outcome.get("returncode") != expected["pytest_returncode"] or not intact():
                raise ValueError("oracle calibration differs from fixed counts and failures")
            rows.append({"phase": phase, **result})
    return {"scope": "pinned external oracle calibration; no model requests or agent score", "success": True,
            "repository_task": adapter.metadata, "python": platform.python_version(),
            "platform": platform.platform(), "results": rows}


def load_mini():
    spec = importlib.util.spec_from_file_location("repository_mini_task", REPO / "benchmarks/clients/mini_task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", type=Path)
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--run", action="store_true")
    parser.add_argument("--fixture", type=Path, required=True, help="checksum-verified Click snapshots from benchmark-edit-repo.py --fetch")
    parser.add_argument("--client", choices=("sprig", "mini"), default="sprig")
    parser.add_argument("--model", default=os.environ.get("SPRIG_MODEL", "agnes-3.0-flash"))
    parser.add_argument("--base-url", default=os.environ.get("SPRIG_BASE_URL", "https://apihub.agnes-ai.com/v1"))
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--check-before-task", action="store_true", help="run fixed baseline checks with fresh approval before the normal model loop")
    parser.add_argument("--max-context-bytes", type=int, default=131072)
    parser.add_argument("--request-interval", type=float, default=0, help="0-60 seconds between backend starts; no automatic retry")
    parser.add_argument("--request-timeout", type=float, default=30, help="backend timeout in seconds (0 < value <= 120)")
    parser.add_argument("--report", type=Path, help="explicitly create a new numeric report; never overwrite a file")
    args = parser.parse_args()
    try:
        bench.RequestPacer(args.request_interval)
        if not math.isfinite(args.request_timeout) or not 0 < args.request_timeout <= 120:
            parser.error("--request-timeout must be finite and greater than 0, at most 120 seconds")
        if args.max_tokens is not None and not 1 <= args.max_tokens <= 65536:
            parser.error("--max-tokens must be between 1 and 65536")
        if args.report and (args.report.exists() or args.report.is_symlink() or not args.report.parent.is_dir()):
            parser.error("--report must be new, with an existing parent")
        if args.run:
            key = environment_api_key(args.base_url, os.environ)
            if not key:
                parser.error("live run requires a process environment API key")
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                parser.error("live run requires an interactive terminal for fresh approvals")
            if sys.version_info[:2] != (3, 12):
                parser.error("repository runs require the locked Python 3.12 comparison environment")
        adapter = task_module.ClickTask(args.fixture.resolve())
        if args.prepare:
            report = prepare(args.prepare, adapter)
        elif args.self_test:
            report = self_test(adapter, args.fixture.resolve())
        else:
            from importlib.metadata import version
            if version("pytest") != "8.4.2":
                raise ValueError("acceptance dependencies differ from the pin")
            options = {"model": args.model, "base_url": args.base_url, "api_key": key,
                       "max_steps": args.max_steps, "max_context_bytes": args.max_context_bytes,
                       "max_tokens": args.max_tokens, "thinking": args.thinking,
                       "timeout": args.request_timeout, "check_before_task": args.check_before_task}
            entry = load_mini().run_case if args.client == "mini" else bench.run_case
            report = entry(adapter.case, options, case_adapter=adapter,
                           request_interval=args.request_interval,
                           emit=lambda text: print(terminal_text(redact(text, key)), file=sys.stderr, flush=True))
        report["recorded_at_utc"] = datetime.now(timezone.utc).isoformat()
        rendered = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
        print(rendered, end="")
        if args.report:
            with args.report.open("x", encoding="utf-8", newline="\n") as output:
                output.write(rendered)
        return 0 if args.prepare or report.get("success", report.get("acceptance_passed", False)) else 1
    except KeyboardInterrupt:
        print("Repository task cancelled during setup.", file=sys.stderr)
        return 130
    except (OSError, ValueError, ImportError):
        print("Repository task setup or validation failed; no raw exception data is shown.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
