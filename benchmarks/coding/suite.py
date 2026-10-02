"""Reproducible diagnostic fixtures and an external output oracle, not a sandbox."""

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from sprig_agent.config import Config
from sprig_agent.tools import Tools

CASE_FILE = Path(__file__).with_name("cases.json")

PY_WORKER = '''
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
arguments = json.loads(sys.argv[4])
module = importlib.import_module(sys.argv[2])
function = getattr(module, sys.argv[3])
results = []
for args in arguments:
    try:
        value = function(*args)
        results.append({"value": value, "args_after": args})
    except Exception as error:
        kind = "ValueError" if isinstance(error, ValueError) else "other"
        results.append({"error": kind, "args_after": args})
sys.stdout.buffer.write(json.dumps(results, allow_nan=False).encode("utf-8"))
'''

JS_WORKER = '''
const path = require('node:path');
(async () => {
    const args = JSON.parse(process.argv[4]);
    const module = require(path.join(process.argv[1], process.argv[2]));
    const results = [];
    for (const values of args) {
        try {
            results.push({value: await module[process.argv[3]](...values), args_after: values});
        } catch (_) {
            results.push({error: 'other', args_after: values});
        }
    }
    process.stdout.write(JSON.stringify(results));
})().catch(() => { process.exitCode = 1; });
'''


def cases():
    data = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    if data.get("schema") != 1 or not isinstance(data.get("cases"), list):
        raise ValueError("unsupported benchmark manifest")
    return {case["id"]: case for case in data["cases"]}


def digest(files):
    result = hashlib.sha256()
    for name, body in sorted(files.items()):
        result.update(name.encode("utf-8") + b"\0" + body)
    return result.hexdigest()


def fixture_files(case, reference=False):
    files = dict(case["files"])
    if reference:
        files.update(case["reference"])
    padding = "# documentation padding for a source module\n" * case.get("padding_lines", 0)
    if padding:
        name = next(iter(files))
        files[name] = padding + files[name]
    files["TASK.md"] = case["task"] + "\n"
    if case.get("history_reads"):
        files["notes.txt"] = "# previous documentation\n" * 300
    return {name: text.encode("utf-8") for name, text in files.items()}


def seed(root, files):
    for name, body in files.items():
        path = PurePosixPath(name)
        if path.is_absolute() or any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in path.parts):
            raise ValueError("unsafe fixture path")
        target = root.joinpath(*path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


def synthetic_history(case):
    if not case.get("history_reads"):
        return None
    history = [{"role": "system", "content": "replaced by Agent"},
               {"role": "user", "content": "Preserve the public function names while working on this project."}]
    for number in range(case["history_reads"]):
        identifier = f"fixture-read-{number}"
        history.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": identifier, "type": "function", "function": {"name": "read_file", "arguments": '{"path":"notes.txt"}'}}]})
        history.append({"role": "tool", "tool_call_id": identifier, "content": json.dumps({
            "content": "# previous documentation\n" * 300, "truncated": False})})
    return history


def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))


def verify(case, root):
    """Keep the oracle in this process; execute candidate code in a bounded child."""
    payload = json.dumps([check["args"] for check in case["checks"]], ensure_ascii=True)
    common = [str(root.resolve()), case["module"], case["function"], payload]
    if case["language"] == "python":
        argv = [sys.executable, "-I", "-B", "-c", PY_WORKER, *common]
    else:
        node = shutil.which("node")
        if node is None:
            return {"passed": False, "reason": "runtime_missing", "checks": len(case["checks"])}
        argv = [node, "-e", JS_WORKER, *common]
    # This is a fixed child of the explicitly approved acceptance command, not
    # a model-selectable command or an automatic approval option for the agent.
    config = Config(root, "fixture-verifier", timeout=10, max_output=65536)
    tools = Tools(config, lambda _: True)
    try:
        result = tools.run(argv)
    finally:
        tools.close()
    if result.get("returncode") != 0 or result.get("timed_out") or result.get("output_limited") or "error" in result:
        return {"passed": False, "reason": "candidate_execution_failed", "checks": len(case["checks"])}
    try:
        rows = json.loads(result["output"])
        if not isinstance(rows, list) or len(rows) != len(case["checks"]):
            raise ValueError
        failed = []
        for number, (row, check) in enumerate(zip(rows, case["checks"])):
            expected = {key: check[key] for key in ("value", "error") if key in check}
            expected["args_after"] = check["args"]
            if canonical(row) != canonical(expected):
                failed.append(number)
        return {"passed": not failed, "reason": "checked_outputs", "checks": len(rows), "failed_checks": failed}
    except (ValueError, TypeError, KeyError, RecursionError):
        return {"passed": False, "reason": "invalid_candidate_output", "checks": len(case["checks"])}


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=cases())
    args = parser.parse_args()
    result = verify(cases()[args.case], Path.cwd())
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
