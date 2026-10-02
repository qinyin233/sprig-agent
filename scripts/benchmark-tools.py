"""Offline retrieval and Python map measurements; not a model success benchmark."""

import argparse
import ast
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]


def measure(source, repeat):
    sys.path.insert(0, str(source))
    from sprig_agent.config import Config
    from sprig_agent.tools import Tools

    with tempfile.TemporaryDirectory(prefix="sprig-tool-bench-") as temporary:
        root = Path(temporary)
        target = "def target():\n    return 'value'\n"
        padding = "# documentation padding for a realistic source module\n" * 2000
        (root / "late.py").write_text(padding + target, encoding="utf-8", newline="")
        (root / "early.py").write_text(target + padding, encoding="utf-8", newline="")
        (root / "small.py").write_text(target, encoding="utf-8", newline="")
        def unexpected_approval(_):
            raise AssertionError("benchmark requested approval")

        tools = Tools(Config(root, "offline", read_only=True, max_output=32768), unexpected_approval)
        try:
            ranged = {"offset", "limit"} <= set(inspect.signature(tools.read_file).parameters)
            cases = []
            for path in ("small.py", "early.py", "late.py"):
                timings, sizes, found = [], [], []
                for _ in range(repeat):
                    start = time.perf_counter()
                    location = tools.execute("search", {"query": "def target", "path": "."})
                    matches = [item for item in location.get("matches", []) if item["path"] == path]
                    if not matches:
                        raise RuntimeError("benchmark target was not discoverable")
                    arguments = {"path": path}
                    if ranged:
                        arguments.update(offset=matches[0]["line"], limit=2)
                    result = tools.execute("read_file", arguments)
                    timings.append((time.perf_counter() - start) * 1000)
                    sizes.append(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()))
                    found.append(target in result.get("content", ""))
                cases.append({"case": path, "target_found_runs": sum(found), "runs": repeat,
                              "median_ms": round(statistics.median(timings), 3),
                              "max_ms": round(max(timings), 3), "tool_result_bytes": sizes[0],
                              "stable_result_size": len(set(sizes)) == 1, "tool_calls_per_run": 2})
            mapping = measure_map(root, Config, Tools, unexpected_approval, repeat)
            digest = hashlib.sha256()
            for file in sorted(source.rglob("*.py")):
                digest.update(file.relative_to(source).as_posix().encode() + b"\0" + file.read_bytes())
            return {"source_sha256": digest.hexdigest(), "ranged_reads": ranged, "cases": cases, "project_map": mapping}
        finally:
            tools.close()


def measure_map(root, Config, Tools, approve, repeat):
    # Different source and names prevent identical files from inflating hits.
    for module in range(100):
        source = "\n".join(f"def module_{module}_function_{function}(): return {function}" for function in range(100))
        (root / f"module_{module}.py").write_text(source, encoding="utf-8", newline="")
    tools = Tools(Config(root, "offline", read_only=True, max_output=1000000), approve)
    parse, parse_calls = ast.parse, []

    def count_parse(*arguments, **kwargs):
        parse_calls.append(True)
        return parse(*arguments, **kwargs)

    ast.parse = count_parse
    try:
        started = time.perf_counter()
        first = tools.execute("project_map", {})
        first_ms, first_parses = (time.perf_counter() - started) * 1000, len(parse_calls)
        first_body = json.dumps(first, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        if first.get("truncated") or len(first.get("files", [])) != 103:
            raise RuntimeError("map benchmark did not cover every fixture")
        timings, identical = [], True
        for _ in range(repeat):
            started = time.perf_counter()
            current = tools.execute("project_map", {})
            timings.append((time.perf_counter() - started) * 1000)
            body = json.dumps(current, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            identical = identical and body == first_body
        return {"files": len(first["files"]), "symbols": sum(len(item.get("symbols", [])) for item in first["files"]),
                "first_ms": round(first_ms, 3), "median_repeated_ms": round(statistics.median(timings), 3),
                "max_repeated_ms": round(max(timings), 3), "repeated_runs": repeat,
                "first_parse_calls": first_parses, "repeated_parse_calls": len(parse_calls) - first_parses,
                "tool_result_bytes": len(first_body), "result_sha256": hashlib.sha256(first_body).hexdigest(),
                "results_identical": identical}
    finally:
        ast.parse = parse
        tools.close()


def worker(source, repeat):
    # Both revisions use the same managed interpreter, fixture, and options.
    environment = dict(os.environ)
    for key in ("SPRIG_API_KEY", "OPENAI_API_KEY"):
        environment.pop(key, None)
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", "--source", str(source),
                             "--repeat", str(repeat)], cwd=REPO, env=environment,
                            capture_output=True, check=True, timeout=60)
    return json.loads(result.stdout)


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(REPO), *arguments])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=11)
    parser.add_argument("--compare-ref", help="trusted local Git revision to measure in an isolated source directory")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, default=REPO / "src", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 50:
        parser.error("--repeat must be from 1 to 50")
    if args.worker:
        print(json.dumps(measure(args.source, args.repeat)))
        return 0
    report = {"scope": "offline retrieval and Python parsing; no model/API or coding-task success comparison",
              "python": platform.python_version(), "platform": platform.platform(),
              "revision": git("rev-parse", "HEAD").decode().strip(),
              "dirty": bool(git("status", "--porcelain")), "repeat": args.repeat,
              "current": worker(REPO / "src", args.repeat)}
    if args.compare_ref:
        revision = git("rev-parse", "--verify", "--end-of-options", args.compare_ref + "^{commit}").decode().strip()
        archive = git("archive", "--format=tar", revision, "src")
        with tempfile.TemporaryDirectory(prefix="sprig-bench-baseline-") as temporary:
            root = Path(temporary)
            with tarfile.open(fileobj=io.BytesIO(archive)) as files:
                for member in files:
                    if not member.isfile() or not member.name.endswith(".py"):
                        continue
                    parts = member.name.split("/")
                    if parts[0] != "src" or any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
                        raise ValueError("unsafe baseline source path")
                    target = root.joinpath(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with files.extractfile(member) as source:
                        target.write_bytes(source.read())
            report["baseline"] = {"revision": revision, **worker(root / "src", args.repeat)}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
