"""Offline retrieval and multilingual map measurements; not model task success."""

import argparse
import ast
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.util
import inspect
from importlib.metadata import version
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
import tracemalloc

REPO = Path(__file__).resolve().parents[1]


def source_digest(source):
    digest = hashlib.sha256()
    for file in sorted(source.rglob("*.py")):
        digest.update(file.relative_to(source).as_posix().encode() + b"\0" + file.read_bytes())
    return digest.hexdigest()


def measure_regex_search(source, repeat):
    """Known definition windows: one regex versus three fixed literal queries."""
    sys.path.insert(0, str(source))
    from sprig_agent.config import Config
    from sprig_agent.tools import Tools
    if Path(inspect.getfile(Tools)).resolve().parent.parent != source.resolve():
        raise ValueError("benchmark loaded the wrong source revision")
    supports_regex = "regex" in inspect.signature(Tools.search).parameters
    def unexpected_approval(action):
        raise AssertionError("read-only search requested approval")
    def encode(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    cases = []
    for label, count, noise, focused in (("many_files_three_definitions", 32, 8, True),
                                       ("one_file_three_definitions", 1, 512, True),
                                       ("literal_control", 32, 8, False)):
        with tempfile.TemporaryDirectory(prefix="sprig-regex-bench-") as temporary:
            root, expected, digest, input_bytes = Path(temporary), [], hashlib.sha256(), 0
            for module in range(count):
                path = f"module_{module:03}.py"
                rows = [row for number in range(noise) for row in (
                    f"def noise_{number:04}():", "    return '值🙂'")]
                positions = []
                for operation in ("load", "save", "delete"):
                    positions.append(len(rows) + 1)
                    rows.extend((f"{'async ' if operation == 'save' else ''}def {operation}_item():",
                                 f"    return '{operation}'"))
                body = ("\r\n".join(rows) + "\r\n").encode()
                (root / path).write_bytes(body)
                digest.update(path.encode() + b"\0" + body)
                input_bytes += len(body)
                for line in positions if focused else positions[:1]:
                    expected.append({"path": path, "line": line, "text": rows[line - 1],
                                     "before": rows[max(0, line - 2):line - 1], "after": rows[line:line + 1]})
            expected.sort(key=lambda hit: (hit["path"], hit["line"]))
            common = {"path": ".", "glob": "*.py", "context": 1, "limit": 1000}
            if focused and supports_regex:
                calls = [{**common, "query": r"^(?:async )?def (?:load|save|delete)_item\(", "regex": True}]
            else:
                operations = ("load", "save", "delete") if focused else ("load",)
                calls = [{**common, "query": f"def {operation}_item("} for operation in operations]
            samples = []
            for run in range(repeat):
                tools = Tools(Config(root, "offline", read_only=True, max_output=256000), unexpected_approval)
                try:
                    started = time.perf_counter()
                    results = [tools.execute("search", call) for call in calls]
                    elapsed = (time.perf_counter() - started) * 1000
                finally:
                    tools.close()
                if any(result.get("error") or result.get("truncated") or result.get("timed_out")
                       or result.get("skipped_files") for result in results):
                    raise ValueError("regex comparison produced incomplete retrieval")
                matches = sorted((hit for result in results for hit in result["matches"]),
                                 key=lambda hit: (hit["path"], hit["line"]))
                if matches != expected:
                    raise ValueError("regex comparison changed a known definition window")
                reads = sum(result["files_read"] for result in results)
                read_bytes = sum(result["bytes_read"] for result in results)
                if reads != count * len(calls) or read_bytes != input_bytes * len(calls):
                    raise ValueError("regex comparison read counters differ from fixture bytes")
                samples.append({"run": run + 1, "ms": round(elapsed, 3),
                                "files_read": reads, "bytes_read": read_bytes,
                                "tool_result_bytes": sum(len(encode(result)) for result in results)})
            cases.append({"case": label, "files": count, "source_bytes": input_bytes,
                          "fixture_sha256": digest.hexdigest(), "calls": calls,
                          "tool_calls_per_run": len(calls), "matching_lines": len(expected),
                          "match_windows_sha256": hashlib.sha256(encode(expected)).hexdigest(),
                          "median_ms": round(statistics.median(sample["ms"] for sample in samples), 3),
                          "samples": samples})
    return {"source_sha256": source_digest(source), "supports_regex": supports_regex,
            "scope": "scripted queries over synthetic Python fixtures; identical known windows, not model decisions or coding-task success",
            "cases": cases}


def measure_scan_cache(source, repeat):
    """Complete sweeps across entry/byte bounds; no model or cached file reads."""
    sys.path.insert(0, str(source))
    from sprig_agent.config import Config
    from sprig_agent.tools import Tools
    from sprig_agent.index import MAX_CACHE_BYTES, MAX_CACHE_ENTRIES
    if Path(inspect.getfile(Tools)).resolve().parent.parent != source.resolve():
        raise ValueError("benchmark loaded the wrong source revision")
    languages = ("py", "js", "ts", "tsx", "rs", "go", "c", "cpp", "java")
    def unexpected_approval(action):
        raise AssertionError("read-only scan requested approval")
    cases = []
    for label, count, declarations, mixed, filtered in (
        ("fits_entry_limit", 128, 1, False, False),
        ("one_over_entry_limit", 129, 100, False, True),
        ("twice_entry_limit", 257, 50, False, True),
        ("over_byte_limit", 65, 400, False, True),
        ("nine_languages_over_entry_limit", 144, 1, True, True),
    ):
        with tempfile.TemporaryDirectory(prefix="sprig-scan-cache-bench-") as temporary:
            root, digest, input_bytes = Path(temporary), hashlib.sha256(), 0
            for module in range(count):
                extension = languages[module % len(languages)] if mixed else "py"
                rows = []
                for number in range(declarations):
                    name = f"function_{module:04}_{number:04}"
                    rows.append({
                        "py": f"def {name}(): return {number}",
                        "js": f"export function {name}() {{ return {number}; }}",
                        "ts": f"export function {name}(): number {{ return {number}; }}",
                        "tsx": f"export function {name}() {{ return <span>{{{number}}}</span>; }}",
                        "rs": f"pub fn {name}() -> i32 {{ {number} }}",
                        "go": f"func {name}() int {{ return {number} }}",
                        "c": f"int {name}(void) {{ return {number}; }}",
                        "cpp": f"int {name}() {{ return {number}; }}",
                        "java": f"public int {name}() {{ return {number}; }}",
                    }[extension])
                text = "\n".join(rows) + "\n"
                if extension == "go":
                    text = "package fixture\n" + text
                if extension == "java":
                    text = f"public class Module{module:04} {{\n" + text + "}\n"
                filename, body = f"module_{module:04}.{extension}", text.encode("utf-8")
                (root / filename).write_bytes(body)
                digest.update(filename.encode() + b"\0" + body)
                input_bytes += len(body)
            query = f"function_{count - 1:04}_{declarations - 1:04}"
            arguments = {"query": query} if filtered else {}
            output_budget = 16000 if filtered else 1000000
            tools = Tools(Config(root, "offline", read_only=True, max_output=output_budget),
                          unexpected_approval)
            counters = {"ast_parses": 0, "native_parses": 0, "file_reads": 0, "file_bytes": 0, "declarations_seen": 0}
            originals = (ast.parse, tools.index.syntax.parse, tools._read, tools.index.symbols)
            def count_ast(*args, **kwargs):
                counters["ast_parses"] += 1
                return originals[0](*args, **kwargs)
            def count_native(*args, **kwargs):
                counters["native_parses"] += 1
                return originals[1](*args, **kwargs)
            def count_read(*args, **kwargs):
                value = originals[2](*args, **kwargs)
                counters["file_reads"] += 1
                counters["file_bytes"] += len(value)
                return value
            def count_symbols(*args, **kwargs):
                value = originals[3](*args, **kwargs)
                if isinstance(value, list):
                    counters["declarations_seen"] += len(value)
                return value
            ast.parse, tools.index.syntax.parse, tools._read, tools.index.symbols = count_ast, count_native, count_read, count_symbols
            runs, result_hashes = [], set()
            try:
                for iteration in range(repeat + 1):
                    before = dict(counters)
                    started = time.perf_counter()
                    result = tools.execute("project_map", arguments)
                    elapsed = (time.perf_counter() - started) * 1000
                    encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
                    names = [symbol["name"] for file in result.get("files", []) for symbol in file.get("symbols", [])]
                    if ("error" in result or result.get("truncated") or any(file.get("symbols_unavailable") for file in result.get("files", []))
                            or (filtered and not any(name == query or name.endswith("." + query) for name in names))
                            or (not filtered and len(result.get("files", [])) != count)):
                        raise ValueError("cache scan did not return the complete required result")
                    delta = {name: value - before[name] for name, value in counters.items()}
                    if delta["file_reads"] != count or delta["file_bytes"] != input_bytes:
                        raise ValueError("cache scan skipped fresh source reads")
                    expected_declarations = count * declarations + (count // len(languages) if mixed else 0)
                    if delta["declarations_seen"] != expected_declarations:
                        raise ValueError("cache scan did not inspect every known declaration")
                    if iteration == 0 and delta["ast_parses"] + delta["native_parses"] != count:
                        raise ValueError("cache scan cold count does not match distinct source files")
                    if len(tools.index.entries) > MAX_CACHE_ENTRIES or tools.index.bytes > MAX_CACHE_BYTES:
                        raise ValueError("cache scan exceeded cache bounds")
                    result_hashes.add(hashlib.sha256(encoded).hexdigest())
                    runs.append({"iteration": iteration, "elapsed_ms": round(elapsed, 3), **delta,
                                 "cache_entries": len(tools.index.entries), "cache_bytes": tools.index.bytes,
                                 "tool_result_bytes": len(encoded)})
            finally:
                ast.parse, tools.index.syntax.parse, tools._read, tools.index.symbols = originals
                tools.close()
            if len(result_hashes) != 1:
                raise ValueError("cache policy changed repeated tool contents")
            cases.append({"case": label, "files": count, "declarations_per_file": declarations,
                          "mixed_languages": mixed, "filtered": filtered, "max_output": output_budget,
                          "source_bytes": input_bytes, "fixture_sha256": digest.hexdigest(),
                          "result_sha256": next(iter(result_hashes)), "results_identical": True,
                          "cold": runs[0], "warm": runs[1:],
                          "median_warm_ms": round(statistics.median(row["elapsed_ms"] for row in runs[1:]), 3)})
    return {"source_sha256": source_digest(source), "max_cache_entries": MAX_CACHE_ENTRIES,
            "max_cache_bytes": MAX_CACHE_BYTES, "cases": cases}


def measure_edit_context(source, repeat):
    sys.path.insert(0, str(source))
    from sprig_agent.agent import Agent
    if Path(inspect.getfile(Agent)).resolve().parent.parent != source.resolve():
        raise ValueError("benchmark loaded the wrong source revision")
    spec = importlib.util.spec_from_file_location("context_replay", REPO / "benchmarks/context/replay.py")
    replay = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(replay)
    rows = []
    for _ in range(repeat):
        with tempfile.TemporaryDirectory(prefix="sprig-context-replay-") as temporary:
            row = replay.run(Path(temporary), compact=True)
        if (not row["wire_history_valid"] or not row["saved_arguments_full"]
                or row["transport_requests"] != row["http_requests"]
                or any(size > 131072 for size in row["request_bytes"])
                or row["edit_approvals"] != row["changed_files"]):
            raise ValueError("replay violated protocol, budget, approval or history invariants")
        if row["complete"]:
            if row["changed_files"] != replay.EDIT_COUNT or not row["checks_passed"] or row["check_approvals"] != 1:
                raise ValueError("completed replay lacks fixed acceptance evidence")
        elif not row["budget_blocked"] or row["check_approvals"]:
            raise ValueError("unexpected replay failure")
        rows.append(row)
    return {"source_sha256": source_digest(source), "fixture": {"edits": replay.EDIT_COUNT,
            "old_and_new_characters_each": replay.TEXT_LENGTH, "max_request_bytes": 131072,
            "max_output": 65536, "compact_enabled": True}, "runs": rows}


def measure_click_search(source, fixture, repeat):
    """Compare actual retrieval of the same known code window, without a model."""
    sys.path.insert(0, str(source))
    from sprig_agent.config import Config
    from sprig_agent.filesystem import Directory
    from sprig_agent.tools import Tools
    if Path(inspect.getfile(Tools)).resolve().parent.parent != source.resolve():
        raise ValueError("benchmark loaded the wrong source revision")
    spec = importlib.util.spec_from_file_location("click_snapshot", REPO / "scripts/benchmark-edit-repo.py")
    click = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(click)
    files = click.load(fixture, "base")
    scoped = {"glob", "context"} <= set(inspect.signature(Tools.search).parameters)
    cases = []
    with click.bench.disposable() as root:
        click.bench.suite.seed(root, files)
        for name, path, query, mode, context in (
            ("known_source_file", "src/click/core.py", "def format_commands(", "file", 2),
            ("test_file_filter", "tests/test_options.py", "def test_unknown_options(", "glob", 2),
            ("unscoped_search_control", "src/click/core.py", "def format_commands(", "directory", 0),
        ):
            lines = [line.rstrip("\r\n") for line in io.StringIO(files[path].decode("utf-8"), newline="")]
            hits = [index for index, line in enumerate(lines) if query in line]
            if len(hits) != 1:
                raise ValueError("pinned target is no longer unique")
            line = hits[0] + 1
            offset, stop = max(1, line - context), min(len(lines), line + context)
            expected = lines[offset - 1:stop]
            rows = []
            for _ in range(repeat):
                def unexpected_approval(_):
                    raise AssertionError("read-only retrieval requested approval")
                tools = Tools(Config(root, "offline", read_only=True), unexpected_approval)
                counters = {"content_file_reads": 0, "content_bytes_read": 0}
                original_open = Directory.open_read
                @contextmanager
                def counted_open(directory, filename, **options):
                    with original_open(directory, filename, **options) as reader:
                        counters["content_file_reads"] += 1
                        class Reader:
                            def fileno(self):
                                return reader.fileno()
                            def read(self, *arguments):
                                body = reader.read(*arguments)
                                counters["content_bytes_read"] += len(body)
                                return body
                        yield Reader()
                arguments = {"query": query, "path": str(Path(path).parent).replace("\\", "/")}
                if scoped:
                    arguments.update(context=context)
                    if mode == "file":
                        arguments["path"] = path
                    elif mode == "glob":
                        arguments["glob"] = path
                try:
                    Directory.open_read = counted_open
                    started = time.perf_counter()
                    search = tools.execute("search", arguments)
                    results = [search]
                    if not scoped and context:
                        results.append(tools.execute("read_file", {
                            "path": path, "offset": offset, "limit": stop - offset + 1}))
                    elapsed = (time.perf_counter() - started) * 1000
                finally:
                    Directory.open_read = original_open
                    tools.close()
                if (any("error" in result for result in results) or search.get("truncated")
                        or (len(results) > 1 and results[1].get("line_truncated"))):
                    raise ValueError("retrieval was incomplete")
                matches = [hit for hit in search["matches"] if hit["path"] == path]
                if len(matches) != 1 or matches[0]["line"] != line:
                    raise ValueError("retrieval did not find the fixed target")
                hit = matches[0]
                if not context:
                    actual = [hit["text"]]
                elif scoped:
                    actual = hit["before"] + [hit["text"]] + hit["after"]
                else:
                    actual = [text.rstrip("\r\n") for text in io.StringIO(results[1]["content"], newline="")]
                if actual != expected:
                    raise ValueError("retrieved context differs from pinned source")
                rows.append({**counters, "tool_calls": len(results), "tool_ms": round(elapsed, 3),
                             "tool_result_bytes": sum(len(json.dumps(result, ensure_ascii=False).encode()) for result in results),
                             "context_sha256": hashlib.sha256(json.dumps(actual, ensure_ascii=False).encode()).hexdigest()})
            cases.append({"case": name, "path": path, "query": query, "line": line, "context": context,
                          "max_output": 16000, "scoped_search": scoped and mode != "directory", "context_exact_runs": repeat,
                          "target_source_sha256": hashlib.sha256(files[path]).hexdigest(), "runs": rows,
                          "median_ms": round(statistics.median(row["tool_ms"] for row in rows), 3)})
    return {"source_sha256": source_digest(source), "snapshot_sha256": click.PINS["base"][2],
            "upstream_commit": click.PINS["base"][0], "cases": cases}


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
            return {"source_sha256": digest.hexdigest(), "ranged_reads": ranged, "cases": cases, "project_map": mapping,
                    "multilingual_map": measure_multilingual(Config, Tools, unexpected_approval, repeat),
                    "focused_map": measure_focused_map(Config, Tools, unexpected_approval, repeat),
                    "python_index_bounds": measure_index_bounds(Config, Tools, unexpected_approval, repeat)}
        finally:
            tools.close()


def measure_index_bounds(Config, Tools, approve, repeat):
    fixtures = {
        "qualified_name_amplification": "class " + "A" * 20000 + ":\n" + "".join(f"    def f{number}(self): pass\n" for number in range(512)),
        "many_declarations": "\n".join(f"def f{number}(): pass" for number in range(4097)),
        "ordinary_class": "class Service:\n" + "".join(f"    def f{number}(self): pass\n" for number in range(128)),
    }
    cases = []
    with tempfile.TemporaryDirectory(prefix="sprig-index-bound-bench-") as temporary:
        root = Path(temporary)
        for name, body in fixtures.items():
            (root / "sample.py").write_text(body, encoding="utf-8", newline="")
            timings, peaks, bodies = [], [], []
            for _ in range(repeat):
                # Fresh index every time. Trace only the actual map call, not
                # fixture construction, imports or encoding the final report.
                tools = Tools(Config(root, "offline", read_only=True, max_output=1000000), approve)
                try:
                    tracemalloc.start()
                    started = time.perf_counter()
                    result = tools.execute("project_map", {})
                    timings.append((time.perf_counter() - started) * 1000)
                    peaks.append(tracemalloc.get_traced_memory()[1])
                finally:
                    tracemalloc.stop()
                    tools.close()
                bodies.append(json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
            cases.append({"case": name, "source_bytes": len(body.encode()), "source_sha256": hashlib.sha256(body.encode()).hexdigest(),
                          "runs": repeat, "median_ms": round(statistics.median(timings), 3), "max_ms": round(max(timings), 3),
                          "median_traced_peak_bytes": int(statistics.median(peaks)), "max_traced_peak_bytes": max(peaks),
                          "returned_symbols": sum(len(file.get("symbols", [])) for file in result.get("files", [])),
                          "symbols_unavailable": any(file.get("symbols_unavailable") for file in result.get("files", [])),
                          "truncated": result.get("truncated"), "tool_result_bytes": len(bodies[0]),
                          "result_sha256": hashlib.sha256(bodies[0]).hexdigest(), "results_identical": len(set(bodies)) == 1})
    return {"scope": "fresh Python map calls with tracemalloc; traced Python allocation peak, not total process RSS or native memory",
            "max_output": 1000000, "cases": cases}


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


def worker(source, repeat, click_fixture=None, edit_context=False, scan_cache=False, regex_search=False):
    # Both revisions use the same managed interpreter, fixture, and options.
    environment = dict(os.environ)
    for key in ("SPRIG_API_KEY", "OPENAI_API_KEY"):
        environment.pop(key, None)
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--source", str(source), "--repeat", str(repeat)]
    if click_fixture is not None:
        command.extend(("--click-fixture", str(click_fixture)))
    if edit_context:
        command.append("--edit-context")
    if scan_cache:
        command.append("--scan-cache")
    if regex_search:
        command.append("--regex-search")
    result = subprocess.run(command, cwd=REPO, env=environment,
                            capture_output=True, check=True, timeout=60)
    return json.loads(result.stdout)


def measure_focused_map(Config, Tools, approve, repeat):
    with tempfile.TemporaryDirectory(prefix="sprig-focused-map-bench-") as temporary:
        root = Path(temporary).resolve()
        body = "".join(f"def helper_{number}(): return {number}\n" for number in range(1000))
        body += "def resolve_customer_route(): return 'value'\n"
        (root / "routing.py").write_text(body, encoding="utf-8", newline="")
        targeted = "query" in inspect.signature(Tools.project_map).parameters
        cases = []
        for name, cap, focus in (("default_budget_full_map", 16000, False),
                                 ("complete_budget_full_map", 1000000, False),
                                 ("default_budget_target_lookup", 16000, targeted)):
            tools = Tools(Config(root, "offline", read_only=True, max_output=cap), approve)
            arguments = {"query": "resolve_customer_route"} if focus else {}
            try:
                started = time.perf_counter()
                first = tools.execute("project_map", arguments)
                first_ms = (time.perf_counter() - started) * 1000
                timings, bodies, found = [], [], []
                for _ in range(repeat):
                    started = time.perf_counter()
                    result = tools.execute("project_map", arguments)
                    timings.append((time.perf_counter() - started) * 1000)
                    encoded = json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
                    bodies.append(encoded)
                    found.append(any(symbol["name"] == "resolve_customer_route"
                                     for file in result.get("files", []) for symbol in file.get("symbols", [])))
                first_body = json.dumps(first, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
                cases.append({"case": name, "max_output": cap, "query_used": focus,
                              "target_found_runs": sum(found), "runs": repeat,
                              "first_ms": round(first_ms, 3), "median_repeated_ms": round(statistics.median(timings), 3),
                              "max_repeated_ms": round(max(timings), 3), "tool_result_bytes": len(first_body),
                              "returned_symbols": sum(len(file.get("symbols", [])) for file in first.get("files", [])),
                              "truncated": first.get("truncated"), "results_identical": all(encoded == first_body for encoded in bodies)})
            finally:
                tools.close()
        return {"scope": "one 1,001-declaration fixture; target lookup intentionally omits other declarations, not identical full-map contents or model success",
                "supports_query": targeted, "fixture_sha256": hashlib.sha256(body.encode()).hexdigest(), "cases": cases}


def measure_multilingual(Config, Tools, approve, repeat):
    with tempfile.TemporaryDirectory(prefix="sprig-language-bench-") as temporary:
        root, expected = Path(temporary), {}
        for language in ("js", "ts", "tsx", "rs", "go", "c", "cpp", "java"):
            for module in range(10):
                names = [f"module_{module}_function_{number}" for number in range(10)]
                if language in {"js", "ts"}:
                    source = "\n".join(f"export function {name}() {{ return 1; }}" for name in names)
                elif language == "tsx":
                    source = "\n".join(f"export function {name}() {{ return <p>value</p>; }}" for name in names)
                elif language == "rs":
                    source = "\n".join(f"pub fn {name}() -> i32 {{ 1 }}" for name in names)
                elif language == "go":
                    source = "package sample\n" + "\n".join(f"func {name}() int {{ return 1 }}" for name in names)
                elif language in {"c", "cpp"}:
                    source = "\n".join(f"int {name}(void) {{ return 1; }}" for name in names)
                else:
                    container = f"Service{module}"
                    source = f"class {container} {{\n" + "\n".join(f"int {name}() {{ return 1; }}" for name in names) + "\n}"
                    names = [container] + [f"{container}.{name}" for name in names]
                file = f"module_{module}.{language}"
                expected[file] = names
                (root / file).write_text(source, encoding="utf-8", newline="")
        tools = Tools(Config(root, "offline", read_only=True, max_output=1000000), approve)
        parse_calls, parser = [], getattr(getattr(tools, "index", None), "syntax", None)
        original = parser.parse if parser is not None else None

        def count_parse(*args, **kwargs):
            parse_calls.append(True)
            return original(*args, **kwargs)

        if parser is not None:
            parser.parse = count_parse
        try:
            started = time.perf_counter()
            first = tools.execute("project_map", {})
            first_ms, first_parses = (time.perf_counter() - started) * 1000, len(parse_calls)
            first_body = json.dumps(first, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            if first.get("truncated") or len(first.get("files", [])) != len(expected):
                raise RuntimeError("multilingual map benchmark did not cover every fixture")
            timings, identical = [], True
            for _ in range(repeat):
                started = time.perf_counter()
                current = tools.execute("project_map", {})
                timings.append((time.perf_counter() - started) * 1000)
                body = json.dumps(current, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
                identical = identical and body == first_body
            found = sum([symbol["name"] for symbol in item.get("symbols", [])] == expected[item["path"]]
                        for item in first["files"])
            return {"files": len(expected), "complete_declaration_files": found,
                    "expected_symbols": sum(len(names) for names in expected.values()),
                    "returned_symbols": sum(len(item.get("symbols", [])) for item in first["files"]),
                    "first_ms": round(first_ms, 3), "median_repeated_ms": round(statistics.median(timings), 3),
                    "max_repeated_ms": round(max(timings), 3), "repeated_runs": repeat,
                    "first_parse_calls": first_parses if parser is not None else None,
                    "repeated_parse_calls": len(parse_calls) - first_parses if parser is not None else None,
                    "tool_result_bytes": len(first_body), "result_sha256": hashlib.sha256(first_body).hexdigest(),
                    "results_identical": identical}
        finally:
            if parser is not None:
                parser.parse = original
            tools.close()


def git(*arguments):
    return subprocess.check_output(["git", "-C", str(REPO), *arguments])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeat", type=int, default=11)
    parser.add_argument("--compare-ref", help="trusted local Git revision to measure in an isolated source directory")
    scenario = parser.add_mutually_exclusive_group()
    scenario.add_argument("--click-fixture", type=Path, help="measure only scoped retrieval from the checksum-verified Click snapshot")
    scenario.add_argument("--edit-context", action="store_true", help="replay fixed large edits over localhost HTTP; no model/API score")
    scenario.add_argument("--scan-cache", action="store_true", help="measure complete map sweeps beyond cache bounds; no model/API score")
    scenario.add_argument("--regex-search", action="store_true", help="compare known definition windows from one regex or three literal searches; no model/API score")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", type=Path, default=REPO / "src", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 50:
        parser.error("--repeat must be from 1 to 50")
    if args.click_fixture is not None:
        args.click_fixture = args.click_fixture.resolve()
    if args.worker:
        if args.regex_search:
            result = measure_regex_search(args.source, args.repeat)
        elif args.scan_cache:
            result = measure_scan_cache(args.source, args.repeat)
        elif args.edit_context:
            result = measure_edit_context(args.source, args.repeat)
        else:
            result = (measure_click_search(args.source, args.click_fixture, args.repeat) if args.click_fixture is not None
                      else measure(args.source, args.repeat))
        print(json.dumps(result))
        return 0
    report = {"scope": ("synthetic known definition windows: regex versus fixed literal queries; no model/API or coding-task success comparison"
                        if args.regex_search else "complete synthetic map sweeps across cache entry/byte bounds; no model/API or coding-task success comparison"
                        if args.scan_cache else "scripted large edits over localhost HTTP; protocol, budget, tools and history, not model task success"
                        if args.edit_context else "pinned Click code windows; file tools only, no model/API or coding-task success comparison"
                        if args.click_fixture is not None else "offline retrieval and multilingual parsing; no model/API or coding-task success comparison"),
              "python": platform.python_version(), "platform": platform.platform(),
              "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
              "dependencies": {name: version(name) for name in (
                  "textual", "google-re2", "tree-sitter", "tree-sitter-javascript", "tree-sitter-typescript",
                  "tree-sitter-rust", "tree-sitter-go", "tree-sitter-c", "tree-sitter-cpp", "tree-sitter-java")},
              "revision": git("rev-parse", "HEAD").decode().strip(),
              "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "uv_lock_sha256": hashlib.sha256((REPO / "uv.lock").read_bytes()).hexdigest(),
              "dirty": bool(git("status", "--porcelain")), "repeat": args.repeat,
              "current": worker(REPO / "src", args.repeat, args.click_fixture, args.edit_context, args.scan_cache, args.regex_search)}
    if args.click_fixture is not None:
        report["support_sha256"] = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest() for name in (
            "scripts/benchmark-edit-repo.py", "scripts/benchmark-tasks.py", "benchmarks/coding/suite.py")}
    if args.edit_context:
        name = "benchmarks/context/replay.py"
        report["support_sha256"] = {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()}
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
            report["baseline"] = {"revision": revision, **worker(root / "src", args.repeat, args.click_fixture, args.edit_context, args.scan_cache, args.regex_search)}
        if args.scan_cache:
            current, baseline = report["current"], report["baseline"]
            for field in ("max_cache_entries", "max_cache_bytes"):
                if current[field] != baseline[field]:
                    raise ValueError("cache limits differ between revisions")
            old_cases = {case["case"]: case for case in baseline["cases"]}
            if set(old_cases) != {case["case"] for case in current["cases"]}:
                raise ValueError("cache scenarios differ between revisions")
            for case in current["cases"]:
                for field in ("fixture_sha256", "result_sha256", "source_bytes", "files", "declarations_per_file", "max_output"):
                    if case[field] != old_cases[case["case"]][field]:
                        raise ValueError("cache comparison changed inputs or returned contents")
            report["comparison_inputs_and_results_identical"] = True
        if args.regex_search:
            current, baseline = report["current"], report["baseline"]
            old_cases = {case["case"]: case for case in baseline["cases"]}
            if set(old_cases) != {case["case"] for case in current["cases"]}:
                raise ValueError("regex scenarios differ between revisions")
            for case in current["cases"]:
                for field in ("fixture_sha256", "match_windows_sha256", "source_bytes", "files", "matching_lines"):
                    if case[field] != old_cases[case["case"]][field]:
                        raise ValueError("regex comparison changed inputs or returned windows")
            report["comparison_inputs_and_match_windows_identical"] = True
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
