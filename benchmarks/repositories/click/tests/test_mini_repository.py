"""Actual mini 2.4.6 loop on the same Click inputs, with scripted actions only.

Run in the locked mini Python 3.12 environment with SPRIG_CLICK_FIXTURE set.
"""

import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys
import unittest

REPO = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("mini_repository_driver", REPO / "scripts/benchmark-repository-task.py")
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class MiniRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.version_info[:2] != (3, 12):
            raise RuntimeError("use the locked Python 3.12 mini environment")
        cls.fixture = Path(os.environ["SPRIG_CLICK_FIXTURE"]).resolve()
        cls.adapter = entry.task_module.ClickTask(cls.fixture)
        cls.mini = entry.load_mini()

    def model(self, commands, observed):
        def factory(defaults):
            from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output
            outputs = []
            for index, command in enumerate(commands):
                identifier = "call_" + str(index)
                outputs.append(make_toolcall_output(None, [{"id": identifier, "type": "function", "function": {
                    "name": "bash", "arguments": json.dumps({"command": command})}}],
                    [{"command": command, "tool_call_id": identifier}]))
            class Model(DeterministicToolcallModel):
                def query(self, messages, **kwargs):
                    observed.append({"same_task": any(message.get("content") == self_adapter.case["task"]
                                                       or self_adapter.case["task"] in (message.get("content") or "")
                                                       for message in messages),
                                     "failed": "Fixed acceptance failed" in json.dumps(messages)})
                    return super().query(messages, **kwargs)
            self_adapter = self.adapter
            return Model(outputs=outputs, cost_per_call=0, observation_template=defaults["model"]["observation_template"])
        return factory

    def repair(self):
        fixture = entry.task_module.fixture
        edits = fixture.replacements(self.adapter.files, fixture.load(self.fixture, "reference"))
        payload = base64.b64encode(json.dumps(edits).encode()).decode()
        code = "import base64,json; from pathlib import Path; edits=json.loads(base64.b64decode('" + payload + "'))\n"
        code += "for name,chunks in edits.items():\n p=Path(name); text=p.read_text(encoding='utf-8')\n"
        code += " for item in chunks:\n  assert text.count(item['old'])==1; text=text.replace(item['old'],item['new'],1)\n"
        code += " p.write_text(text,encoding='utf-8')\n"
        return shlex.join([sys.executable.replace("\\", "/"), "-I", "-B", "-c", code])

    def test_upstream_loop_fails_repairs_and_passes_identical_repository_acceptance(self):
        observed = []
        marker = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
        report = self.mini.run_case(self.adapter.case, {"model": "fixture", "max_steps": 4},
                                    case_adapter=self.adapter, model_factory=self.model([marker, self.repair(), marker], observed),
                                    confirm=lambda _: True)
        self.assertTrue(report["acceptance_passed"], report)
        self.assertTrue(report["verifier_intact"])
        self.assertEqual(report["model_queries"], 3)
        self.assertEqual(report["approvals"]["requests"], 5)
        self.assertTrue(all(row["same_task"] for row in observed))
        self.assertTrue(observed[1]["failed"])
        self.assertEqual(report["fixture_sha256"], self.adapter.metadata["base_sha256"])
        self.assertEqual(report["task_sha256"], self.adapter.metadata["task_sha256"])
        self.assertEqual(report["repository_task"], self.adapter.metadata)
        self.assertEqual(report["model_mode"], "scripted_protocol_fixture")
        self.assertIsNone(report["http_attempts"])
        self.assertIn("repository mini", report["scope"])

    def test_repository_acceptance_rejection_stops_the_upstream_loop(self):
        answers = iter((True, False))
        report = self.mini.run_case(self.adapter.case, {"model": "fixture"}, case_adapter=self.adapter,
                                    model_factory=self.model(["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"], []),
                                    confirm=lambda _: next(answers))
        self.assertEqual(report["outcome"], "acceptance_rejected")
        self.assertFalse(report["acceptance_passed"])

    def test_independent_post_check_scores_exhausted_loop_and_preserves_its_result(self):
        for accepted in (True, False):
            with self.subTest(accepted=accepted):
                decisions = iter((True, accepted))
                approvals, observed = [], []

                def confirm(description):
                    approvals.append(description)
                    return next(decisions)

                report = self.mini.run_case(self.adapter.case, {"model": "fixture", "max_steps": 1},
                    case_adapter=self.adapter, model_factory=self.model(["echo observation"], observed),
                    confirm=confirm, post_check=entry.click_post_check)
                self.assertEqual(len(observed), 1)
                self.assertEqual(len(approvals), 2)
                self.assertEqual(report["outcome"], "limits_exceeded")
                self.assertFalse(report["acceptance_passed"])
                self.assertEqual(report["approvals"]["requests"], 1)
                self.assertEqual([row["tool"] for row in report["tools"]], ["bash"])
                expected = ({"status": "failed", "collected": 642, "checks_passed": 635, "skipped": 0,
                             "public_exception_contract": False} if accepted else {"status": "rejected"})
                self.assertEqual(report["independent_post_check"], expected)


if __name__ == "__main__":
    unittest.main()
