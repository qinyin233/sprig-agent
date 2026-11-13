"""Repository task preflights and result validation; no network or model calls."""

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


driver = load("repository_task_driver", REPO / "scripts/benchmark-repository-task.py")
oracle = load("click_task_oracle", REPO / "benchmarks/repositories/click/oracle.py")


class RepositoryTaskTests(unittest.TestCase):
    def test_invalid_output_cap_is_rejected_before_credentials_or_fixture_access(self):
        for value in ("0", "-1", "65537"):
            with self.subTest(value=value), patch.object(sys, "argv", [
                "repository-task", "--run", "--fixture", "unused", "--max-tokens", value
            ]), patch.object(driver.task_module, "ClickTask") as adapter, \
                    patch.object(driver, "environment_api_key") as key, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    driver.main()
                self.assertEqual(error.exception.code, 2)
                adapter.assert_not_called()
                key.assert_not_called()

    def test_invalid_request_timeout_is_rejected_before_fixture_or_model_access(self):
        for value in ("0", "-1", "121", "nan", "inf"):
            with self.subTest(value=value), patch.object(sys, "argv", [
                "repository-task", "--self-test", "--fixture", "unused", "--request-timeout", value
            ]), patch.object(driver.task_module, "ClickTask") as adapter, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    driver.main()
                self.assertEqual(error.exception.code, 2)
                adapter.assert_not_called()

    def test_runtime_options_reach_the_sprig_loop_and_validate_before_fixture_access(self):
        argv = ["repository-task", "--run", "--fixture", "unused", "--request-timeout", "120",
                "--command-timeout", "60", "--request-deadline", "180", "--max-steps", "12",
                "--max-tokens", "2048", "--wire-api", "responses", "--stream", "--request-usage",
                "--check-before-task", "--check-after-edit", "--compact-context",
                "--preserve-reasoning", "--recover-output-limit", "--independent-post-check"]
        with (patch.object(sys, "argv", argv), patch.object(sys, "version_info", (3, 12, 12)),
              patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True),
              patch.object(driver, "environment_api_key", return_value="fixture-private-marker"),
              patch("importlib.metadata.version", return_value="8.4.2"),
              patch.object(driver.task_module, "ClickTask", return_value=SimpleNamespace(case={})) as adapter,
              patch.object(driver.bench, "run_case", return_value={"acceptance_passed": False,
                           "independent_post_check": {"status": "passed"}}) as run, patch("builtins.print")):
            self.assertEqual(driver.main(), 1)
            adapter.assert_called_once()
            options = run.call_args.args[1]
            self.assertEqual((options["request_timeout"], options["timeout"], options["request_deadline"]), (120, 60, 180))
            self.assertEqual((options["max_steps"], options["max_tokens"], options["wire_api"]), (12, 2048, "responses"))
            for flag in ("stream", "request_usage", "check_before_task", "check_after_edit",
                         "compact_context", "preserve_reasoning", "recover_output_limit"):
                self.assertIs(options[flag], True)
            self.assertIs(run.call_args.kwargs["post_check"], driver.click_post_check)
            self.assertTrue(callable(run.call_args.kwargs["emit_delta"]))
        for flags in (["--client", "mini", "--recover-output-limit"], ["--request-deadline", "nan"],
                      ["--command-timeout", "0"], ["--request-usage"]):
            with (self.subTest(flags=flags), patch.object(sys, "argv", argv[:4] + flags),
                  patch.object(driver.task_module, "ClickTask") as adapter, patch("sys.stderr", io.StringIO()),
                  self.assertRaises(SystemExit) as stopped):
                try:
                    driver.main()
                finally:
                    adapter.assert_not_called()
            self.assertEqual(stopped.exception.code, 2)

    def test_independent_post_check_does_not_replace_the_original_outcome_or_run_model_tools(self):
        for approved in (True, False):
            asks, seen = [], []

            class TruncatedProvider:
                def __init__(self, config):
                    pass

                def complete(self, messages, tools):
                    seen.append(1)
                    raise driver.bench.ProviderError("fixture output limit", code="output_limit")

            with self.subTest(approved=approved):
                report = driver.bench.run_case(driver.bench.suite.cases()["py-interval"], {
                    "model": "fixture", "request_timeout": 120, "timeout": 60, "request_deadline": 180,
                    "max_steps": 1, "recover_output_limit": True,
                }, provider_factory=TruncatedProvider, confirm=lambda description: asks.append(description) or approved,
                    post_check=lambda result: {"status": "fixture", "approved": result.get("approved")})
                self.assertEqual(report["outcome"], "provider_error")
                self.assertFalse(report["acceptance_passed"])
                self.assertEqual(report["approvals"]["requests"], 0)
                self.assertEqual(report["independent_post_check"], {"status": "fixture", "approved": approved})
                self.assertEqual(len(asks), 1)
                self.assertEqual(len(seen), 1)
                self.assertEqual(report["tools"], [])
                self.assertEqual((report["request_timeout_seconds"], report["command_timeout_seconds"],
                                  report["request_deadline_seconds"]), (120, 60, 180))
                self.assertTrue(report["recover_output_limit"])

    def adapter(self):
        base = {"src/click/__init__.py": b"baseline-source", "tests/test_commands.py": b"initial-test"}
        reference = {"src/click/__init__.py": b"reference-implementation-marker",
                     "tests/test_commands.py": b"fixed-external-test", "pyproject.toml": b"fixed-config"}
        with patch.object(driver.task_module.fixture, "load", side_effect=[base, reference]):
            return driver.task_module.ClickTask(Path("unused"))

    def test_prepare_exports_only_initial_source_and_external_oracle(self):
        adapter = self.adapter()
        with driver.bench.disposable() as parent:
            report = driver.prepare(parent / "prepared", adapter)
            root = Path(report["workspace"])
            files = {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(files, dict(adapter.files))
            self.assertEqual(report["fixture_sha256"], driver.bench.suite.digest(files))
            self.assertEqual(report["acceptance_argv"][1:3], ["-I", "-B"])
            self.assertFalse(Path(report["acceptance_argv"][-1]).is_relative_to(root))
            self.assertFalse(any(b"reference-implementation-marker" in body for body in adapter.oracle.values()))
            self.assertNotIn("upstream/src/click/__init__.py", adapter.oracle)
            self.assertEqual(len(report["verifier_sha256"]), 64)
            with self.assertRaises(ValueError):
                driver.prepare(parent / "prepared", adapter)
            self.assertEqual(files["src/click/__init__.py"], (root / "src/click/__init__.py").read_bytes())

    def test_fixed_bytes_and_verifier_inventory_cannot_change_silently(self):
        adapter = self.adapter()
        with self.assertRaises(TypeError):
            adapter.oracle["oracle.py"] = b"changed"
        with self.assertRaises(TypeError):
            adapter.files["src/click/__init__.py"] = b"changed"
        with driver.bench.disposable() as parent:
            argv, _, intact = adapter.bundle(parent, adapter.case)
            self.assertTrue(intact())
            path = Path(argv[-1])
            body = path.read_bytes()
            path.write_bytes(b"raise SystemExit(0)")
            self.assertFalse(intact())
            path.write_bytes(body)
            self.assertTrue(intact())
            extra = path.parent / "extra.py"
            extra.write_bytes(b"")
            self.assertFalse(intact())
            extra.unlink()
            self.assertTrue(intact())
            (path.parent / "empty").mkdir()
            self.assertFalse(intact())

    def test_replaced_verifier_directory_with_identical_bytes_is_rejected(self):
        adapter = self.adapter()
        with driver.bench.disposable() as parent:
            argv, _, intact = adapter.bundle(parent, adapter.case)
            directory = Path(argv[-1]).parent
            # Both paths are inside this harness-owned disposable root.
            directory.rename(parent / "original-verification")
            driver.bench.suite.seed(directory, adapter.oracle)
            self.assertFalse(intact())

    def test_changed_task_contract_is_rejected_before_export(self):
        adapter = self.adapter()
        with driver.bench.disposable() as parent, self.assertRaises(ValueError):
            adapter.bundle(parent, {**adapter.case, "task": "different task"})
        self.assertEqual(adapter.metadata["expected_upstream_checks"], 642)

    def test_relative_prepare_destination_uses_absolute_guarded_verifier(self):
        adapter = self.adapter()
        original = Path.cwd()
        with driver.bench.disposable() as parent:
            try:
                os.chdir(parent)
                report = driver.prepare(Path("prepared"), adapter)
            finally:
                os.chdir(original)
            self.assertTrue(Path(report["acceptance_argv"][-1]).is_absolute())
            self.assertEqual(Path(report["workspace"]), parent / "prepared/workspace")

    def test_live_preflights_stop_before_fixture_loading_and_do_not_print_secrets(self):
        scenarios = ({"SPRIG_API_KEY": "", "OPENAI_API_KEY": "", "AGNES_API_KEY": ""},
                     {"SPRIG_API_KEY": "fixture-private-marker", "OPENAI_API_KEY": ""})
        for values in scenarios:
            errors = io.StringIO()
            with self.subTest(values=bool(values["SPRIG_API_KEY"])), patch.dict(os.environ, values), \
                    patch.object(sys, "argv", ["repository-task", "--run", "--fixture", "missing"]), \
                    patch.object(sys.stdin, "isatty", return_value=False), \
                    patch.object(driver.task_module, "ClickTask") as adapter, patch("sys.stderr", errors):
                with self.assertRaises(SystemExit) as stopped:
                    driver.main()
                self.assertEqual(stopped.exception.code, 2)
                adapter.assert_not_called()
            self.assertNotIn("fixture-private-marker", errors.getvalue())

    def test_existing_report_stops_before_loading_fixture_or_launching_client(self):
        with driver.bench.disposable() as parent:
            report = parent / "report.json"
            report.write_bytes(b"preserve")
            with patch.object(sys, "argv", ["repository-task", "--run", "--fixture", "missing", "--report", str(report)]), \
                    patch.object(driver.task_module, "ClickTask") as adapter, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    driver.main()
                adapter.assert_not_called()
            self.assertEqual(report.read_bytes(), b"preserve")

    def test_dependency_error_is_sanitized_and_setup_cancellation_is_preserved(self):
        for error, code in ((ImportError("fixture-private-marker"), 1), (KeyboardInterrupt(), 130)):
            errors = io.StringIO()
            with patch.object(sys, "argv", ["repository-task", "--self-test", "--fixture", "missing"]), \
                    patch.object(driver.task_module, "ClickTask", side_effect=error), patch("sys.stderr", errors):
                self.assertEqual(driver.main(), code)
            self.assertNotIn("fixture-private-marker", errors.getvalue())


class OracleResultTests(unittest.TestCase):
    def result(self, **changes):
        value = {"schema": 1, "passed": True, "collected": 642, "checks_passed": 642, "skipped": 0,
                 "failed": [], "pytest_returncode": 0, "public_exception_contract": True}
        value.update(changes)
        return {"returncode": 0, "output": json.dumps(value)}

    def test_supervisor_recomputes_counts_and_contract_instead_of_trusting_passed(self):
        self.assertTrue(oracle.validate_result(self.result(), set())["passed"])
        changes = [{"collected": 641}, {"checks_passed": 641}, {"skipped": 1}, {"failed": ["known"]},
                   {"public_exception_contract": False}, {"pytest_returncode": 1}, {"schema": True},
                   {"collected": True}, {"passed": 1}, {"failed": ["known", "known"]}]
        for change in changes:
            with self.subTest(change=change):
                self.assertFalse(oracle.validate_result(self.result(**change), {"known"})["passed"])

    def test_independent_report_only_keeps_counts_and_checks_returncode(self):
        payload = {"schema": 1, "collected": 642, "checks_passed": 638, "skipped": 0,
                   "public_exception_contract": False, "passed": False, "failed": ["private-marker"]}
        result = {"approved": True, "returncode": 1, "output": json.dumps(payload)}
        self.assertEqual(driver.click_post_check(result), {"status": "failed", "collected": 642,
                         "checks_passed": 638, "skipped": 0, "public_exception_contract": False})
        for extra in ({"output_truncated": True}, {"returncode": 0}, {"approved": None},
                      {"output": json.dumps({**payload, "checks_passed": True})}):
            self.assertEqual(driver.click_post_check({**result, **extra}), {"status": "incomplete"})
        self.assertEqual(driver.click_post_check({"approved": False}), {"status": "rejected"})

    def test_empty_zero_exit_or_bounded_child_failure_never_passes(self):
        outcomes = [{"returncode": 0, "output": ""}, {"returncode": True, "output": self.result()["output"]},
                    {**self.result(), "timed_out": True}, {**self.result(), "output_limited": True},
                    {**self.result(), "returncode": -1}, {**self.result(), "error": "private-marker"}]
        for result in outcomes:
            with self.subTest(result=result):
                checked = oracle.validate_result(result, set())
                self.assertFalse(checked["passed"])
                self.assertNotIn("private-marker", json.dumps(checked))

    def test_duplicate_json_fields_and_candidate_identifiers_are_not_exported(self):
        raw = self.result()["output"].replace('"schema": 1', '"schema": 1, "schema": 1')
        self.assertEqual(oracle.validate_result({"returncode": 0, "output": raw}, set())["reason"], "candidate_result_invalid")
        result = self.result(passed=False, checks_passed=640, failed=["known", "private-marker"], pytest_returncode=1)
        result["returncode"] = 1
        checked = oracle.validate_result(result, {"known"})
        self.assertEqual(checked["failed"], ["known"])
        self.assertEqual(checked["other_failures"], 1)
        self.assertNotIn("private-marker", json.dumps(checked))


if __name__ == "__main__":
    unittest.main()
