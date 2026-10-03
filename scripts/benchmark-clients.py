"""Probe pinned native clients; version/help invocations are not coding scores."""

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import re
import shutil
import statistics
import sys
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from sprig_agent.config import Config
from sprig_agent.tools import Tools

task_spec = importlib.util.spec_from_file_location("sprig_task_bench", REPO / "scripts" / "benchmark-tasks.py")
task_bench = importlib.util.module_from_spec(task_spec)
task_spec.loader.exec_module(task_bench)
CLIENT_ROOT = REPO / "benchmarks" / "clients"
CHILD = CLIENT_ROOT / "probe_child.py"
VERSIONS = {"sprig": "0.1.0", "aider": "0.86.2", "opencode": "1.18.34", "pi": "1.0.0", "mini": "2.4.6"}
FLAGS = {"sprig": ("--tui", "--metrics", "--check"), "aider": ("--model", "--openai-api-base", "--no-check-update"),
         "opencode": ("--model", "--agent"), "pi": ("--provider", "--model", "--offline"),
         "mini": ("--model", "--task", "--environment-class")}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def invocation(client):
    if client == "sprig":
        return [sys.executable, "-I", "-m", "sprig_agent"]
    if client in ("aider", "mini"):
        binary = CLIENT_ROOT / client / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        return [str(binary), "-I", str(CLIENT_ROOT / "mini_help.py")] if client == "mini" else [str(binary), "-I", "-m", "aider"]
    modules = CLIENT_ROOT / "node" / "node_modules"
    if client == "opencode":
        # The pinned postinstall links the correct native binary to this name,
        # including ELF/Mach-O binaries on POSIX despite the .exe suffix.
        return [str(modules / "opencode-ai" / "bin" / "opencode.exe")]
    node = shutil.which("node")
    if node is None:
        raise ValueError("Node is required")
    return [node, str(modules / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js")]


def probe(argv, expected_version, flags, repeat, *, version_argv=None, runtime_name=None):
    """Only fixed --version / --help calls; never constructs a model provider."""
    with task_bench.disposable() as root:
        tools = Tools(Config(root, "client-probe", timeout=30, max_output=65536), lambda _: True)
        records = []
        runtime = None
        try:
            if runtime_name:
                result = tools.run([sys.executable, "-I", "-B", str(CHILD), str(root), argv[0], "--version"])
                output = result.get("output", "").strip()
                pattern = r"Python (\d+\.\d+\.\d+)" if runtime_name == "python" else r"v(\d+\.\d+\.\d+)"
                match = re.fullmatch(pattern, output)
                valid = result.get("returncode") == 0 and "error" not in result and not result.get("timed_out") and not result.get("output_limited") and match is not None
                runtime = {"name": runtime_name, "version": match.group(1) if valid else None, "valid": valid}
            for option, count in (("--version", repeat), ("--help", 1)):
                times, bytes_read, valid = [], [], True
                for _ in range(count):
                    started = time.perf_counter()
                    command = version_argv if option == "--version" and version_argv else [*argv, option]
                    result = tools.run([sys.executable, "-I", "-B", str(CHILD), str(root), *command])
                    times.append((time.perf_counter() - started) * 1000)
                    output = result.get("output", "")
                    bytes_read.append(len(output.encode("utf-8")))
                    passed = result.get("returncode") == 0 and not result.get("timed_out") and not result.get("output_limited") and "error" not in result
                    if option == "--version":
                        passed = passed and bool(re.fullmatch(r"(?:(?:sprig|aider|pi|__main__\.py)\s+)?" + re.escape(expected_version), output.strip()))
                    else:
                        passed = passed and all(flag in output for flag in flags)
                    valid = valid and passed
                records.append({"option": "distribution-version" if option == "--version" and version_argv else option,
                                "runs": count, "valid": valid,
                                "first_ms": round(times[0], 3), "median_ms": round(statistics.median(times), 3),
                                "max_ms": round(max(times), 3), "output_bytes": bytes_read})
        finally:
            tools.close()
        return {"success": all(row["valid"] for row in records) and (runtime is None or runtime["valid"]),
                "runtime": runtime, "invocations": records}


def run(clients, repeat):
    result = {"scope": "native metadata/help probe; mini uses a help-only prompt IO adapter; no model requests, task scores, TUI readiness or total memory comparison",
              "platform": platform.platform(), "controller_python": platform.python_version(), "repeat": repeat,
              "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "driver_sha256": digest(Path(__file__)), "child_sha256": digest(CHILD),
              "mini_help_sha256": digest(CLIENT_ROOT / "mini_help.py"),
              "locks": {"sprig": digest(REPO / "uv.lock"), "aider": digest(CLIENT_ROOT / "aider" / "uv.lock"),
                        "mini": digest(CLIENT_ROOT / "mini" / "uv.lock"),
                        "node": digest(CLIENT_ROOT / "node" / "package-lock.json")}, "clients": []}
    for client in clients:
        argv = invocation(client)
        entry = Path(argv[1]) if client == "pi" else Path(argv[0])
        signature = digest(entry)
        # mini's CLI has no --version. Its distribution metadata is checked
        # separately; only --help starts the actual CLI module.
        version_argv = [argv[0], "-I", "-c", "from importlib.metadata import version; print(version('mini-swe-agent'))"] if client == "mini" else None
        runtime_name = "node" if client == "pi" else (None if client == "opencode" else "python")
        row = {"client": client, "expected_version": VERSIONS[client], "entry_sha256": signature,
               **probe(argv, VERSIONS[client], FLAGS[client], repeat, version_argv=version_argv, runtime_name=runtime_name)}
        row["entry_unchanged"] = digest(entry) == signature
        row["success"] = row["success"] and row["entry_unchanged"]
        result["clients"].append(row)
    result["success"] = all(row["success"] for row in result["clients"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=["all", *VERSIONS], default="all")
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 20:
        parser.error("repeat must be from 1 to 20")
    if args.report and (args.report.exists() or args.report.is_symlink() or not args.report.parent.is_dir()):
        parser.error("report must be a new file in an existing directory")
    try:
        report = run(list(VERSIONS) if args.client == "all" else [args.client], args.repeat)
        text = json.dumps(report, ensure_ascii=True, indent=2) + "\n"
        print(text, end="")
        if args.report:
            with args.report.open("x", encoding="utf-8", newline="\n") as target:
                target.write(text)
        return 0 if report["success"] else 1
    except (OSError, ValueError):
        print("Client probe failed; check pinned installations. Raw output and exceptions are not published.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
