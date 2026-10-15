"""Diagnostic coding tasks. Live runs retain fresh approvals; no auto-approve flag."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "benchmarks" / "coding"))
sys.path.insert(0, str(REPO / "benchmarks" / "clients"))

import suite
from sprig_agent.agent import Agent
from sprig_agent.cli import approve
from sprig_agent.config import Config, environment_api_key, redact, terminal_text
from request_pacing import PacedProvider, RequestPacer
from tool_diagnostics import ToolDiagnostics
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.policy import command_environment
from sprig_agent.tools import Tools


@contextmanager
def disposable():
    root = Path(tempfile.mkdtemp(prefix="sprig-task-bench-")).resolve()
    original = root.stat()
    try:
        yield root
    finally:
        if root.exists():
            current = root.stat()
            junction = getattr(os.path, "isjunction", lambda _: False)(root)
            if root.resolve() != root or root.is_symlink() or junction or (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise ValueError("temporary root changed; preserved instead of deleting it")
            shutil.rmtree(root)  # Verified absolute directory created by this harness.


def bundle(parent, case):
    """Freeze the verifier and Sprig runner outside the candidate workspace."""
    directory = parent / "verification"
    coding = directory / "benchmarks" / "coding"
    coding.mkdir(parents=True)
    (coding / "suite.py").write_bytes(Path(suite.__file__).read_bytes())
    fields = {key: case[key] for key in ("id", "language", "module", "function", "checks")}
    (coding / "cases.json").write_text(json.dumps({"schema": 1, "cases": [fields]}), encoding="utf-8")
    for source in (REPO / "src").rglob("*.py"):
        target = directory / "src" / source.relative_to(REPO / "src")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    protected = {file.relative_to(directory).as_posix(): file.read_bytes()
                 for file in directory.rglob("*") if file.is_file()}
    signature = suite.digest(protected)
    directories = {path.as_posix() for name in protected for path in Path(name).parents if str(path) != "."}

    def intact():
        try:
            found = set()
            for file in directory.rglob("*"):
                if file.is_symlink() or not file.resolve().is_relative_to(directory):
                    return False
                if file.is_file():
                    name = file.relative_to(directory).as_posix()
                    if name not in protected or file.stat().st_size != len(protected[name]):
                        return False
                    with file.open("rb") as reader:
                        if reader.read(len(protected[name]) + 1) != protected[name]:
                            return False
                    found.add(name)
                elif file.relative_to(directory).as_posix() not in directories:
                    return False
            return found == set(protected)
        except (OSError, RuntimeError):
            return False

    return (sys.executable, "-B", str(coding / "suite.py"), case["id"]), signature, intact


def self_test():
    results = []
    for case in suite.cases().values():
        with disposable() as parent:
            root = parent / "workspace"
            root.mkdir()
            suite.seed(root, suite.fixture_files(case))
            baseline = suite.verify(case, root)
            suite.seed(root, suite.fixture_files(case, reference=True))
            reference = suite.verify(case, root)
            results.append({"case": case["id"], "checks": reference["checks"],
                            "baseline_rejected": not baseline["passed"], "reference_passed": reference["passed"]})
    success = all(row["baseline_rejected"] and row["reference_passed"] for row in results)
    return {"scope": "oracle self-test; no model requests or coding-agent score", "success": success, "cases": results}


def prepare(target, case):
    # Never overwrite an existing project. Export only initial source and task.
    target.mkdir(parents=False, exist_ok=False)
    root = target / "workspace"
    root.mkdir()
    files = suite.fixture_files(case)
    suite.seed(root, files)
    history = suite.synthetic_history(case)
    if history:
        (target / "initial-history.json").write_text(json.dumps(history, ensure_ascii=True), encoding="utf-8")
    argv, signature, _ = bundle(target, case)
    return {"case": case["id"], "workspace": str(root.resolve()), "acceptance_argv": list(argv),
            "fixture_sha256": suite.digest(files), "verifier_sha256": signature,
            "synthetic_history": bool(case.get("history_reads")),
            "note": "Prepared inputs only; no model or candidate commands were executed. Synthetic history needs a client-specific adapter and is not directly comparable."}


def run_case(case, config_options, *, provider_factory=Provider, confirm=approve, emit=None, emit_delta=None,
             case_adapter=None, request_interval=0):
    pacer = RequestPacer(request_interval)
    key = config_options.get("api_key", "")
    with disposable() as parent:
        root = parent / "workspace"
        root.mkdir()
        files = case_adapter.files if case_adapter is not None else suite.fixture_files(case)
        suite.seed(root, files)
        argv, signature, intact = (case_adapter.bundle(parent, case) if case_adapter is not None else bundle(parent, case))
        config = Config(root, checks=(argv,), **config_options)
        lock_signature = hashlib.sha256((REPO / "uv.lock").read_bytes()).hexdigest()
        driver_signature = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        node_version = None
        node = shutil.which("node") if case["language"] == "javascript" else None
        if node:
            version = subprocess.run([node, "--version"], env=command_environment(config), capture_output=True, timeout=5)
            text = version.stdout.decode("ascii", errors="ignore").strip()
            if version.returncode == 0 and len(text) < 32 and text.startswith("v") and all(part.isdecimal() for part in text[1:].split(".")):
                node_version = text
        provider = PacedProvider(provider_factory(config), pacer)
        history = suite.synthetic_history(case)
        approvals = {"requests": 0, "accepted": 0, "rejected": 0, "wait_ms": 0.0}
        tool_records = []
        diagnostics = ToolDiagnostics()

        def fresh_approval(description):
            if not intact():
                return False
            started = time.perf_counter()
            try:
                answer = bool(confirm(redact(description, key)))
            finally:
                approvals["wait_ms"] += (time.perf_counter() - started) * 1000
                approvals["requests"] += 1
            approvals["accepted" if answer else "rejected"] += 1
            return answer and intact()

        class MeasuredTools(Tools):
            def execute(self, name, arguments):
                if not intact():
                    return {"error": "benchmark verifier changed; run is invalid"}
                started = time.perf_counter()
                before = approvals["wait_ms"]
                try:
                    result = super().execute(name, arguments)
                finally:
                    elapsed = (time.perf_counter() - started) * 1000
                    tool_records.append({"tool": name if name in {"read_file", "list_files", "search", "project_map", "edit_file", "run"} else "unknown",
                                         "active_ms": round(max(0, elapsed - (approvals["wait_ms"] - before)), 3),
                                         "result_bytes": None})
                tool_records[-1]["result_bytes"] = len(json.dumps(result, ensure_ascii=True).encode("utf-8"))
                tool_records[-1]["diagnostics"] = diagnostics.record(name, arguments, result)
                if not intact():
                    return {"error": "benchmark verifier changed; run is invalid"}
                return result

        tools = MeasuredTools(config, fresh_approval)
        agent = Agent(config, provider, tools, emit or (lambda _: None), emit_delta=emit_delta)
        started = time.perf_counter()
        outcome, completed = "stopped", False
        try:
            _, completed = agent.run(case["task"], history)
            outcome = "acceptance_passed" if completed else "not_completed"
        except KeyboardInterrupt:
            outcome = "cancelled"
        except ProviderError:
            outcome = "provider_error"
        except (OSError, ValueError):
            outcome = "local_error"
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            tools.close()
        valid = intact()
        report = {"schema": 1, "scope": "one diagnostic Sprig task; no competitor or overall performance claim",
                  "case": case["id"], "category": case["category"], "language": case["language"],
                  "fixture_sha256": suite.digest(files), "verifier_sha256": signature, "verifier_intact": valid,
                  "task_sha256": hashlib.sha256(case["task"].encode("utf-8")).hexdigest(),
                  "initial_history_sha256": hashlib.sha256(json.dumps(suite.synthetic_history(case), sort_keys=True).encode()).hexdigest(),
                  "uv_lock_sha256": lock_signature,
                  "driver_sha256": driver_signature,
                  "python": platform.python_version(), "platform": platform.platform(),
                  "node": node_version,
                  "request_timeout_seconds": config.timeout,
                  "generation_options": config.generation_options(),
                  "model": config.model, "base_url": config.base_url, "stream": config.stream,
                  "request_usage": config.request_usage, "max_steps": config.max_steps,
                  "check_before_task": config.check_before_task, "baseline_status": agent.baseline_status,
                  "max_context_bytes": config.max_context_bytes, "compact_context": config.compact_context,
                  "synthetic_history": bool(case.get("history_reads")),
                  "outcome": outcome if valid else "verifier_modified", "acceptance_passed": completed and valid,
                  "elapsed_ms": round(elapsed, 3), "active_ms": round(max(0, elapsed - approvals["wait_ms"]), 3),
                  "approvals": {**approvals, "wait_ms": round(approvals["wait_ms"], 3)},
                  "tools": tool_records, "requests": agent.usage_report(), "request_pacing": pacer.report(),
                  "tool_diagnostics": diagnostics.metadata()}
        if case_adapter is not None:
            report.update(scope="one repository Sprig task; no overall performance or competitor ranking claim",
                          repository_task=case_adapter.metadata)
        return redact(report, key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--list", action="store_true")
    modes.add_argument("--self-test", action="store_true")
    modes.add_argument("--prepare", type=Path, help="new directory for identical inputs and verifier usable by other agents")
    modes.add_argument("--run", action="store_true", help="one live Sprig task; interactive approvals required")
    parser.add_argument("--case", choices=suite.cases())
    parser.add_argument("--model", default=os.environ.get("SPRIG_MODEL", "agnes-3.0-flash"))
    parser.add_argument("--base-url", default=os.environ.get("SPRIG_BASE_URL", "https://apihub.agnes-ai.com/v1"))
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--check-before-task", action="store_true", help="freshly approve fixed baseline checks before model actions; final acceptance remains mandatory")
    parser.add_argument("--request-interval", type=float, default=0)
    parser.add_argument("--request-timeout", type=float, default=30)
    parser.add_argument("--max-context-bytes", type=int, default=131072)
    parser.add_argument("--compact-context", action="store_true")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--request-usage", action="store_true")
    parser.add_argument("--report", type=Path, help="explicitly write numeric JSON to a new file; existing files are never overwritten")
    args = parser.parse_args()
    try:
        RequestPacer(args.request_interval)
        if not math.isfinite(args.request_timeout) or not 0 < args.request_timeout <= 120:
            parser.error("--request-timeout must be finite and greater than 0, at most 120 seconds")
        if args.report and (args.report.exists() or args.report.is_symlink()):
            parser.error("--report must name a new file; no existing report is overwritten")
        if args.report and not args.report.parent.is_dir():
            parser.error("--report parent directory must already exist")
        if args.list:
            report = [{key: case[key] for key in ("id", "language", "category")} for case in suite.cases().values()]
        elif args.self_test:
            report = self_test()
        else:
            if not args.case:
                parser.error("--case is required for preparation or a live run")
            case = suite.cases()[args.case]
            if args.prepare:
                report = prepare(args.prepare.resolve(), case)
            else:
                key = environment_api_key(args.base_url, os.environ)
                if not key:
                    parser.error("live run requires SPRIG_API_KEY or OPENAI_API_KEY from the process environment")
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    parser.error("live run requires an interactive terminal for fresh write and command approvals")
                report = run_case(case, {"model": args.model, "base_url": args.base_url, "api_key": key,
                                         "max_steps": args.max_steps, "stream": args.stream, "request_usage": args.request_usage,
                                         "max_tokens": args.max_tokens, "thinking": args.thinking,
                                         "timeout": args.request_timeout, "check_before_task": args.check_before_task,
                                         "max_context_bytes": args.max_context_bytes, "compact_context": args.compact_context},
                                  request_interval=args.request_interval,
                                  emit=lambda text: print(terminal_text(redact(text, key)), file=sys.stderr, flush=True),
                                  emit_delta=(lambda text: print(terminal_text(redact(text, key)), end="", file=sys.stderr, flush=True))
                                  if args.stream else None)
        rendered = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
        print(rendered, end="")
        if args.report:
            with args.report.open("x", encoding="utf-8", newline="\n") as output:
                output.write(rendered)
        return 0 if args.list or args.prepare or report.get("success", report.get("acceptance_passed", False)) else 1
    except (OSError, ValueError):
        print("Benchmark setup or local validation failed; no raw exception or credential data is shown.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
