"""Historical edits fork before the selected request and never replay old actions."""
from contextlib import closing, redirect_stderr, redirect_stdout
import copy
import hashlib
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.conversation_fork import ConversationFork, turn_catalog
from sprig_agent.conversation_revision import RevisionSource, revision_command
from sprig_agent.message_origin import application_message
from sprig_agent.revision_ui import editor_text
from sprig_agent.session import SessionStore
from sprig_agent.tools import Tools
from tests.test_conversation_fork import conversation
from tests.test_generation_options import envelope
from tests.test_mcp_stdio import wire
from tests.test_native_protocols import backend
from tests import test_tui as ui


class ConversationRevisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", read_only=True)
        self.cancelled = threading.Event()
        self.tools = Tools(self.config, self.fail, self.cancelled)
        self.addCleanup(self.tools.close)
        self.provider = Mock()
        self.provider.complete.side_effect = AssertionError("no model request in preparation")
        self.agent = Agent(self.config, self.provider, self.tools, lambda _: None, cancel_event=self.cancelled)
        self.history = conversation()
        self.sessions = self.parent / "sessions"

    def test_before_first_request_keeps_source_and_workspace_and_invalidates_old_evidence(self):
        path = self.root / "current.txt"
        path.write_text("CURRENT_WORKSPACE", encoding="utf-8")
        read_id = self.tools.read_versions.remember(("current.txt",), "old-digest")
        owner = self.tools.logs.owner
        self.agent.baseline_status = self.agent.edit_check_status = "passed"
        self.agent.summary = {"old": "projection"}
        original = copy.deepcopy(self.history)
        candidate = RevisionSource(self.agent, self.history, "2").prepare("NEW_REQUEST 中文\nkeep whitespace  ")
        prompts = []
        self.assertTrue(candidate.preflight(lambda text: prompts.append(text) or True, self.sessions))
        agent, history, store, name = candidate.commit(self.sessions)
        self.assertEqual(self.history, original)
        self.assertNotIn("FIRST_TASK", json.dumps(history))
        self.assertNotIn("LATER_REQUIREMENT", json.dumps(history))
        self.assertEqual([row["content"] for row in history if row.get("role") == "user"], ["NEW_REQUEST 中文\nkeep whitespace  "])
        self.assertEqual(history[0]["content"], agent._prompt())
        self.assertIn("新请求正文", prompts[0])
        self.assertIn("不会重放", prompts[0])
        self.assertEqual(candidate.through, 1)
        self.assertEqual(candidate.replaced, 2)
        self.assertEqual(path.read_text(), "CURRENT_WORKSPACE")
        self.assertIsNone(self.tools.read_versions.resolve(read_id, ("current.txt",)))
        self.assertNotEqual(owner, self.tools.logs.owner)
        self.assertIsNone(agent.summary)
        self.assertEqual(agent.baseline_status, "not_run")
        self.assertEqual(agent.edit_check_status, "not_run")
        self.assertIsNone(store)
        self.assertIsNone(name)
        self.assertIs(agent.provider, self.provider)
        self.provider.complete.assert_not_called()
        with self.assertRaises(ValueError):
            candidate.commit(self.sessions)

    def test_later_revision_retains_only_closed_prefix_and_real_request_boundaries(self):
        self.history.insert(5, application_message("APPLICATION_OBSERVATION"))
        source = RevisionSource(self.agent, self.history, "7")
        candidate = source.prepare("/exit $ARGUMENTS is literal new text")
        self.assertTrue(candidate.preflight(lambda _: True, self.sessions))
        _, history, _, _ = candidate.commit(self.sessions)
        serialized = json.dumps(history)
        self.assertIn("FIRST_TASK", serialized)
        self.assertIn("FIRST_ANSWER", serialized)
        self.assertIn("old-action", serialized)
        self.assertIn("APPLICATION_OBSERVATION", serialized)
        self.assertNotIn("LATER_REQUIREMENT", serialized)
        self.assertNotIn("LATER_ANSWER", serialized)
        self.assertNotIn("OLD_PRIVATE_REASON", serialized)
        self.assertEqual(len(turn_catalog(history)), 2)
        self.assertEqual(history[-1]["content"], "/exit $ARGUMENTS is literal new text")
        self.provider.complete.assert_not_called()

    def test_private_branch_preserves_original_file_and_saves_replacement_once(self):
        with closing(SessionStore(self.sessions, self.config)) as original:
            original.save("original", self.history)
            before = (self.sessions / "original.json").read_bytes()
            candidate = RevisionSource(self.agent, self.history, "6").prepare("CHANGED", saving=True, source_name="original")
            self.assertTrue(candidate.preflight(lambda _: True, self.sessions))
            _, history, store, name = candidate.commit(self.sessions)
            with closing(store):
                self.assertEqual(store.load(name), history)
            self.assertEqual((self.sessions / "original.json").read_bytes(), before)
            self.assertEqual(sum(row.get("content") == "CHANGED" for row in history), 1)

    def test_invalid_targets_observations_and_replacement_types_refuse(self):
        self.history.append(application_message("not a new human task"))
        for target in ("1", "3", "4", "5", "8", "current", "２", "0", "5001", "-1", True):
            with self.subTest(target=target), self.assertRaises(ValueError):
                RevisionSource(self.agent, self.history, target)
        source = RevisionSource(self.agent, self.history, "2")
        for body in (None, False, "", " \n", "x" * (self.config.max_context_bytes + 1)):
            with self.subTest(body_type=type(body).__name__), self.assertRaises(ValueError):
                source.prepare(body)
        self.provider.complete.assert_not_called()

    def test_source_and_proposal_staleness_cancel_and_closed_resources_fail(self):
        for mode in ("content", "version", "cancel", "closed"):
            with self.subTest(mode=mode):
                event = threading.Event()
                tools = Tools(self.config, self.fail, event)
                self.addCleanup(tools.close)
                agent = Agent(self.config, self.provider, tools, lambda _: None, cancel_event=event)
                history = conversation()
                source = RevisionSource(agent, history, "2")
                candidate = source.prepare("fresh")
                self.assertTrue(candidate.preflight(lambda _: True, self.sessions))
                if mode == "content": history[1]["content"] = "changed behind editor"
                elif mode == "version": tools.rebind_model(self.config)
                elif mode == "cancel": event.set()
                else: tools.close()
                with self.assertRaises((ValueError, OSError, KeyboardInterrupt)):
                    source.prepare("must refuse")
                with self.assertRaises((ValueError, OSError, KeyboardInterrupt)):
                    candidate.commit(self.sessions)

    def test_decline_and_truthy_non_boolean_never_commit_or_send(self):
        source = RevisionSource(self.agent, self.history, "2")
        for answer in (False, 1, "yes", [True]):
            with self.subTest(answer_type=type(answer).__name__):
                candidate = source.prepare("new", saving=True)
                self.assertFalse(candidate.preflight(lambda _: answer, self.sessions))
                with self.assertRaises(ValueError):
                    candidate.commit(self.sessions)
        self.assertFalse(self.sessions.exists())
        self.provider.complete.assert_not_called()

    def test_actual_request_budget_checks_retained_prefix_and_replacement_before_save(self):
        config = replace(self.config, max_context_bytes=4096)
        with closing(Tools(config, self.fail)) as tools:
            agent = Agent(config, self.provider, tools, lambda _: None)
            candidate = RevisionSource(agent, self.history, "2").prepare("small", saving=True)
            with self.assertRaisesRegex(ValueError, "request budget"):
                candidate.preflight(lambda _: self.fail("budget failure must not ask approval"), self.sessions)
            with self.assertRaises(ValueError):
                candidate.commit(self.sessions)
        self.assertFalse(self.sessions.exists())
        self.history[-1]["content"] = "FUTURE_ONLY" * 100000
        candidate = RevisionSource(self.agent, self.history, "2").prepare("small")
        self.assertTrue(candidate.preflight(lambda _: True, self.sessions))
        self.assertNotIn("FUTURE_ONLY", json.dumps(candidate.history))

    def test_plain_fork_preflight_uses_current_system_prompt_before_accepting_budget(self):
        config = replace(self.config, max_context_bytes=10000)
        with closing(Tools(config, self.fail)) as tools:
            agent = Agent(config, self.provider, tools, lambda _: None)
            candidate = ConversationFork(agent, self.history, "2")
            with patch.object(Agent, "_prompt", return_value="CURRENT_SYSTEM" * 2000):
                with self.assertRaisesRegex(ValueError, "request budget"):
                    candidate.preflight(lambda _: True, self.sessions)
            self.assertFalse(self.sessions.exists())
            self.provider.complete.assert_not_called()

    def test_confirmation_redacts_credentials_and_does_not_turn_text_into_a_command(self):
        secret = "fixture-revision-credential"
        config = replace(self.config, api_key=secret)
        with closing(Tools(config, self.fail)) as tools:
            agent = Agent(config, self.provider, tools, lambda _: None)
            candidate = RevisionSource(agent, self.history, "2").prepare("/undo " + secret)
            descriptions = []
            self.assertTrue(candidate.preflight(lambda text: descriptions.append(text) or True, self.sessions))
            self.assertNotIn(secret, descriptions[0])
            self.assertEqual(candidate.history[-1]["content"], "/undo [REDACTED]")

    def test_large_confirmation_binds_full_body_by_digest_without_dumping_it(self):
        body = "BODY_PAYLOAD_" * 4000
        candidate = RevisionSource(self.agent, self.history, "2").prepare(body)
        prompts = []
        self.assertTrue(candidate.preflight(lambda text: prompts.append(text) or True, self.sessions))
        self.assertLess(len(prompts[0].encode("utf-8")), 4096)
        self.assertIn(hashlib.sha256(body.encode()).hexdigest(), prompts[0])
        self.assertIn(str(len(body.encode())), prompts[0])
        self.assertIn(self.config.model, prompts[0])
        self.assertIn(self.config.base_url, prompts[0])
        self.assertNotIn("BODY_PAYLOAD_", prompts[0])
        self.assertEqual(candidate.history[-1]["content"], body)

    def test_revision_command_keeps_json_text_and_rejects_invalid_or_oversized_input(self):
        body = '  中文\r\n\t"quote"  '
        self.assertEqual(revision_command("0002 " + json.dumps(body), 4096), ("2", body))
        self.assertEqual(revision_command("2", 4096), ("2", None))
        self.assertEqual(revision_command("", 4096), (None, None))
        for value in ('2 true', '2 {}', '2 ""', '2 "bad" extra', '２ "body"', 'current "body"', '5001 "body"', '2 ' + json.dumps("中" * 1400)):
            with self.subTest(size=len(value)), self.assertRaises(ValueError):
                revision_command(value, 4096)

    def test_editor_safe_json_view_roundtrips_controls_and_rejects_without_truncation(self):
        body = "中文\r\n\t\x1b[31m\u202e\U000e0001"
        view, escaped = editor_text(body, 4096)
        self.assertTrue(escaped)
        self.assertIn("中文", view)
        self.assertNotIn("\u202e", view)
        self.assertNotIn("\U000e0001", view)
        self.assertEqual(json.loads(view), body)
        self.assertNotIn("\x1b", view)
        self.assertEqual(editor_text("普通正文\nnext", 4096), ("普通正文\nnext", False))
        readable, escaped = editor_text("中" * 1000 + "\t", 4096)
        self.assertTrue(escaped)
        self.assertLessEqual(len(readable.encode("utf-8")), 4096)
        self.assertEqual(json.loads(readable), "中" * 1000 + "\t")
        with self.assertRaises(ValueError): editor_text("\0" * 2000, 4096)
        with self.assertRaises(ValueError): editor_text("中" * 2000, 4096)

    def test_six_real_cli_protocol_paths_revise_and_freshly_edit_and_verify_current_workspace(self):
        check = [sys.executable, "-I", "-B", "-c", "from pathlib import Path;assert Path('answer.txt').read_text()=='new'"]
        for protocol in ("chat_completions", "messages", "responses"):
            for streaming in (False, True):
                with self.subTest(protocol=protocol, streaming=streaming):
                    sessions = self.parent / ("saved-" + protocol + str(streaming))
                    path = self.root / "answer.txt"
                    path.write_text("new", encoding="utf-8")
                    captured, approvals, original_bytes = [], [], []
                    messages = [ui.response("FIRST_ANSWER"), ui.response("LATER_ANSWER"),
                                ui.call("read-now", "read_file", {"path": "answer.txt"}),
                                ui.call("edit-now", "edit_file", {"path": "answer.txt", "old": "old", "new": "new"}), ui.response()]
                    replies = [envelope(row) if protocol == "chat_completions" and not streaming else wire(protocol, row) for row in messages]
                    body = "/exit is literal REVISION_BODY 中文"
                    inputs = iter(["FIRST_TASK", "LATER_REQUIREMENT", "/revise 2 " + json.dumps(body), "/exit"])
                    def user_input(_):
                        value = next(inputs)
                        if value.startswith("/revise "):
                            original_bytes.append((sessions / "original.json").read_bytes())
                            path.write_text("old", encoding="utf-8")  # External change; branching must not roll it back.
                        return value
                    with backend(protocol, replies, captured, stream=streaming and protocol != "chat_completions") as url:
                        output = io.StringIO()
                        with patch.dict(os.environ, {}, clear=True), patch("pathlib.Path.home", return_value=self.parent), patch("sys.stdin.isatty", return_value=True), \
                                patch("builtins.input", side_effect=user_input), patch.object(cli, "approve", side_effect=lambda text: approvals.append(text) or True), \
                                redirect_stdout(output), redirect_stderr(output):
                            status = cli.main(["--no-config", "--workdir", str(self.root), "--model", "fixture", "--base-url", url, "--wire-api", protocol,
                                               "--proxy", "direct", "--chat", "--session-dir", str(sessions), "--save-session", "original", "--max-steps", "3",
                                               "--check", json.dumps(check), "--stream" if streaming else "--no-stream"])
                    self.assertEqual(status, 0, output.getvalue()[-1800:])
                    self.assertEqual(len(captured), 5)
                    current_request = json.dumps(captured[2]["body"], ensure_ascii=False)
                    self.assertIn(body, current_request)
                    self.assertNotIn("FIRST_TASK", current_request)
                    self.assertNotIn("LATER_REQUIREMENT", current_request)
                    self.assertEqual(len(approvals), 5)  # Two old checks, branch, new edit, independent new check.
                    self.assertEqual(path.read_text(), "new")
                    self.assertEqual((sessions / "original.json").read_bytes(), original_bytes[0])
                    branches = list(sessions.glob("fork-*.json"))
                    self.assertEqual(len(branches), 1)
                    saved = json.loads(branches[0].read_text("utf-8"))["messages"]
                    self.assertEqual([row["content"] for row in saved if row.get("role") == "user"], [body])
