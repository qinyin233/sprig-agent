"""External, offline Requests acceptance. No candidate text leaves the supervisor."""

from concurrent.futures import ProcessPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import multiprocessing
import os
from pathlib import Path
import pickle
import sys

DOCUMENTS = (
    ("Extra data", '{"responseCode":["706"],"data":null}{"responseCode":["706"],"data":null}', 36),
    ("Invalid document", '{"汉字": 1}\nextra', 10),
    ("Expecting value", "[", 1),
)
IDS = tuple([f"pickle:p{protocol}:doc{index}" for protocol in range(6) for index in range(3)] + [
    "upstream:regression", "copy:copy", "copy:deepcopy", "pool:return", "pool:raise", "json:inheritance",
    "json:valid:0", "json:valid:1", "json:valid:2", "pickle:request-exception", "json:invalid"])
FAILED_BEFORE = tuple(sorted(name for name in IDS if name.startswith(("pickle:p", "upstream:", "copy:", "pool:"))))


class Discard(io.TextIOBase):
    def write(self, value):
        return len(value)
    def flush(self):
        pass


def bootstrap(backend):
    global requests
    if sys.version_info[:2] != (3, 12) or backend not in {"stdlib", "simplejson"}:
        raise ValueError("acceptance runtime or backend differs from the fixed contract")
    # Load the selected decoder before installing candidate import paths.
    if backend == "stdlib":
        sys.modules["simplejson"] = None
    else:
        import simplejson
    source = Path.cwd().resolve() / "src"
    sys.path.insert(0, str(source))
    import requests
    if Path(requests.__file__).resolve() != source / "requests/__init__.py":
        raise ValueError("wrong candidate package imported")
    if requests.compat.has_simplejson != (backend == "simplejson"):
        raise ValueError("wrong JSON backend selected")


def error(index=0):
    return requests.exceptions.JSONDecodeError(*DOCUMENTS[index])


def equivalent(before, after):
    assert type(before) is type(after)
    for field in ("msg", "doc", "pos", "lineno", "colno", "args"):
        assert getattr(before, field) == getattr(after, field)
    assert str(before) == str(after) and repr(before) == repr(after)


def pool_work(kind):
    if kind == "ok":
        return {"ok": True}
    if kind == "return":
        return error()
    response = requests.Response()
    response._content = DOCUMENTS[0][1].encode("utf-8")
    response.encoding = "utf-8"
    return response.json()


def pool_check(raise_error):
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as executor:
        future = executor.submit(pool_work, "raise" if raise_error else "return")
        if raise_error:
            try:
                future.result(timeout=5)
            except requests.exceptions.JSONDecodeError as caught:
                assert caught.doc == DOCUMENTS[0][1] and caught.pos == 36
            else:
                raise AssertionError("expected JSON exception")
        else:
            equivalent(error(), future.result(timeout=5))
        assert executor.submit(pool_work, "ok").result(timeout=5) == {"ok": True}


def verify_worker(backend):
    bootstrap(backend)
    if any(os.environ.get(name) for name in ("SPRIG_API_KEY", "AGNES_API_KEY", "USTC_API_KEY", "OPENAI_API_KEY")):
        raise ValueError("model credential leaked into the acceptance worker")
    checks = []
    for protocol in range(6):
        for index in range(3):
            def check(protocol=protocol, index=index):
                original = error(index)
                equivalent(original, pickle.loads(pickle.dumps(original, protocol=protocol)))
            checks.append((f"pickle:p{protocol}:doc{index}", check))
    namespace = {"requests": requests, "pickle": pickle}
    regression = Path(__file__).with_name("upstream-regression.py").read_bytes()
    exec(compile(regression, "upstream-regression.py", "exec"), namespace)
    checks.append(("upstream:regression", namespace["test_json_decode_errors_are_serializable_deserializable"]))
    for name in ("copy", "deepcopy"):
        def check(name=name):
            original = error(1)
            equivalent(original, getattr(copy, name)(original))
        checks.append(("copy:" + name, check))
    checks += [("pool:return", lambda: pool_check(False)), ("pool:raise", lambda: pool_check(True))]
    def inheritance():
        value = error()
        assert isinstance(value, requests.exceptions.RequestException)
        assert isinstance(value, requests.compat.JSONDecodeError)
        assert isinstance(value, (ValueError,)) and isinstance(value, OSError)
    checks.append(("json:inheritance", inheritance))
    for index, (body, expected) in enumerate(((b'{"ok":true}', {"ok": True}), (b"[1,2]", [1, 2]), (b"null", None))):
        def check(body=body, expected=expected):
            response = requests.Response()
            response._content, response.encoding = body, "utf-8"
            assert response.json() == expected
        checks.append(("json:valid:" + str(index), check))
    def ordinary_exception():
        original = requests.exceptions.RequestException("fixture")
        restored = pickle.loads(pickle.dumps(original))
        assert type(restored) is type(original) and restored.args == original.args
    checks.append(("pickle:request-exception", ordinary_exception))
    def invalid_json():
        response = requests.Response()
        response._content = b"{oops}"
        try:
            response.json()
        except requests.exceptions.JSONDecodeError:
            pass
        else:
            raise AssertionError("expected JSON exception")
    checks.append(("json:invalid", invalid_json))
    assert tuple(name for name, _ in checks) == IDS and len(IDS) == 29
    failed = []
    with redirect_stdout(Discard()), redirect_stderr(Discard()):
        for name, callback in checks:
            try:
                callback()
            except BaseException:
                failed.append(name)
    return {"schema": 1, "backend": backend, "collected": len(checks), "checks_passed": len(checks) - len(failed),
            "failed": sorted(failed), "skipped": 0, "passed": not failed}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate result key")
        result[key] = value
    return result


def validate_worker(result, backend):
    if (type(result.get("returncode")) is not int or result["returncode"] not in (0, 1)
            or any(result.get(name) for name in ("error", "timed_out", "output_limited", "output_truncated", "outcome"))):
        raise ValueError("acceptance worker did not complete")
    data = json.loads(result.get("output", ""), object_pairs_hook=unique_object)
    if (not isinstance(data, dict) or set(data) != {"schema", "backend", "collected", "checks_passed", "failed", "skipped", "passed"}
            or data["schema"] != 1 or type(data["schema"]) is not int or data["backend"] != backend
            or type(data["collected"]) is not int or data["collected"] != 29
            or type(data["checks_passed"]) is not int or not 0 <= data["checks_passed"] <= 29
            or type(data["skipped"]) is not int or data["skipped"] != 0 or type(data["passed"]) is not bool
            or not isinstance(data["failed"], list) or any(not isinstance(name, str) or name not in IDS for name in data["failed"])
            or data["failed"] != sorted(set(data["failed"])) or data["checks_passed"] + len(data["failed"]) != 29
            or data["passed"] != (not data["failed"]) or result["returncode"] != (0 if data["passed"] else 1)):
        raise ValueError("invalid acceptance worker result")
    return data


def supervise():
    directory = Path(__file__).resolve().parent
    sys.path.insert(0, str(directory / "runner"))
    from sprig_agent.config import Config
    from sprig_agent.policy import command_environment
    sys.path.insert(0, str(directory))
    from supervised_worker import run_worker
    environment = command_environment(Config(Path.cwd(), "offline-verifier"))
    rows = []
    for backend in ("stdlib", "simplejson"):
        result = run_worker([sys.executable, "-I", "-B", str(directory / "oracle.py"), "--worker", backend], env=environment, timeout=15)
        rows.append(validate_worker(result, backend))
    return {"schema": 1, "valid": True, "passed": all(row["passed"] for row in rows),
            "collected": sum(row["collected"] for row in rows), "checks_passed": sum(row["checks_passed"] for row in rows),
            "skipped": 0, "backends": rows}


# Spawned pool workers import this protected file as __mp_main__. They need
# the same candidate/backend bootstrap, but must not recursively run the suite.
if __name__ == "__mp_main__":
    bootstrap(sys.argv[2])

if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--worker":
            report = verify_worker(sys.argv[2])
        elif len(sys.argv) == 1:
            report = supervise()
        else:
            raise ValueError("invalid verifier arguments")
    except BaseException:
        report = {"schema": 1, "valid": False, "passed": False}
    print(json.dumps(report, sort_keys=True))
    raise SystemExit(0 if report["passed"] else 1)
