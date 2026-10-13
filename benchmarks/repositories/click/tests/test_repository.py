"""Actual pinned Click acceptance and Sprig loop with local scripted HTTP replies.

Run in the locked Click Python 3.12 environment with SPRIG_CLICK_FIXTURE set.
These fixtures test the protocol, never model coding ability or agent ranking.
"""

import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import unittest

REPO = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("click_repository_driver", REPO / "scripts/benchmark-repository-task.py")
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


class ClickRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("use the locked Python 3.12 repository environment")
        cls.fixture = Path(os.environ["SPRIG_CLICK_FIXTURE"]).resolve()
        cls.adapter = driver.task_module.ClickTask(cls.fixture)

    def verify_candidate(self, additions):
        with driver.bench.disposable() as parent:
            root = parent / "workspace"
            driver.bench.suite.seed(root, self.adapter.files)
            driver.bench.suite.seed(root, additions)
            argv, _, intact = self.adapter.bundle(parent, self.adapter.case)
            tools = driver.Tools(driver.Config(root, "offline", max_output=65536), lambda _: intact())
            try:
                result = tools.run(list(argv), timeout=30)
            finally:
                tools.close()
            self.assertTrue(intact())
            self.assertFalse(result.get("timed_out"), result)
            self.assertFalse(result.get("output_limited"), result)
            return json.loads(result["output"]), result["returncode"]

    def test_actual_fixed_oracle_rejects_base_and_accepts_reference_with_no_skips(self):
        report = driver.self_test(self.adapter, self.fixture)
        self.assertTrue(report["success"])
        self.assertEqual([(row["collected"], row["checks_passed"], row["skipped"]) for row in report["results"]],
                         [(642, 635, 0), (642, 642, 0)])

    def test_candidate_zero_exit_and_native_stdout_noise_are_rejected_without_export(self):
        candidates = [b"import os; os._exit(0)\n",
                      b"import os; os.write(1, b'private-marker' * 10000); os._exit(0)\n"]
        for body in candidates:
            with self.subTest(body=body[:30]):
                result, code = self.verify_candidate({"src/click/__init__.py": body})
                self.assertEqual(code, 1)
                self.assertFalse(result["passed"])
                self.assertNotIn("private-marker", json.dumps(result))

    def test_candidate_tests_config_and_pytest_shadow_do_not_change_selected_oracle(self):
        result, code = self.verify_candidate({"src/pytest.py": b"raise RuntimeError('private-marker')\n",
                                             "tests/test_commands.py": b"raise SystemExit(0)\n",
                                             "conftest.py": b"raise RuntimeError('private-marker')\n",
                                             "pyproject.toml": b"invalid candidate pytest configuration"})
        self.assertEqual(code, 1)
        self.assertEqual(result["collected"], 642)
        self.assertEqual(result["checks_passed"], 635)
        self.assertEqual(result["failed"], sorted(driver.task_module.fixture.FAILED_BEFORE))
        self.assertNotIn("private-marker", json.dumps(result))

    def test_actual_sprig_http_loop_gets_failed_acceptance_then_repairs_four_files(self):
        fixture = driver.task_module.fixture
        edits = fixture.replacements(self.adapter.files, fixture.load(self.fixture, "reference"))
        self.assertEqual(len(edits), 4)
        replies = [{"role": "assistant", "content": "Finish without changes"},
                   {"role": "assistant", "content": None, "tool_calls": [
                       {"id": "edit_" + str(number), "type": "function", "function": {"name": "edit_file",
                        "arguments": json.dumps({"path": name, "edits": chunks})}}
                       for number, (name, chunks) in enumerate(edits.items())]},
                   {"role": "assistant", "content": "Finish after fixture repair"}]
        observations = []
        case = self.adapter.case
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                index = len(observations)
                observations.append({"model": data["model"], "same_task": any(
                    message.get("content") == case["task"] for message in data["messages"]),
                    "failure_feedback": "Application acceptance checks failed" in json.dumps(data["messages"]),
                    "auth_ok": self.headers.get("Authorization") == "Bearer fixture-private-marker"})
                body = json.dumps({"choices": [{"index": 0, "finish_reason": "tool_calls" if index == 1 else "stop",
                                                "message": replies[index]}],
                                   "usage": {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        approvals = []
        try:
            report = driver.bench.run_case(case, {"model": "fixture", "api_key": "fixture-private-marker", "max_steps": 4,
                                                  "base_url": f"http://127.0.0.1:{server.server_port}/v1"},
                                          case_adapter=self.adapter, confirm=lambda text: approvals.append(text) or True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.assertTrue(report["acceptance_passed"], report)
        self.assertTrue(report["verifier_intact"])
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(row["same_task"] and row["auth_ok"] and row["model"] == "fixture" for row in observations))
        self.assertTrue(observations[1]["failure_feedback"])
        self.assertEqual(report["approvals"]["requests"], 6)
        self.assertEqual([text.split()[0] for text in approvals], ["RUN", "WRITE", "WRITE", "WRITE", "WRITE", "RUN"])
        self.assertEqual(report["fixture_sha256"], self.adapter.metadata["base_sha256"])
        self.assertEqual(report["task_sha256"], self.adapter.metadata["task_sha256"])
        self.assertEqual(report["repository_task"], self.adapter.metadata)
        self.assertNotIn("fixture-private-marker", json.dumps(report))

    def test_rejected_acceptance_leaves_task_unverified(self):
        class Finish:
            def __init__(self, config):
                pass
            def complete(self, messages, tools, on_text=None):
                return {"role": "assistant", "content": "Finish"}
        report = driver.bench.run_case(self.adapter.case, {"model": "fixture"}, case_adapter=self.adapter,
                                      provider_factory=Finish, confirm=lambda _: False)
        self.assertFalse(report["acceptance_passed"])
        self.assertTrue(report["verifier_intact"])
        self.assertEqual(report["approvals"]["rejected"], 1)


if __name__ == "__main__":
    unittest.main()
