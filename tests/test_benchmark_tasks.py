"""Coding-oracle coverage and live-run boundaries, without remote model requests."""

import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("task_benchmark", REPO / "scripts" / "benchmark-tasks.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)
suite = benchmark.suite


class BenchmarkTests(unittest.TestCase):
    def test_all_baselines_are_rejected_and_all_references_pass_on_native_runtimes(self):
        self.assertIsNotNone(shutil.which("node"), "Node is required: JavaScript skips are not passes")
        report = benchmark.self_test()
        self.assertTrue(report["success"], report)
        self.assertEqual(len(report["cases"]), 8)
        self.assertEqual(sum(row["checks"] for row in report["cases"]), 37)

    def test_zero_exit_code_missing_output_type_confusion_and_mutation_cannot_pass(self):
        case = suite.cases()["py-interval"]
        candidates = ["import os; os._exit(0)\n", "def contains(*args): return 1\n"]
        for candidate in candidates:
            with self.subTest(candidate=candidate), benchmark.disposable() as parent:
                root = parent / "workspace"
                root.mkdir()
                suite.seed(root, {"interval.py": candidate.encode()})
                self.assertFalse(suite.verify(case, root)["passed"])
        case = suite.cases()["py-long-history"]
        with benchmark.disposable() as parent:
            root = parent / "workspace"
            root.mkdir()
            suite.seed(root, {"dedupe.py": b"def dedupe(items):\n    result = list(dict.fromkeys(items))\n    items.clear()\n    return result\n"})
            self.assertFalse(suite.verify(case, root)["passed"])

    def test_candidate_output_is_bounded_and_failed_output_is_not_republished(self):
        case = suite.cases()["py-interval"]
        with benchmark.disposable() as parent:
            root = parent / "workspace"
            root.mkdir()
            suite.seed(root, {"interval.py": b"print('private-marker' * 100000)\ndef contains(*args): return False\n"})
            report = suite.verify(case, root)
            self.assertFalse(report["passed"])
            self.assertEqual(report["reason"], "candidate_execution_failed")
            self.assertNotIn("private-marker", json.dumps(report))

    def test_prepare_only_exports_new_initial_project_and_matching_source_digest(self):
        case = suite.cases()["py-pagination"]
        with benchmark.disposable() as parent:
            target = parent / "prepared"
            report = benchmark.prepare(target, case)
            files = {file.relative_to(target / "workspace").as_posix(): file.read_bytes()
                     for file in (target / "workspace").rglob("*") if file.is_file()}
            self.assertEqual(suite.digest(files), report["fixture_sha256"])
            self.assertEqual(files, suite.fixture_files(case))
            self.assertNotIn(case["reference"]["service.py"].encode(), files.values())
            self.assertTrue(Path(report["acceptance_argv"][2]).is_relative_to(target / "verification"))
            with self.assertRaises(FileExistsError):
                benchmark.prepare(target, case)

    def test_verifier_inventory_detects_changes_new_files_and_new_directories(self):
        case = suite.cases()["py-interval"]
        with benchmark.disposable() as parent:
            argv, signature, intact = benchmark.bundle(parent, case)
            self.assertTrue(intact())
            self.assertEqual(len(signature), 64)
            source = Path(argv[2])
            original = source.read_bytes()
            source.write_bytes(b"raise SystemExit(0)")
            self.assertFalse(intact())
            source.write_bytes(original)
            self.assertTrue(intact())
            shadow = source.parent / "extra.py"
            shadow.write_text("", encoding="utf-8")
            self.assertFalse(intact())
            shadow.unlink()
            empty = source.parent / "unrecognized"
            empty.mkdir()
            self.assertFalse(intact())

    def test_disposable_cleanup_does_not_suppress_cancellation_when_root_was_removed(self):
        with self.assertRaises(KeyboardInterrupt):
            with benchmark.disposable() as parent:
                shutil.rmtree(parent)
                raise KeyboardInterrupt

    def test_scripted_task_retains_fresh_approvals_and_fixed_external_acceptance(self):
        case = suite.cases()["py-interval"]
        asks = []
        baseline_seen = []

        class ScriptedProvider:
            def __init__(self, config):
                self.config, self.calls = config, 0

            def complete(self, messages, tools, on_text=None):
                self.calls += 1
                if self.calls == 1:
                    if self.config.check_before_task:
                        baseline_seen.append([json.loads(message["content"])["returncode"] for message in messages
                                              if message.get("tool_call_id", "").startswith("sprig-baseline-")])
                    return {"role": "assistant", "content": None, "tool_calls": [{"id": "edit", "type": "function", "function": {
                        "name": "edit_file", "arguments": json.dumps({"path": "interval.py", "old": case["files"]["interval.py"],
                                                                     "new": case["reference"]["interval.py"]})}}]}
                return {"role": "assistant", "content": "Done"}

        options = {"model": "offline", "base_url": "http://localhost/v1", "max_steps": 3}
        report = benchmark.run_case(case, options, provider_factory=ScriptedProvider,
                                    confirm=lambda description: asks.append(description) or True)
        self.assertTrue(report["acceptance_passed"], report)
        self.assertTrue(report["verifier_intact"])
        self.assertEqual(report["approvals"]["requests"], 2)
        self.assertTrue(asks[0].startswith("WRITE"))
        self.assertTrue(asks[1].startswith("RUN"))
        self.assertEqual([row["tool"] for row in report["tools"]], ["edit_file", "run"])
        self.assertTrue(all(row["result_bytes"] is not None for row in report["tools"]))
        self.assertTrue(all(not row["diagnostics"]["error"] for row in report["tools"]))
        self.assertTrue(all(row["diagnostics"]["approved"] for row in report["tools"]))
        self.assertEqual(report["tool_diagnostics"]["recent_inspection_limit"], 128)
        self.assertLessEqual(report["active_ms"], report["elapsed_ms"])
        self.assertFalse(report["requests"]["available"])  # A fake provider never supplies real request usage.
        rejected = benchmark.run_case(case, options, provider_factory=ScriptedProvider, confirm=lambda _: False)
        self.assertFalse(rejected["acceptance_passed"])
        self.assertEqual(rejected["outcome"], "not_completed")
        self.assertEqual(rejected["approvals"]["accepted"], 0)
        self.assertFalse(rejected["tools"][0]["diagnostics"]["approved"])
        with_baseline = benchmark.run_case(case, {**options, "check_before_task": True},
                                          provider_factory=ScriptedProvider, confirm=lambda _: True)
        self.assertTrue(with_baseline["acceptance_passed"])
        self.assertTrue(with_baseline["check_before_task"])
        self.assertEqual(with_baseline["baseline_status"], "failed")
        self.assertEqual(with_baseline["approvals"]["requests"], 3)
        self.assertEqual([row["tool"] for row in with_baseline["tools"]], ["run", "edit_file", "run"])
        self.assertEqual(len(baseline_seen), 1)
        self.assertNotEqual(baseline_seen[0][0], 0)

    def test_live_cli_requires_credentials_and_interactive_approvals_before_running(self):
        with patch.dict(os.environ, {"SPRIG_API_KEY": "", "OPENAI_API_KEY": "", "AGNES_API_KEY": ""}), \
                patch.object(sys, "argv", ["benchmark-tasks", "--run", "--case", "py-interval"]), \
                patch.object(benchmark, "run_case") as run, patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                benchmark.main()
            self.assertEqual(error.exception.code, 2)
            run.assert_not_called()
        with patch.dict(os.environ, {"SPRIG_API_KEY": "fake-secret"}), \
                patch.object(sys, "argv", ["benchmark-tasks", "--run", "--case", "py-interval"]), \
                patch.object(sys.stdin, "isatty", return_value=False), \
                patch.object(benchmark, "run_case") as run, patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                benchmark.main()
            run.assert_not_called()

    def test_batch_preflight_keeps_private_previews_and_fresh_write_approvals(self):
        case = suite.cases()["py-pagination"]
        for mode in ("accepted", "rejected", "invalid"):
            with self.subTest(mode=mode):
                asks, observed, received = [], [], []

                class BatchProvider:
                    def __init__(self, config):
                        self.config, self.calls = config, 0

                    def complete(self, messages, tools, on_text=None):
                        self.calls += 1
                        if self.calls == 1:
                            calls = []
                            for index, name in enumerate(case["files"]):
                                old = "missing-private-edit-marker" if mode == "invalid" and index == 1 else case["files"][name]
                                calls.append({"id": str(index), "type": "function", "function": {
                                    "name": "edit_file", "arguments": json.dumps({"path": name, "old": old,
                                                                                 "new": case["reference"][name]})}})
                            return {"role": "assistant", "content": None, "tool_calls": calls}
                        observed.append({name: (self.config.workdir / name).read_text(encoding="utf-8")
                                         for name in case["files"]})
                        received.extend(json.loads(message["content"]) for message in messages
                                        if message.get("role") == "tool")
                        return {"role": "assistant", "content": "Done"}

                report = benchmark.run_case(case, {"model": "offline", "max_steps": 2},
                                            provider_factory=BatchProvider,
                                            confirm=lambda prompt: asks.append(prompt) or mode == "accepted")
                self.assertEqual(report["acceptance_passed"], mode == "accepted", report)
                self.assertEqual(observed, [case["reference"] if mode == "accepted" else case["files"]])
                previews = report["edit_preflight"]
                self.assertEqual(len(previews), 2)
                self.assertEqual([row["ready"] for row in previews], [True, mode != "invalid"])
                self.assertTrue(all(set(row) == {"tool", "active_ms", "ready"} and row["active_ms"] >= 0 for row in previews))
                self.assertTrue(all(type(row["result_bytes"]) is int for row in report["tools"]))
                self.assertEqual([prompt.split()[0] for prompt in asks],
                                 ["WRITE", "WRITE", "RUN"] if mode == "accepted" else
                                 ["WRITE", "RUN"] if mode == "rejected" else ["RUN"])
                if mode != "accepted":
                    self.assertTrue(any(row.get("skipped") for row in received))
                encoded = json.dumps(report)
                self.assertNotIn("missing-private-edit-marker", encoded)
                for field in ("revision", "before", "after", "rules", "parts"):
                    self.assertTrue(all(field not in row for row in previews))

    def test_preflight_verifier_change_blocks_writes_and_invalidates_the_run(self):
        case = suite.cases()["py-pagination"]
        asks, observed = [], []

        class BatchProvider:
            def __init__(self, config):
                self.config, self.calls = config, 0

            def complete(self, messages, tools, on_text=None):
                self.calls += 1
                if self.calls == 1:
                    return {"role": "assistant", "content": None, "tool_calls": [
                        {"id": str(index), "type": "function", "function": {"name": "edit_file", "arguments": json.dumps({
                            "path": name, "old": case["files"][name], "new": case["reference"][name]})}}
                        for index, name in enumerate(case["files"])]}
                observed.append({name: (self.config.workdir / name).read_text(encoding="utf-8") for name in case["files"]})
                return {"role": "assistant", "content": "Done"}

        original = benchmark.Tools.edit_file
        def tampered_preview(tools, **arguments):
            result = original(tools, **arguments)
            if tools._edit_preview:
                verifier = tools.config.workdir.parent / "verification/benchmarks/coding/suite.py"
                verifier.write_bytes(verifier.read_bytes() + b"\n# changed\n")
            return result

        with patch.object(benchmark.Tools, "edit_file", tampered_preview):
            report = benchmark.run_case(case, {"model": "offline", "max_steps": 2}, provider_factory=BatchProvider,
                                        confirm=lambda text: asks.append(text) or True)
        self.assertEqual(asks, [])
        self.assertEqual(observed, [case["files"]])
        self.assertFalse(report["verifier_intact"])
        self.assertFalse(report["acceptance_passed"])
        self.assertEqual(report["outcome"], "verifier_modified")
        self.assertEqual([row["ready"] for row in report["edit_preflight"]], [False])

    def test_benchmark_requires_literal_true_before_baseline_or_model_contact(self):
        class Truthy:
            def __bool__(self):
                raise AssertionError("approval must not coerce callback values")

        class UnreachableProvider:
            def __init__(self, config):
                pass

            def complete(self, *args, **kwargs):
                raise AssertionError("unapproved baseline must stop before model contact")

        for value in (1, "yes", [True], Truthy(), False, None):
            with self.subTest(kind=type(value).__name__):
                report = benchmark.run_case(suite.cases()["py-interval"],
                                            {"model": "offline", "max_steps": 1, "check_before_task": True},
                                            provider_factory=UnreachableProvider, confirm=lambda _: value)
                self.assertFalse(report["acceptance_passed"])
                self.assertEqual(report["approvals"]["accepted"], 0)
                self.assertEqual(report["approvals"]["rejected"], 1)
                self.assertEqual(report["baseline_status"], "blocked")
                self.assertFalse(report["tools"][0]["diagnostics"]["approved"])

    def test_invalid_request_controls_stop_before_key_or_fixture_access(self):
        for option, value, parser_error in (("--request-interval", "nan", False),
                                            ("--request-interval", "61", False),
                                            ("--request-timeout", "0", True),
                                            ("--request-timeout", "inf", True)):
            with self.subTest(option=option, value=value), patch.object(sys, "argv", [
                "benchmark-tasks", "--run", "--case", "py-pagination", option, value]), \
                    patch.object(benchmark, "environment_api_key") as key, \
                    patch.object(benchmark, "run_case") as run, patch("sys.stderr", io.StringIO()):
                if parser_error:
                    with self.assertRaises(SystemExit):
                        benchmark.main()
                else:
                    self.assertEqual(benchmark.main(), 1)
                key.assert_not_called()
                run.assert_not_called()

    def test_actual_search_diagnostics_count_misses_repeats_and_regex_without_query_text(self):
        class Searches:
            def __init__(self, config):
                self.calls = 0
            def complete(self, messages, tools, on_text=None):
                self.calls += 1
                if self.calls > 1:
                    return {"role": "assistant", "content": "Stopped"}
                queries = [("private-query|contains", False), ("private-query|contains", False),
                           ("private-query|contains", True)]
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": str(i), "type": "function", "function": {"name": "search", "arguments": json.dumps({
                        "path": "interval.py", "query": query, "regex": regex})}}
                    for i, (query, regex) in enumerate(queries)]}
        report = benchmark.run_case(suite.cases()["py-interval"], {"model": "offline", "max_steps": 2},
                                    provider_factory=Searches, confirm=lambda _: False)
        records = [row["diagnostics"] for row in report["tools"] if row["tool"] == "search"]
        self.assertEqual([row["matched_lines"] for row in records], [0, 0, 1])
        self.assertEqual([row["same_output_repeats"] for row in records], [0, 1, 0])
        self.assertEqual([row["regex"] for row in records], [False, False, True])
        self.assertTrue(all(row["files_read"] == 1 and not row["truncated"] for row in records))
        self.assertNotIn("private-query", json.dumps(report))

    def test_existing_report_is_rejected_before_any_live_request(self):
        with benchmark.disposable() as parent:
            report = parent / "report.json"
            report.write_text("preserve", encoding="utf-8")
            with patch.object(sys, "argv", ["benchmark-tasks", "--run", "--case", "py-interval", "--report", str(report)]), \
                    patch.object(benchmark, "run_case") as run, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    benchmark.main()
                run.assert_not_called()
            self.assertEqual(report.read_text(encoding="utf-8"), "preserve")

    def test_missing_report_directory_is_rejected_before_any_live_request(self):
        with benchmark.disposable() as parent:
            with patch.object(sys, "argv", ["benchmark-tasks", "--run", "--case", "py-interval", "--report", str(parent / "missing" / "run.json")]), \
                    patch.object(benchmark, "run_case") as run, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    benchmark.main()
                run.assert_not_called()

    def test_script_entry_point_runs_with_offline_provider_and_reports_provenance(self):
        class OfflineProvider:
            def __init__(self, config):
                pass

            def complete(self, messages, tools, on_text=None):
                return {"role": "assistant", "content": "Nothing changed"}

        output = io.StringIO()
        with patch.object(sys, "argv", ["benchmark-tasks", "--run", "--case", "py-interval", "--max-steps", "1"]), \
                patch.dict(os.environ, {"SPRIG_API_KEY": "fake-secret"}), \
                patch.object(sys.stdin, "isatty", return_value=True), \
                patch.object(sys.stdout, "isatty", return_value=True), \
                patch("sprig_agent.provider.Provider", OfflineProvider), \
                patch("sprig_agent.cli.approve", return_value=False), \
                patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
            output.isatty = lambda: True
            with self.assertRaises(SystemExit) as error:
                runpy.run_path(str(REPO / "scripts" / "benchmark-tasks.py"), run_name="__main__")
            self.assertEqual(error.exception.code, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["outcome"], "not_completed")
        self.assertEqual(len(report["driver_sha256"]), 64)
        self.assertEqual(report["approvals"]["rejected"], 1)
        self.assertNotIn("fake-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
