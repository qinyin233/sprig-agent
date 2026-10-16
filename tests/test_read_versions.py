"""Read-bound line edits preserve revision, privacy and fresh-approval checks."""

import copy
from contextlib import closing
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.config import Config
from sprig_agent.agent import Agent
from sprig_agent.context import prepare_context
from sprig_agent.provider import Provider
from sprig_agent.revisions import MAX_READ_VERSIONS, ReadVersions
from sprig_agent.session import SessionStore
from sprig_agent.tools import TOOL_SCHEMAS, ToolError, Tools
from tests.test_planning import replies


class ReadVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.approve = Mock(return_value=True)
        self.config = Config(self.root, "offline", api_key="fixture-private-version-marker")
        self.cancel = threading.Event()
        self.tools = Tools(self.config, self.approve, self.cancel)
        self.addCleanup(self.tools.close)

    def test_revision_argument_rejections_are_distinguishable_before_approval(self):
        arguments = {"path": "source.txt", "line_edits": [{"start_line": 1, "end_line": 1, "new": "new\n"}]}
        for extra, reason in (({}, "missing_revision"), ({"sha256": None}, "invalid_revision"),
                              ({"sha256": "not-a-digest"}, "invalid_revision")):
            with self.subTest(reason=reason):
                result = self.tools.execute("edit_file", {**arguments, **extra})
                self.assertIn("error", result)
                self.assertEqual(result.get("edit_failure", {}).get("reason"), reason, result)
        self.approve.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_short_read_identifier_pins_line_edit_without_copying_full_digest(self):
        path = self.root / "source.txt"
        path.write_bytes(b"alpha\nbeta\n")
        observed = self.tools.execute("read_file", {"path": "source.txt", "offset": 2, "limit": 1})
        self.assertIsInstance(observed.get("read_id"), str)
        second = self.tools.execute("read_file", {"path": "source.txt", "limit": 1})
        self.assertEqual(second["read_id"], observed["read_id"])
        result = self.tools.execute("edit_lines", {"path": "source.txt", "read_id": observed["read_id"],
            "edits": [{"start_line": 2, "end_line": 2, "new": "changed\n"}]})
        self.assertTrue(result.get("changed"), result)
        self.assertEqual(path.read_bytes(), b"alpha\nchanged\n")
        self.approve.assert_called_once()

    def test_identifiers_are_path_bound_and_other_file_receipts_survive_an_edit(self):
        for name in ("a.txt", "b.txt"):
            (self.root / name).write_bytes(b"old\n")
        first = self.tools.read_file("a.txt")["read_id"]
        second = self.tools.read_file("b.txt")["read_id"]
        edit = [{"start_line": 1, "end_line": 1, "new": "new\n"}]
        wrong = self.tools.execute("edit_lines", {"path": "b.txt", "read_id": first, "edits": edit})
        self.assertEqual(wrong["edit_failure"]["reason"], "unknown_read_id")
        self.approve.assert_not_called()
        self.assertTrue(self.tools.edit_lines("a.txt", first, edit)["changed"])
        self.assertTrue(self.tools.edit_lines("b.txt", second, edit)["changed"])
        used = self.tools.edit_lines("a.txt", first, edit)
        self.assertEqual(used["edit_failure"]["reason"], "unknown_read_id")
        self.assertEqual(self.approve.call_count, 2)

    def test_changed_revision_and_approval_race_never_overwrite_external_changes(self):
        path = self.root / "source.txt"
        path.write_bytes(b"old\n")
        identity = self.tools.read_file("source.txt")["read_id"]
        path.write_bytes(b"external\n")
        edit = [{"start_line": 1, "end_line": 1, "new": "new\n"}]
        stale = self.tools.edit_lines("source.txt", identity, edit)
        self.assertEqual(stale["edit_failure"], {"reason": "stale_revision", "source": "read_id"})
        self.approve.assert_not_called()
        fresh = self.tools.read_file("source.txt")["read_id"]
        self.approve.side_effect = lambda _: (path.write_bytes(b"during-review\n") and True)
        result = self.tools.execute("edit_lines", {"path": "source.txt", "read_id": fresh, "edits": edit})
        self.assertIn("error", result)
        self.assertEqual(path.read_bytes(), b"during-review\n")
        self.assertEqual(self.tools.history.summary()["records"], [])

    def test_prefix_partial_and_sensitive_reads_never_issue_identifiers(self):
        path = self.root / "source.txt"
        path.write_bytes(b"value\n" * 4000)
        self.assertNotIn("read_id", self.tools.read_file("source.txt"))
        self.assertIn("read_id", self.tools.read_file("source.txt", limit=1))
        path.write_bytes(b"x" * 20000)
        partial = self.tools.read_file("source.txt", limit=1)
        self.assertTrue(partial["line_truncated"])
        self.assertNotIn("read_id", partial)
        path.write_text("safe\n" + self.config.api_key, encoding="utf-8")
        for args in ({}, {"limit": 1}):
            result = self.tools.execute("read_file", {"path": "source.txt", **args})
            self.assertNotIn("read_id", result)
            self.assertNotIn("sha256", result)
            self.assertNotIn(self.config.api_key, json.dumps(result))
        path.write_bytes(b"binary\0")
        self.assertNotIn("read_id", self.tools.execute("read_file", {"path": "source.txt"}))

    def test_approved_command_invalidates_identifiers_but_rejection_does_not(self):
        path = self.root / "source.txt"
        path.write_bytes(b"old\n")
        identity = self.tools.read_file("source.txt")["read_id"]
        argv = [sys.executable, "-I", "-B", "-c", "pass"]
        self.approve.return_value = False
        self.assertFalse(self.tools.run(argv)["approved"])
        self.assertEqual(self.tools.read_file("source.txt")["read_id"], identity)
        self.approve.return_value = True
        self.assertEqual(self.tools.run(argv)["returncode"], 0)
        result = self.tools.edit_lines("source.txt", identity, [{"start_line": 1, "end_line": 1, "new": "new\n"}])
        self.assertEqual(result["edit_failure"]["reason"], "unknown_read_id")
        fresh = self.tools.read_file("source.txt")["read_id"]
        self.assertNotEqual(fresh, identity)
        target = "sprig_agent.windows.run_command" if os.name == "nt" else "sprig_agent.tools.subprocess.Popen"
        with patch(target, side_effect=OSError("fixture spawn failure")):
            self.assertIn("error", self.tools.execute("run", {"argv": argv}))
        failed = self.tools.edit_lines("source.txt", fresh, [{"start_line": 1, "end_line": 1, "new": "new\n"}])
        self.assertEqual(failed["edit_failure"]["reason"], "unknown_read_id")

    def test_undo_and_new_invocation_invalidate_identifiers(self):
        path = self.root / "source.txt"
        path.write_bytes(b"old\n")
        edit = [{"start_line": 1, "end_line": 1, "new": "new\n"}]
        identity = self.tools.read_file("source.txt")["read_id"]
        self.assertTrue(self.tools.edit_lines("source.txt", identity, edit)["changed"])
        fresh = self.tools.read_file("source.txt")["read_id"]
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertEqual(self.tools.edit_lines("source.txt", fresh, edit)["edit_failure"]["reason"], "unknown_read_id")
        identity = self.tools.read_file("source.txt")["read_id"]
        provider = Mock()
        provider.complete.side_effect = [model_call("old-id", "edit_lines", {"path": "source.txt", "read_id": identity, "edits": edit}),
                                         {"role": "assistant", "content": "Stopped"}]
        history, _ = Agent(self.config, provider, self.tools, lambda _: None).run("New invocation")
        self.assertEqual(json.loads(history[3]["content"])["edit_failure"]["reason"], "unknown_read_id")
        self.assertEqual(path.read_bytes(), b"old\n")

    def test_readonly_disabled_base_policy_cancellation_and_privacy_still_block(self):
        path = self.root / "source.txt"
        path.write_bytes(b"old\n")
        edit = [{"start_line": 1, "end_line": 1, "new": "new\n"}]
        for config in (replace(self.config, read_only=True), replace(self.config, disabled_tools=("edit_file",)),
                       replace(self.config, disabled_tools=("edit_lines",))):
            tools = Tools(config, self.approve)
            try:
                identity = tools.read_file("source.txt")["read_id"]
                blocked = tools.execute("edit_lines", {"path": "source.txt", "read_id": identity, "edits": edit})
                self.assertIn("error", blocked)
                self.assertNotIn("edit_lines", {s["function"]["name"] for s in Agent(config, Mock(), tools, lambda _: None).schemas})
            finally:
                tools.close()
        identity = self.tools.read_file("source.txt")["read_id"]
        self.cancel.set()
        with self.assertRaises(KeyboardInterrupt):
            self.tools.edit_lines("source.txt", identity, edit)
        self.cancel.clear()
        (self.root / ".sprigignore").write_text("source.txt\n", encoding="utf-8")
        self.assertIn("error", self.tools.execute("edit_lines", {"path": "source.txt", "read_id": identity, "edits": edit}))
        self.assertEqual(path.read_bytes(), b"old\n")
        self.approve.assert_not_called()

    def test_private_resume_does_not_restore_read_identifiers_or_replay_intents(self):
        path = self.root / "source.txt"
        path.write_bytes(b"old\n")
        observed = self.tools.read_file("source.txt")
        edit = [{"start_line": 1, "end_line": 1, "new": "new\n"}]
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"},
                   model_call("read", "read_file", {"path": "source.txt"}),
                   {"role": "tool", "tool_call_id": "read", "content": json.dumps(observed)},
                   model_call("pending", "edit_lines", {"path": "source.txt", "read_id": observed["read_id"], "edits": edit})]
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            store.ensure_new("read-bound")
            store.save("read-bound", history)
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            restored = store.load("read-bound")
        self.assertIn("NOT replayed", restored[-1]["content"])
        with closing(Tools(self.config, self.approve)) as tools:
            unknown = tools.edit_lines("source.txt", observed["read_id"], edit)
        self.assertEqual(unknown["edit_failure"]["reason"], "unknown_read_id")
        self.assertEqual(path.read_bytes(), b"old\n")
        self.approve.assert_not_called()

    def test_bounded_read_versions_and_unknown_or_other_path_ids(self):
        versions = ReadVersions()
        digest = hashlib.sha256(b"observed").hexdigest()
        ids = [versions.remember((str(index),), digest) for index in range(MAX_READ_VERSIONS + 1)]
        self.assertIsNone(versions.resolve(ids[0], ("0",)))
        self.assertIsNone(versions.resolve(ids[1], ("other",)))
        self.assertEqual(versions.resolve(ids[1], ("1",)), digest)
        versions.clear()
        self.assertIsNone(versions.resolve(ids[1], ("1",)))
        self.assertNotEqual(versions.remember(("1",), digest), ids[1])

    def test_actual_http_read_edit_verify_uses_required_short_id_and_fresh_approvals(self):
        path = self.root / "source.txt"
        path.write_bytes(b"old\n")
        captured = []
        edit = [{"start_line": 1, "end_line": 1, "new": "new\n"}]
        def propose(payload):
            observed = json.loads(payload["messages"][-1]["content"])
            self.assertIn("read_id", observed)
            self.assertEqual(observed["content"], "1: old\n")
            self.assertTrue(observed["line_numbers"])
            return model_call("edit", "edit_lines", {"path": "source.txt", "read_id": observed["read_id"], "edits": edit})
        with replies([model_call("read", "read_file", {"path": "source.txt", "limit": 1}), propose,
                      {"role": "assistant", "content": "Done"}], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=3,
                checks=((sys.executable, "-I", "-B", "-c", "from pathlib import Path;assert Path('source.txt').read_bytes()==b'new\\n'"),))
            with closing(Tools(config, self.approve)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Change and verify")
        self.assertTrue(complete)
        self.assertEqual(path.read_bytes(), b"new\n")
        self.assertEqual(self.approve.call_count, 2)
        self.assertEqual(len(captured), 3)
        schema = next(s["function"] for s in captured[0][0]["tools"] if s["function"]["name"] == "edit_lines")
        self.assertEqual(set(schema["parameters"]["required"]), {"path", "read_id", "edits"})
        self.assertTrue(json.loads(history[-1]["content"])["returncode"] == 0)

    def test_http_boundary_failure_can_be_repaired_without_any_partial_write(self):
        path = self.root / "source.txt"
        path.write_bytes(b"old\nkeep\n")
        captured, state = [], {}
        def bad_proposal(payload):
            observed = json.loads(payload["messages"][-1]["content"])
            state["read_id"] = observed["read_id"]
            self.assertEqual(observed["content"], "1: old\n2: keep\n")
            return model_call("bad", "edit_lines", {"path": "source.txt", "read_id": state["read_id"],
                "edits": [{"start_line": 1, "end_line": 1, "new": "new"}]})
        def repaired(payload):
            rejected = json.loads(payload["messages"][-1]["content"])
            self.assertIn("line ending", rejected["error"])
            self.assertEqual(path.read_bytes(), b"old\nkeep\n")
            self.approve.assert_not_called()
            return model_call("good", "edit_lines", {"path": "source.txt", "read_id": state["read_id"],
                "edits": [{"start_line": 1, "end_line": 1, "new": "new\n"}]})
        with replies([model_call("read", "read_file", {"path": "source.txt", "limit": 2}), bad_proposal, repaired,
                      {"role": "assistant", "content": "Done"}], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=4,
                checks=((sys.executable, "-I", "-B", "-c", "from pathlib import Path;assert Path('source.txt').read_bytes()==b'new\\nkeep\\n'"),))
            with closing(Tools(config, self.approve)) as tools:
                _, complete = Agent(config, Provider(config), tools, lambda _: None).run("Change and verify")
        self.assertTrue(complete)
        self.assertEqual(len(captured), 4)
        self.assertEqual(self.approve.call_count, 2)
        self.assertEqual(path.read_bytes(), b"new\nkeep\n")

    def test_completed_line_edits_can_be_compacted_without_replaying_ids(self):
        arguments = {"path": "source.txt", "read_id": "r0123456789ab-1", "edits": [
            {"start_line": 1, "end_line": 1, "new": "new text " * 1000}]}
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"},
                   model_call("old", "edit_lines", arguments), {"role": "tool", "tool_call_id": "old", "content": json.dumps({
                       "path": "source.txt", "approved": True, "changed": True})}]
        for index in range(4):
            history.extend([{"role": "user", "content": str(index)}, {"role": "assistant", "content": "ack"}])
        original = copy.deepcopy(history)
        selected, report = prepare_context(self.config, history, TOOL_SCHEMAS, compact=True)
        self.assertLess(report["after_bytes"], report["before_bytes"])
        self.assertTrue(json.loads(selected[2]["tool_calls"][0]["function"]["arguments"])["context_elided"])
        self.assertEqual(history, original)


def model_call(identity, name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}]}
