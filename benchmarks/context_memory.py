"""Offline request-preparation resources; not a model task or ranking."""

import argparse
import gc
import hashlib
import json
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_payload
from sprig_agent.tools import TOOL_SCHEMAS


def digest(config, history):
    raw = json.dumps(request_payload(config, history, TOOL_SCHEMAS), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest(), len(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=("ascii", "unicode", "escaped"), default="ascii")
    parser.add_argument("--repeats", type=int, choices=range(1, 6), default=3)
    args = parser.parse_args()
    unit = {"ascii": "data-value ", "unicode": "中文🙂e\u0301 ", "escaped": '"\\\t\r\n'}[args.variant]
    block = (unit * (65536 // len(unit.encode("utf-8")) + 1)).encode("utf-8")[:65536].decode("utf-8", errors="ignore")
    with tempfile.TemporaryDirectory() as workdir:
        config = Config(Path(workdir), "context-memory-fixture", max_output=65536, max_context_bytes=65536)
        history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "Preserve requirements"}]
        for index in range(99):
            identity = str(index)
            history.extend([{"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"fixture.txt"}'}}]},
                {"role": "tool", "tool_call_id": identity, "content": json.dumps({"path": "fixture.txt", "content": block if index < 96 else "recent\n"}, ensure_ascii=False)}])
        original_digest, original_bytes = digest(config, history)
        gc.collect()
        elapsed = []
        for _ in range(args.repeats):
            started = time.perf_counter()
            selected, report = prepare_context(config, history, TOOL_SCHEMAS, compact=True)
            elapsed.append(round((time.perf_counter() - started) * 1000, 3))
        del selected
        gc.collect()
        tracemalloc.start()
        selected, report = prepare_context(config, history, TOOL_SCHEMAS, compact=True)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        selected_digest, selected_bytes = digest(config, selected)
        assert digest(config, history) == (original_digest, original_bytes)
        assert report["before_bytes"] == original_bytes and report["after_bytes"] == selected_bytes
        source_files = (ROOT / "src/sprig_agent").glob("*.py")
        source = hashlib.sha256()
        for path in sorted(source_files):
            source.update(path.name.encode() + b"\0" + path.read_bytes())
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
        print(json.dumps({"scope": "offline context preparation; no model/network calls or coding-performance claims",
            "variant": args.variant, "python": platform.python_version(), "platform": sys.platform,
            "benchmark_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "git_head": commit, "executed_source_sha256": source.hexdigest(), "messages": len(history),
            "fixture_request_sha256": original_digest, "selected_request_sha256": selected_digest,
            "before_bytes": original_bytes, "after_bytes": selected_bytes, "within_budget": report["within_budget"],
            "changes": len(report["changes"]), "history_unchanged": True, "timings_ms_without_tracing": elapsed,
            "median_ms_without_tracing": statistics.median(elapsed), "incremental_peak_traced_bytes": peak,
            "peak_scope": "new allocations during one preparation; excludes already-created fixture and interpreter"}))


if __name__ == "__main__":
    main()
