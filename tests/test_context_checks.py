"""Bounded fixed-check history retains current evidence and every approval boundary."""
from contextlib import closing
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body, request_size
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.tools import TOOL_SCHEMAS, Tools
from tests.test_named_reads import call
from tests.test_planning import replies
from tests.test_context_folding import projected_inspections


class FixedCheckContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.argv = [sys.executable, "-I", "-B", "check.py", "x" * 300]
        self.config = Config(self.root, "fixture", checks=(tuple(self.argv),), max_context_bytes=32768)

    def result(self, code=1, **overrides):
        return {"approved": True, "returncode": code, "output": "failure details " + "x" * 4000,
                "timed_out": False, "output_limited": False, **overrides}

    def append(self, history, identity, name, args, result):
        history.extend([call(identity, name, args),
            {"role": "tool", "tool_call_id": identity, "content": json.dumps(result)}])

    def history(self, newer=None, args=None, name="run"):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "preserve all user constraints"}]
        self.append(history, "old", "run", {"argv": self.argv}, self.result())
        self.append(history, "new", name, {"argv": self.argv} if args is None else args,
                    self.result() if newer is None else newer)
        history.extend({"role": "user", "content": f"constraint {number}: do not delete"} for number in range(6))
        return history

    def compact(self, history, config=None):
        return prepare_context(config or self.config, history, TOOL_SCHEMAS, compact=True, force=True)

    def test_older_failure_keeps_status_and_refers_to_retained_newer_failure_without_rewriting_history(self):
        history = self.history(self.result(output="different latest failure " + "y" * 4000))
        original = copy.deepcopy(history)
        selected, report = self.compact(history)
        old = json.loads(selected[3]["content"])
        self.assertEqual((old["approved"], old["returncode"], old["check_ref"]), (True, 1, "new"))
        self.assertTrue(old["context_elided"])
        self.assertNotIn("output", old)
        marker = json.loads(selected[2]["tool_calls"][0]["function"]["arguments"])
        self.assertTrue(marker["context_elided"])
        self.assertIn("not runnable", marker["note"])
        self.assertNotIn("argv", marker)
        self.assertEqual(selected[4:6], history[4:6])
        self.assertEqual(selected[-6:], history[-6:])
        self.assertEqual(history, original)
        self.assertEqual(validate_history(selected), selected)
        self.assertEqual(report["after_bytes"], request_size(self.config, selected, TOOL_SCHEMAS))

    def test_latest_success_keeps_its_status_and_existing_output_policy_while_prior_failure_stays_a_failure(self):
        history = self.history(self.result(0, output="passed " + "p" * 4000))
        selected, _ = self.compact(history)
        current = json.loads(selected[5]["content"])
        self.assertEqual((current["returncode"], current["approved"]), (0, True))
        self.assertTrue(current["context_elided"])
        self.assertNotIn("output", current)
        self.assertEqual(json.loads(selected[3]["content"])["returncode"], 1)
        disabled, _ = prepare_context(self.config, history, TOOL_SCHEMAS, compact=False)
        self.assertEqual(disabled, history)

    def test_refused_incomplete_or_malformed_newer_checks_never_supersede_a_completed_observation(self):
        cases = [self.result(approved=False), self.result(timed_out=True), self.result(output_limited=True),
            self.result(cancelled=True), self.result(skipped=True), self.result(interrupted=True),
            self.result(outcome="unknown"), self.result(returncode=-9), self.result(returncode=True),
            self.result(error="failure"), self.result(output={"unknown": True}), self.result(approved=None),
            self.result(timed_out=0), self.result(context_elided=True)]
        for newer in cases:
            with self.subTest(newer=list(newer)):
                history = self.history(newer)
                selected, _ = self.compact(history)
                self.assertEqual(selected[2:4], history[2:4])
                if newer.get("approved") is False or newer.get("timed_out") or "error" in newer:
                    self.assertEqual(selected[5], history[5])

    def test_only_the_exact_configured_default_cwd_argv_qualifies_not_a_prefix_or_extra_arguments(self):
        variants = [{"argv": self.argv[:-1]}, {"argv": self.argv, "cwd": "."},
                    {"argv": self.argv, "timeout": 5}, {"argv": "not an argv"},
                    {"argv": [True]}, {"argv": self.argv, "extra": "not allowed"}]
        for args in variants:
            history = self.history(args=args)
            selected, _ = self.compact(history)
            self.assertEqual(selected[2:4], history[2:4])
        history = self.history(name="read_file")
        selected, _ = self.compact(history)
        self.assertEqual(selected[2:4], history[2:4])
        selected, _ = self.compact(self.history(), replace(self.config, checks=()))
        self.assertIn("output", json.loads(selected[3]["content"]))

    def test_duplicate_ids_cannot_hide_results_or_qualify_as_a_newer_observation(self):
        history = self.history(self.result(0))
        history[4]["tool_calls"][0]["id"] = "old"
        history[5]["tool_call_id"] = "old"
        selected, _ = self.compact(history)
        self.assertEqual(selected, history)

    def test_discarded_read_body_drops_provenance_and_line_metadata_but_keeps_coverage_and_recent_reads(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        read = {"path": "file.py", "content": "value = 1\n" * 1000, "sha256": "a" * 64,
                "read_id": "r0123456789ab-1", "offset": 1, "end_line": 1000, "total_lines": 2000,
                "next_offset": 1001, "line_numbers": True, "range_complete": True,
                "truncated": True, "line_truncated": False}
        for number in range(5):
            self.append(history, str(number), "read_file", {"path": "file.py", "offset": 1, "limit": 1000}, read)
        selected, _ = self.compact(history)
        old = projected_inspections(selected)["0"][2]
        self.assertTrue(old["context_elided"] and old["range_complete"] and old["truncated"])
        self.assertFalse(old["line_truncated"])
        for field in ("content", "sha256", "read_id", "offset", "end_line", "total_lines", "next_offset", "line_numbers", "path"):
            self.assertNotIn(field, old)
        self.assertEqual(projected_inspections(selected)["0"][1], json.loads(history[2]["tool_calls"][0]["function"]["arguments"]))
        self.assertEqual(selected[-6:], history[-6:])
        history[3]["content"] = json.dumps({**read, "path": "different.py"})
        selected, _ = self.compact(history)
        self.assertIn("read_id", projected_inspections(selected)["0"][2])

    def test_redaction_and_json_escaping_use_the_actual_wire_size_while_original_history_stays_intact(self):
        secret = "dummy-credential-value"
        history = self.history(self.result(output=('中文🙂 "\\\t ' + secret + "\n") * 60))
        original = copy.deepcopy(history)
        config = replace(self.config, api_key=secret)
        selected, report = self.compact(history, config)
        wire = request_body(config, selected, TOOL_SCHEMAS)
        self.assertEqual(report["after_bytes"], len(wire))
        self.assertNotIn(secret.encode(), wire)
        self.assertIn(b"REDACTED", wire)
        self.assertEqual(history, original)

    def test_protected_large_reads_and_unconfigured_runs_do_not_decode_their_bodies_to_find_checks(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        self.append(history, "read", "read_file", {"path": "large.txt"}, {"content": "x" * 10000})
        self.append(history, "other", "run", {"argv": ["unconfigured"]}, self.result(output="y" * 10000))
        bodies = {m["content"] for m in history if m["role"] == "tool"}
        decode = json.loads
        def bounded(value, *args, **kwargs):
            self.assertNotIn(value, bodies, "protected body was unnecessarily decoded")
            return decode(value, *args, **kwargs)
        for checks in ((), self.config.checks):
            with patch("sprig_agent.context.json.loads", side_effect=bounded):
                selected, _ = self.compact(history, replace(self.config, checks=checks))
            self.assertEqual(selected, history)

    def test_actual_http_uses_the_newest_check_without_replaying_history_or_bypassing_approval(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "state.txt").write_text("0")
        (workspace / "notes.txt").write_text("notes\n" * 300)
        checker = self.root / "check.py"
        counter = self.root / "counter.txt"
        checker.write_text("from pathlib import Path\nimport sys\np=Path(" + repr(str(counter)) + ")\n"
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')\n"
            "ok=Path('state.txt').read_text()=='1'\nprint('passed' if ok else 'FAIL fixture '+('x'*4000))\nsys.exit(0 if ok else 1)\n")
        argv = (sys.executable, "-I", "-B", str(checker))
        approve = Mock(return_value=True)
        config = Config(workspace, "fixture", checks=(argv,), check_before_task=True, check_after_edit=True,
                        compact_context=True, context_trigger_ratio=0, max_output=6000, max_steps=2)
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "Set state to 1, preserve all files"}]
        with closing(Tools(config, approve)) as tools:
            self.append(history, "old", "run", {"argv": list(argv)}, tools.run(list(argv)))
            for number in range(4):
                args = {"path": "notes.txt", "offset": 1, "limit": 50}
                self.append(history, "read-" + str(number), "read_file", args, tools.execute("read_file", args))
        original = copy.deepcopy(history)
        captured = []
        def propose(payload):
            old = next(m for m in payload["messages"] if m.get("tool_call_id") == "old")
            result = json.loads(old["content"])
            self.assertTrue(result["context_elided"])
            self.assertEqual(result["returncode"], 1)
            current = next(m for m in payload["messages"] if m.get("tool_call_id") == result["check_ref"])
            self.assertIn("FAIL fixture", json.loads(current["content"])["output"])
            return call("edit", "edit_file", {"path": "state.txt", "old": "0", "new": "1"})
        with replies([propose, {"role": "assistant", "content": "Updated and checked"}], captured) as url:
            config = replace(config, base_url=url, proxy="direct")
            with closing(Tools(config, approve)) as tools:
                result, complete = Agent(config, Provider(config), tools, lambda _: None).run(messages=history)
        self.assertTrue(complete)
        self.assertEqual(len(captured), 2)
        self.assertEqual(counter.read_text(), "4")  # old, baseline, after edit, independent final
        self.assertEqual(approve.call_count, 5)  # four commands and one edit, each fresh
        self.assertEqual(result[3], original[3])
        self.assertEqual((workspace / "state.txt").read_text(), "1")


if __name__ == "__main__":
    unittest.main()
