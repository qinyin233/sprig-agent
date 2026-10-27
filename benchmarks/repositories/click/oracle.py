"""Fixed external Click acceptance; candidate stdout and tracebacks are not exported."""

from contextlib import redirect_stderr, redirect_stdout
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import sys


class Discard(io.TextIOBase):
    def write(self, text):
        return len(text)
    def flush(self):
        pass


def verify_worker():
    if sys.version_info[:2] != (3, 12) or version("pytest") != "8.4.2":
        raise ValueError("acceptance runtime differs from the pinned environment")
    directory = Path(__file__).resolve().parent
    candidate = Path.cwd().resolve()
    source = candidate / "src"
    # The interpreter uses -I -B; do not allow candidate config or entry points
    # to select pytest plugins, tests, collection counts, or the fixed config.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for name in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DEBUG", "PYTEST_CURRENT_TEST"):
        os.environ.pop(name, None)
    class Counts:
        collected = passed = skipped = 0
        def __init__(self):
            self.failed = set()
        def pytest_collection_finish(self, session):
            self.collected = len(session.items)
        def pytest_runtest_logreport(self, report):
            if report.failed:
                self.failed.add(report.nodeid)
            elif report.skipped:
                self.skipped += 1
            elif report.when == "call" and report.passed:
                self.passed += 1
    count = Counts()
    with redirect_stdout(Discard()), redirect_stderr(Discard()):
        # Load the pinned pytest before the candidate import path is installed.
        # A candidate src/pytest.py must not replace the acceptance runner.
        import pytest
        sys.path.insert(0, str(source))
        import click
        if Path(click.__file__).resolve() != (source / "click/__init__.py").resolve():
            raise ValueError("acceptance imported the wrong candidate")
        error = getattr(click, "NoSuchCommand", None)
        api = (isinstance(error, type) and error is not click.UsageError
               and issubclass(error, click.UsageError))
        code = int(pytest.main([
            "-c", str(directory / "upstream/pyproject.toml"), "-p", "no:cacheprovider",
            "--import-mode=prepend", "-q", str(directory / "upstream/tests/test_commands.py"),
            str(directory / "upstream/tests/test_options.py")], plugins=[count]))
    passed = (code == 0 and count.collected == count.passed == 642 and count.skipped == 0
              and not count.failed and api)
    return {"schema": 1, "passed": passed, "collected": count.collected, "checks_passed": count.passed,
            "skipped": count.skipped, "failed": sorted(count.failed), "pytest_returncode": code,
            "public_exception_contract": api}


def supervise():
    directory = Path(__file__).resolve().parent
    sys.path.insert(0, str(directory / "runner"))
    from sprig_agent.config import Config
    from sprig_agent.policy import command_environment
    sys.path.insert(0, str(directory))
    from supervised_worker import run_worker
    argv = [sys.executable, "-I", "-B", str(directory / "oracle.py"), "--worker"]
    result = run_worker(argv, env=command_environment(Config(Path.cwd(), "offline-verifier")), timeout=20)
    allowed = set(json.loads((directory / "known-failures.json").read_text("utf-8")))
    return validate_result(result, allowed)


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate result field")
        value[key] = item
    return value


def validate_result(result, allowed):
    """Recompute acceptance in the supervisor; publish fixed identifiers only."""
    if (type(result.get("returncode")) is not int or result["returncode"] not in (0, 1) or result.get("timed_out")
            or result.get("output_limited") or result.get("error")):
        return {"passed": False, "reason": "candidate_execution_failed"}
    try:
        value = json.loads(result.get("output", ""), object_pairs_hook=unique_object)
        fields = {"schema", "passed", "collected", "checks_passed", "skipped", "failed",
                  "pytest_returncode", "public_exception_contract"}
        if (not isinstance(value, dict) or set(value) != fields
                or type(value["schema"]) is not int or value["schema"] != 1):
            raise ValueError
        if any(type(value[name]) is not int or not 0 <= value[name] <= 1284
               for name in ("collected", "checks_passed", "skipped", "pytest_returncode")):
            raise ValueError
        if any(type(value[name]) is not bool for name in ("passed", "public_exception_contract")):
            raise ValueError
        if (not isinstance(value["failed"], list) or len(value["failed"]) > 1284
                or any(not isinstance(name, str) or len(name) > 1024 for name in value["failed"])
                or len(set(value["failed"])) != len(value["failed"])):
            raise ValueError
        if value["pytest_returncode"] > 5:
            raise ValueError
        known = sorted(set(value["failed"]) & allowed)
        other = len(value["failed"]) - len(known)
        passed = (result["returncode"] == value["pytest_returncode"] == 0
                  and value["collected"] == value["checks_passed"] == 642
                  and value["skipped"] == 0 and not value["failed"] and value["public_exception_contract"])
        if value["passed"] != passed:
            raise ValueError
        # Never publish arbitrary candidate output or forged node IDs. The seven
        # known IDs come from fixed metadata; other regressions have a count.
        return {**{key: value[key] for key in fields - {"failed", "passed"}}, "passed": passed,
                "failed": known, "other_failures": other}
    except (ValueError, TypeError, KeyError, RecursionError):
        return {"passed": False, "reason": "candidate_result_invalid"}


def main():
    try:
        if sys.argv[1:] == ["--worker"]:
            result = verify_worker()
        elif not sys.argv[1:]:
            result = supervise()
        else:
            raise ValueError
    except KeyboardInterrupt:
        result = {"passed": False, "reason": "cancelled"}
    except Exception:
        result = {"passed": False, "reason": "acceptance_setup_failed"}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
