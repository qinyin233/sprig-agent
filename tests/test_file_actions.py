"""Real, bounded lifecycle effects; every stage needs fresh approval."""

import asyncio
from contextlib import closing
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.changes import ChangeError, ChangeHistory
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent.provider import Provider
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.tools import Tools, TOOL_SCHEMAS
from sprig_agent.tui import ApprovalScreen, SprigApp
from textual.widgets import TextArea
from tests.test_edit_checks import DONE, response
from tests.test_planning import replies
from tests import test_settings as settings_fixtures


class FileActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", base_url="http://127.0.0.1:1/v1")
        self.prompts = []
        self.event = threading.Event()
        self.tools = Tools(self.config, lambda text: self.prompts.append(text) or True, cancel_event=self.event)
        self.addCleanup(self.tools.close)
        self.path = self.root / "source.py"
        self.raw = b"\xef\xbb\xbfvalue = 42\r\n# exact bytes\r\n"
        self.path.write_bytes(self.raw)

    def read_id(self, path="source.py"):
        result = self.tools.execute("read_file", {"path": path, "offset": 1, "limit": 1, "line_numbers": True})
        self.assertIn("read_id", result)
        return result["read_id"]

    def action(self, action, path="source.py", **kwargs):
        return self.tools.execute("file_action", {"action": action, "path": path, **kwargs})

    def test_mkdir_is_exclusive_nonrecursive_and_has_its_own_approval(self):
        self.assertTrue(self.action("mkdir", "new folder")["changed"])
        self.assertTrue((self.root / "new folder").is_dir())
        self.assertIn("MKDIR", self.prompts[0])
        self.assertEqual(self.tools.history.summary()["records"], [])
        self.assertIn("error", self.action("mkdir", "new folder"))
        self.assertIn("error", self.action("mkdir", "missing/child"))
        self.assertFalse((self.root / "missing").exists())
        self.assertEqual(len(self.prompts), 1)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE((self.root / "new folder").stat().st_mode), 0o700)
        else:
            with Directory(self.root / "new folder") as directory:
                directory.check_private()

    def test_delete_undo_redo_preserves_exact_bytes_and_file_mode(self):
        if os.name != "nt":
            self.path.chmod(0o640)
        result = self.action("delete", read_id=self.read_id())
        self.assertTrue(result["changed"], result)
        self.assertFalse(self.path.exists())
        self.assertIn("DELETE", self.prompts[0])
        summary = self.tools.change_action("changes")
        self.assertIsNone(summary["records"][0]["after_bytes"])
        self.assertEqual(summary["records"][0]["before_bytes"], len(self.raw))
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertEqual(self.path.read_bytes(), self.raw)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)
        self.assertTrue(self.tools.change_action("redo")["changed"])
        self.assertFalse(self.path.exists())
        self.assertEqual(len(self.prompts), 3)

    def test_empty_file_delete_is_not_confused_with_missing_and_roundtrips(self):
        self.path.write_bytes(b"")
        self.assertTrue(self.action("delete", read_id=self.read_id())["changed"])
        self.assertIn("delete empty file", self.prompts[0])
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertEqual(self.path.read_bytes(), b"")

    def test_delete_journal_restores_metadata_without_replaying_action_on_restart(self):
        self.tools.close()
        history = ChangeHistory(self.config, self.parent / "changes")
        self.tools = Tools(self.config, lambda text: self.prompts.append(text) or True, history=history)
        self.addCleanup(self.tools.close)
        self.assertTrue(self.action("delete", read_id=self.read_id())["changed"])
        self.tools.close()
        restored = ChangeHistory(self.config, self.parent / "changes")
        self.addCleanup(restored.close)
        self.assertIsNone(restored.summary()["records"][0]["after_bytes"])
        self.assertFalse(self.path.exists())
        other = Tools(self.config, lambda text: self.prompts.append(text) or True, history=restored)
        self.addCleanup(other.close)
        self.assertTrue(other.change_action("undo")["changed"])
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_move_uses_two_approvals_and_two_reversible_file_records(self):
        (self.root / "dest").mkdir()
        result = self.action("move", destination="dest/moved.py", read_id=self.read_id())
        self.assertTrue(result["source_removed"], result)
        self.assertFalse(result["partial"])
        self.assertFalse(self.path.exists())
        moved = self.root / "dest/moved.py"
        self.assertEqual(moved.read_bytes(), self.raw)
        if os.name == "nt":
            with Directory(self.root / "dest") as directory:
                directory.check_private("moved.py")
        self.assertEqual(len(self.prompts), 2)
        self.assertIn("COPY FOR MOVE", self.prompts[0])
        self.assertIn("Step 2/2", self.prompts[1])
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertTrue(moved.exists())
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertFalse(moved.exists())
        self.assertTrue(self.tools.change_action("redo")["changed"])
        self.assertTrue(self.tools.change_action("redo")["changed"])
        self.assertFalse(self.path.exists())
        self.assertEqual(moved.read_bytes(), self.raw)

    def test_move_first_refusal_has_no_copy_or_history(self):
        self.tools.approve = Mock(return_value=False)
        result = self.action("move", destination="moved.py", read_id=self.read_id())
        self.assertFalse(result["changed"])
        self.assertFalse((self.root / "moved.py").exists())
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.tools.history.summary()["records"], [])

    def test_second_refusal_leaves_both_files_and_reports_partial_copy(self):
        self.tools.approve = Mock(side_effect=[True, False])
        result = self.action("move", destination="moved.py", read_id=self.read_id())
        self.assertTrue(result["partial"])
        self.assertTrue(result["changed"])
        self.assertFalse(result["source_removed"])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual((self.root / "moved.py").read_bytes(), self.raw)
        self.assertEqual(len(self.tools.history.summary()["records"]), 1)
        self.tools.approve = lambda _: True
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.assertTrue(self.path.exists())
        self.assertFalse((self.root / "moved.py").exists())

    def test_source_changes_during_first_or_second_approval_are_preserved(self):
        for stage in (1, 2):
            with self.subTest(stage=stage):
                self.path.write_bytes(self.raw)
                target = self.root / ("moved" + str(stage) + ".py")
                count = 0
                def approve(_):
                    nonlocal count
                    count += 1
                    if count == stage:
                        self.path.write_bytes(b"external change\n")
                    return True
                self.tools.approve = approve
                result = self.action("move", destination=target.name, read_id=self.read_id())
                self.assertIn("error", result)
                self.assertEqual(self.path.read_bytes(), b"external change\n")
                self.assertEqual(target.exists(), stage == 2)
                if stage == 2:
                    self.assertTrue(result["partial"])
                    self.assertFalse(result["source_removed"])

    def test_destination_exists_or_appears_after_approval_is_never_overwritten(self):
        target = self.root / "moved.py"
        target.write_bytes(b"user work")
        self.assertIn("exists", self.action("move", destination=target.name, read_id=self.read_id())["error"])
        self.assertEqual(self.prompts, [])
        target.unlink()
        def race(_):
            target.write_bytes(b"late user work")
            return True
        self.tools.approve = race
        result = self.action("move", destination=target.name, read_id=self.read_id())
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(target.read_bytes(), b"late user work")
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertIsNotNone(self.tools.history.summary()["pending"])

    def test_destination_change_during_delete_approval_preserves_source_and_reports_partial(self):
        target = self.root / "moved.py"
        count = 0
        def approve(_):
            nonlocal count
            count += 1
            if count == 2:
                replacement = self.root / "replacement"
                replacement.write_bytes(self.raw)
                os.replace(replacement, target)
            return True
        self.tools.approve = approve
        result = self.action("move", destination=target.name, read_id=self.read_id())
        self.assertTrue(result["partial"])
        self.assertFalse(result["source_removed"])
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_privacy_changes_during_delete_approval_block_write(self):
        identity = self.read_id()
        def approve(_):
            (self.root / ".sprigignore").write_text("source.py\n", encoding="utf-8")
            return True
        self.tools.approve = approve
        self.assertIn("privacy", self.action("delete", read_id=identity)["error"])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.tools.history.summary()["records"], [])

    def test_new_model_rules_during_approval_block_directory_creation(self):
        def approve(_):
            (self.root / "AGENTS.md").write_text("New development rules\n", encoding="utf-8")
            return True
        self.tools.approve = approve
        proposal = response({"id": "mkdir", "type": "function", "function": {
            "name": "file_action", "arguments": '{"action":"mkdir","path":"new"}'}})
        agent = Agent(self.config, Mock(complete=Mock(side_effect=[proposal, DONE])), self.tools, lambda _: None)
        history, _ = agent.run("Create new directory")
        result = next(json.loads(message["content"]) for message in history if message.get("tool_call_id") == "mkdir")
        self.assertIn("instructions changed", result["error"])
        self.assertFalse((self.root / "new").exists())

    def test_destination_privacy_revoked_during_delete_approval_keeps_source(self):
        count = 0
        def approve(_):
            nonlocal count
            count += 1
            if count == 2:
                (self.root / ".sprigignore").write_text("moved.py\n", encoding="utf-8")
            return True
        self.tools.approve = approve
        result = self.action("move", destination="moved.py", read_id=self.read_id())
        self.assertTrue(result["partial"])
        self.assertFalse(result["source_removed"])
        self.assertIn("privacy", result["error"])
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_mkdir_late_target_is_not_replaced(self):
        def approve(_):
            (self.root / "late").write_text("external work", encoding="utf-8")
            return True
        self.tools.approve = approve
        result = self.action("mkdir", "late")
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual((self.root / "late").read_text(encoding="utf-8"), "external work")

    def test_cancel_before_copy_or_before_delete_never_replays_or_removes_source(self):
        for stage in (1, 2):
            count = 0
            def approve(_):
                nonlocal count
                count += 1
                if count == stage:
                    self.event.set()
                return True
            self.tools.approve = approve
            target = "cancel-" + str(stage) + ".py"
            with self.assertRaises(KeyboardInterrupt):
                self.action("move", destination=target, read_id=self.read_id())
            self.event.clear()
            self.assertEqual(self.path.read_bytes(), self.raw)
            self.assertEqual((self.root / target).exists(), stage == 2)
            self.assertIsNone(self.tools.history.summary()["pending"])

    def test_delete_failure_or_checkpoint_failure_marks_uncertainty_and_blocks_followup(self):
        identity = self.read_id()
        with patch.object(Directory, "unlink", side_effect=OSError("fixture unlink failure")):
            result = self.action("delete", read_id=identity)
        self.assertEqual(result["outcome"], "uncertain")
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertIsNotNone(self.tools.history.summary()["pending"])
        before = len(self.prompts)
        self.assertIn("uncertain", self.action("mkdir", "blocked")["error"])
        self.assertEqual(len(self.prompts), before)

    def test_delete_checkpoint_failure_after_unlink_reports_uncertain_not_no_change(self):
        identity = self.read_id()
        original = self.tools.history._save
        count = 0
        def fail_second(state):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("fixture checkpoint failure")
            return original(state)
        with patch.object(self.tools.history, "_save", side_effect=fail_second):
            result = self.action("delete", read_id=identity)
        self.assertEqual(result["outcome"], "uncertain")
        self.assertFalse(self.path.exists())

    def test_move_delete_checkpoint_failure_does_not_claim_source_was_preserved(self):
        original = self.tools.history._save
        count = 0
        def fail_last(state):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError("fixture final checkpoint failure")
            return original(state)
        with patch.object(self.tools.history, "_save", side_effect=fail_last):
            result = self.action("move", destination="moved.py", read_id=self.read_id())
        self.assertEqual(result["outcome"], "uncertain")
        self.assertIsNone(result["source_removed"])
        self.assertTrue(result["copied"])
        self.assertFalse(self.path.exists())
        self.assertEqual((self.root / "moved.py").read_bytes(), self.raw)
        self.assertIsNotNone(self.tools.history.summary()["pending"])

    def test_unrelated_ids_survive_but_deleted_source_id_cannot_be_replayed(self):
        (self.root / "other.py").write_text("other = 1\n", encoding="utf-8")
        identity, other = self.read_id(), self.read_id("other.py")
        self.assertTrue(self.action("mkdir", "folder")["changed"])
        self.assertTrue(self.action("delete", read_id=identity)["changed"])
        self.path.write_bytes(self.raw)
        self.assertIn("read_id", self.action("delete", read_id=identity)["error"])
        self.assertTrue(self.action("delete", path="other.py", read_id=other)["changed"])

    def test_approved_move_follows_live_reference_then_approved_delete_removes_it(self):
        sent = []
        def complete(messages, schemas):
            sent.append(copy.deepcopy(messages))
            reference = json.loads(messages[-1]["content"].split("\n", 1)[1])[0]
            action = "move" if len(sent) == 1 else "delete"
            arguments = {"action": action, "path": reference["path"], "read_id": reference["read_id"]}
            if action == "move":
                arguments["destination"] = "moved.py"
            if len(sent) == 3:
                return DONE
            return response({"id": "file-" + str(len(sent)), "type": "function", "function": {
                "name": "file_action", "arguments": json.dumps(arguments)}})
        # Third request has no live file frame after the approved deletion.
        def provider(messages, schemas):
            if len(sent) == 2:
                sent.append(copy.deepcopy(messages))
                return DONE
            return complete(messages, schemas)
        config = replace(self.config, reference_files=("source.py",), max_steps=3)
        self.tools.config = config
        agent = Agent(config, Mock(complete=Mock(side_effect=provider)), self.tools, lambda _: None)
        history, done = agent.run("Move the selected file and then delete the moved copy")
        self.assertTrue(done)
        self.assertEqual(agent.reference_state()["files"], [])
        self.assertIn('"path": "source.py"', sent[0][-1]["content"])
        self.assertIn('"path": "moved.py"', sent[1][-1]["content"])
        self.assertFalse(any(message.get("content", "").startswith("Application-prepared context")
                             for message in sent[2] if message.get("content")))
        self.assertEqual(len(self.prompts), 3)
        self.assertEqual(validate_history(history), history)

    def test_partial_move_does_not_follow_reference_and_undo_removal_clears_it(self):
        config = replace(self.config, reference_files=("source.py",))
        self.tools.config = config
        def propose(messages, schemas):
            reference = json.loads(messages[-1]["content"].split("\n", 1)[1])[0]
            return response({"id": "move", "type": "function", "function": {"name": "file_action", "arguments": json.dumps({
                "action": "move", "path": "source.py", "destination": "moved.py", "read_id": reference["read_id"]})}})
        count = 0
        def complete(messages, schemas):
            nonlocal count
            count += 1
            return propose(messages, schemas) if count == 1 else DONE
        agent = Agent(config, Mock(complete=Mock(side_effect=complete)), self.tools, lambda _: None)
        self.tools.approve = Mock(side_effect=[True, False])
        messages, _ = agent.run("Move selected file")
        result = next(json.loads(message["content"]) for message in messages if message.get("tool_call_id") == "move")
        self.assertTrue(result["partial"])
        self.assertEqual(agent.reference_state()["files"], ["source.py"])
        agent.references.files = ("moved.py",)
        self.tools.approve = lambda _: True
        result = self.tools.change_action("undo")
        self.assertTrue(result["file_removed"])
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        agent.record_workspace_change(history, result)
        self.assertEqual(agent.reference_state()["files"], [])
        self.assertFalse((self.root / "moved.py").exists())

    def test_move_reference_path_budget_is_checked_before_any_side_effect(self):
        paths = ("source.py", *(str(number) + "x" * 499 for number in range(15)))
        config = replace(self.config, reference_files=paths)
        agent = Agent(config, Mock(), self.tools, lambda _: None)
        result = agent._execute_model_tool("file_action", {"action": "move", "path": "source.py",
            "destination": "sub/" * 250 + "target.py", "read_id": self.read_id()})
        self.assertIn("8192", result["error"])
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(agent.reference_state()["files"], list(paths))
        self.assertEqual(self.prompts, [])

    def test_missing_wrong_path_stale_and_expired_ids_fail_before_approval(self):
        (self.root / "other.py").write_text("other\n", encoding="utf-8")
        identity = self.read_id()
        for path, given in (("source.py", None), ("source.py", "forged"), ("other.py", identity)):
            self.assertIn("error", self.action("delete", path=path, read_id=given))
        self.path.write_bytes(b"changed\n")
        self.assertIn("revision", self.action("delete", read_id=identity)["error"])
        identity = self.read_id()
        self.tools.read_versions.clear()
        self.assertIn("read_id", self.action("delete", read_id=identity)["error"])
        self.assertEqual(self.prompts, [])

    def test_private_binary_invalid_utf8_and_oversized_source_never_reach_approval(self):
        self.tools.config = replace(self.config, api_key="fixture-model-credential")
        for raw in (b"binary\0", b"invalid\xff", b"fixture-model-credential", b"# sk-" + b"x" * 40, b"x" * (512 * 1024 + 1)):
            self.path.write_bytes(raw)
            identity = self.tools.read_versions.remember((self.path.name,), hashlib.sha256(raw).hexdigest())
            self.assertIn("error", self.action("delete", read_id=identity))
            self.assertEqual(self.path.read_bytes(), raw)
        self.assertEqual(self.prompts, [])

    def test_protected_escape_and_invalid_actions_never_reach_approval(self):
        for action, path, extra in (("mkdir", "../outside", {}), ("mkdir", ".git/new", {}),
                                   ("delete", ".env", {}), ("move", "source.py", {"destination": "../outside"}),
                                   ("move", "source.py", {"destination": "source.py"}), ("recursive_delete", "folder", {}),
                                   ("mkdir", "folder", {"read_id": "invalid"}), ("delete", "source.py", {"destination": "other"})):
            if action == "move":
                extra["read_id"] = self.read_id()
            self.assertIn("error", self.action(action, path, **extra))
        self.assertEqual(self.prompts, [])

    def test_configured_secret_in_paths_cannot_enter_approvals_or_backups(self):
        self.tools.config = replace(self.config, api_key="private-path-credential")
        for action, path, extra in (("mkdir", "private-path-credential", {}),
                                   ("move", "source.py", {"destination": "private-path-credential", "read_id": self.read_id()})):
            result = self.action(action, path, **extra)
            self.assertIn("error", result)
            self.assertNotIn("private-path-credential", json.dumps(result))
        self.assertEqual(self.prompts, [])
        self.assertEqual(self.tools.history.summary()["records"], [])

    def test_lifecycle_schema_does_not_increase_prior_tool_request_overhead(self):
        size = len(json.dumps(TOOL_SCHEMAS, separators=(",", ":")).encode("utf-8"))
        self.assertLessEqual(size, 4932)  # The prior eight-tool request footprint.

    def test_compaction_keeps_lifecycle_intents_and_partial_or_uncertain_evidence(self):
        from sprig_agent.context import prepare_context
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        for index, status in enumerate(({"approved": True, "changed": True},
                                        {"approved": False, "changed": True, "copied": True, "partial": True, "source_removed": False},
                                        {"approved": True, "changed": False, "outcome": "uncertain"})):
            history.extend([response({"id": "action-" + str(index), "type": "function", "function": {
                "name": "file_action", "arguments": json.dumps({"action": "move", "path": "source.py", "destination": "moved.py", "read_id": "r123456789abc-1"})}}),
                {"role": "tool", "tool_call_id": "action-" + str(index), "content": json.dumps(status)}])
        history.extend({"role": "user", "content": "later " + str(number)} for number in range(8))
        original = copy.deepcopy(history)
        projected, _ = prepare_context(self.config, history, TOOL_SCHEMAS, True)
        self.assertEqual(history, original)
        self.assertEqual(projected[2:8], original[2:8])

    def test_readonly_disabled_and_closed_tools_are_enforced_without_model_cooperation(self):
        identity = self.read_id()
        for config in (replace(self.config, read_only=True), replace(self.config, disabled_tools=("file_action",)),
                       replace(self.config, disabled_tools=("edit_file",))):
            self.tools.config = config
            self.assertIn("error", self.action("delete", read_id=identity))
            self.assertNotIn("file_action", {row["function"]["name"] for row in Agent(config, Mock(), self.tools, lambda _: None).schemas})
        self.tools.config = self.config
        self.tools.close()
        self.assertIn("closed", self.action("delete", read_id=identity)["error"])
        self.assertEqual(self.prompts, [])

    @unittest.skipUnless(os.name == "posix", "POSIX link aliases")
    def test_symlink_source_and_parent_cannot_escape(self):
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "data.py").write_bytes(self.raw)
        (self.root / "link.py").symlink_to(outside / "data.py")
        (self.root / "alias").symlink_to(outside, target_is_directory=True)
        identity = self.tools.read_versions.remember(("link.py",), hashlib.sha256(self.raw).hexdigest())
        self.assertIn("error", self.action("delete", "link.py", read_id=identity))
        self.assertIn("error", self.action("mkdir", "alias/new"))
        self.assertEqual((outside / "data.py").read_bytes(), self.raw)
        self.assertFalse((outside / "new").exists())
        self.assertEqual(self.prompts, [])

    @unittest.skipUnless(os.name == "nt", "native Windows aliases")
    def test_windows_device_stream_trailing_dot_and_case_aliases_are_rejected(self):
        identity = self.read_id()
        for destination in ("NUL", "data.py:stream", "trailing.", "trailing ", "SOURCE.PY"):
            self.assertIn("error", self.action("move", destination=destination, read_id=identity))
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(self.prompts, [])

    @unittest.skipUnless(os.name == "nt", "native Windows ACL restore")
    def test_windows_undo_deleted_private_file_does_not_inherit_broader_parent_acl(self):
        self.path.unlink()
        self.tools.root.write_new(self.path.name, self.raw, private=True)
        self.tools.root.check_private(self.path.name)
        self.assertTrue(self.action("delete", read_id=self.read_id())["changed"])
        self.assertTrue(self.tools.change_action("undo")["changed"])
        self.tools.root.check_private(self.path.name)
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertIn("private current-user ACL", self.prompts[-1])

    def test_loopback_project_restructure_checks_stage_and_final_independently(self):
        self.path.write_bytes(b"def value():\n    return 42\n")
        (self.root / "obsolete.txt").write_text("obsolete\n", encoding="utf-8")
        (self.root / "main.py").write_text("from source import value\n", encoding="utf-8")
        captured, approvals = [], []
        def call(identity, name, arguments):
            return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        def restructure(payload):
            results = [json.loads(message["content"]) for message in payload["messages"] if message["role"] == "tool"]
            return response(call("mkdir", "file_action", {"action": "mkdir", "path": "pkg"}),
                call("move", "file_action", {"action": "move", "path": "source.py", "destination": "pkg/core.py", "read_id": results[0]["read_id"]}),
                call("delete", "file_action", {"action": "delete", "path": "obsolete.txt", "read_id": results[1]["read_id"]}),
                call("import", "edit_file", {"path": "main.py", "old": "from source import value", "new": "from pkg.core import value"}))
        initial = response(call("read-source", "read_file", {"path": "source.py", "line_numbers": True}),
                           call("read-obsolete", "read_file", {"path": "obsolete.txt", "line_numbers": True}))
        check = (sys.executable, "-I", "-B", "-c", "import sys; sys.path.insert(0,'.'); from main import value; from pathlib import Path; assert value()==42; assert not Path('source.py').exists(); assert not Path('obsolete.txt').exists()")
        with replies([initial, restructure, DONE], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=3, max_context_bytes=32768,
                             checks=(check,), check_after_edit=True)
            with closing(Tools(config, lambda prompt: approvals.append(prompt) or True)) as tools, \
                    closing(SessionStore(self.parent / "sessions", config)) as store:
                agent = Agent(config, Provider(config), tools, lambda _: None, lambda messages: store.save("restructure", messages))
                history, complete = agent.run("Move the implementation into pkg/core.py, update its caller and remove the obsolete file.")
                self.assertTrue(complete)
                self.assertEqual(agent.edit_check_status, "passed")
                self.assertEqual(store.load("restructure"), history)
        self.assertEqual(len(captured), 3)
        self.assertTrue(all(size <= 32768 for _, size in captured))
        self.assertEqual(len(approvals), 7)
        self.assertEqual(sum(prompt.startswith("RUN") for prompt in approvals), 2)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.root / "obsolete.txt").exists())
        self.assertEqual((self.root / "pkg/core.py").read_bytes(), b"def value():\n    return 42\n")
        self.assertEqual(validate_history(history), history)


class FileActionSettingsTests(unittest.TestCase):
    setUp = settings_fixtures.SettingsTests.setUp
    write = settings_fixtures.SettingsTests.write
    resolve = settings_fixtures.SettingsTests.resolve

    def test_edit_policy_covers_lifecycle_and_explicit_projects_cannot_reenable_it(self):
        from sprig_agent.settings import load_settings, public_config
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\n[tools]\nedit_file=false\n')
        config, details = self.resolve(load_settings(self.path))
        self.assertFalse(config.tool_enabled("file_action"))
        self.assertEqual(public_config(config, details)["setting_sources"]["tools.file_action"], "tools.edit_file")
        project = self.write('[tools]\nfile_action=true\n', self.parent / "project.toml")
        with self.assertRaises(ValueError):
            self.resolve(load_settings(self.path).with_project(project))


class FileActionTuiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)

    async def until(self, pilot, predicate):
        for _ in range(300):
            if predicate():
                # A modal can become app.screen before composition/layout has
                # completed. Drain UI work, then recheck the observed state.
                await pilot.pause()
                if predicate():
                    return
            await pilot.pause(0.02)
        self.fail("TUI lifecycle state did not arrive")

    async def test_guidance_during_second_move_approval_preserves_both_files_and_partial_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "workspace"
            root.mkdir()
            (root / "source.py").write_text("value = 42\n", encoding="utf-8")
            config = Config(root, "fixture", base_url="http://127.0.0.1:1/v1", max_steps=3)
            sent = []
            def complete(messages, tools, **kwargs):
                sent.append(copy.deepcopy(messages))
                if len(sent) == 1:
                    return response({"id": "read", "type": "function", "function": {"name": "read_file",
                        "arguments": '{"path":"source.py","line_numbers":true}'}})
                if len(sent) == 2:
                    result = json.loads(next(message["content"] for message in reversed(messages) if message["role"] == "tool"))
                    return response({"id": "move", "type": "function", "function": {"name": "file_action", "arguments": json.dumps({
                        "action": "move", "path": "source.py", "destination": "moved.py", "read_id": result["read_id"]})}})
                return DONE
            app = SprigApp(config, provider=Mock(complete=Mock(side_effect=complete)),
                           session_dir=parent / "sessions", save_session="move-guidance")
            try:
                async with app.run_test(size=(110, 36)) as pilot:
                    editor = app.query_one("#task", TextArea)
                    editor.load_text("Move source.py to moved.py")
                    editor.focus()
                    await pilot.press("enter")
                    await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen.description.startswith("COPY FOR MOVE")
                                     and app.screen.is_mounted and bool(app.screen.query("#approve")))
                    await pilot.click("#approve")
                    await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and app.screen.description.startswith("DELETE")
                                     and app.screen.is_mounted and bool(app.screen.query("#approval-task")))
                    self.assertEqual((root / "moved.py").read_text(encoding="utf-8"), "value = 42\n")
                    guidance = app.screen.query_one("#approval-task", TextArea)
                    guidance.load_text("Keep both copies; stop moving")
                    guidance.focus()
                    await pilot.press("enter")
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual((root / "source.py").read_text(encoding="utf-8"), "value = 42\n")
                    self.assertTrue((root / "moved.py").exists())
                    result = next(json.loads(message["content"]) for message in app.messages if message.get("tool_call_id") == "move")
                    self.assertTrue(result["partial"])
                    self.assertTrue(result["changed"])
                    self.assertFalse(result["source_removed"])
                    self.assertIn("Stopped after a workspace change", result["error"])
                    self.assertNotIn("Not executed", result["error"])
                    self.assertEqual(sent[-1][-1]["content"], "Keep both copies; stop moving")
                    self.assertEqual(app.store.load("move-guidance"), app.messages)
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
