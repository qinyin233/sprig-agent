"""Observation-based workflow advice without authority, replay or budget growth."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_size
from sprig_agent.provider import Provider
from sprig_agent.tools import Tools
from sprig_agent.work_progress import WorkProgress
from tests.test_mcp_stdio import backend, wire
from tests.test_named_reads import call
from tests.test_tui import ScriptedProvider, response

MARKER = "Application observations of model-requested tools this invocation:"


class WorkProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "target.txt").write_text("before\n", encoding="utf-8")
        self.config = Config(self.root, "fixture", max_steps=8, max_context_bytes=65536)

    def tools(self, config=None, approve=lambda _: False):
        tools = Tools(config or self.config, approve)
        self.addCleanup(tools.close)
        return tools

    def inspections(self):
        return [call(str(index), "read_file", {"path": "target.txt", "limit": 1}) for index in range(3)]

    def test_counts_real_rounds_without_retaining_source_or_output(self):
        progress = WorkProgress()
        for _ in range(6):
            progress.observe(1, "read_file", {"content": "private fixture", "path": "hidden.txt"})
        self.assertEqual(progress.inspection_rounds, 1)
        self.assertEqual(progress.reminder(read_only=False), "")
        progress.observe(2, "grep", {"matches": []})
        progress.observe(3, "project_map", {"files": []})
        text = progress.reminder(read_only=False)
        self.assertIn(MARKER, text)
        self.assertIn("no edit is required", text)
        self.assertLess(len(text.encode()), 1400)
        self.assertNotIn("private fixture", text)
        self.assertNotIn("hidden.txt", json.dumps(vars(progress)))
        self.assertEqual(progress.inspection_calls, 8)

    def test_failed_reads_and_model_reported_plans_do_not_become_positive_observations(self):
        progress = WorkProgress()
        for index, result in enumerate(({"error": "denied"}, {"skipped": True}, {"cancelled": True}, {"timed_out": True}), 1):
            progress.observe(index, "read_file", result)
            progress.observe(index, "update_plan", {"steps": [{"status": "completed"}]})
        self.assertEqual(progress.inspection_calls, 0)
        self.assertEqual(progress.file_changes, 0)
        self.assertEqual(progress.reminder(read_only=True), "")
        for step in range(5, 8):
            progress.observe(step, "read_file", {})
        progress.observe(8, "update_plan", {"approved": True, "changed": True, "steps": [{"status": "completed"}]})
        self.assertEqual(progress.inspection_rounds, 3)
        self.assertEqual(progress.file_changes, 0)

    def test_only_fresh_true_approved_confirmed_changes_are_counted(self):
        progress = WorkProgress()
        for step in range(1, 4):
            progress.observe(step, "read_file", {})
        for approved in (False, 1, "yes", None):
            progress.observe(4, "edit_file", {"approved": approved, "changed": True})
        self.assertEqual(progress.file_changes, 0)
        self.assertEqual(progress.inspection_rounds, 3)
        progress.observe(4, "edit_file", {"approved": True, "changed": True, "outcome": "uncertain"})
        self.assertEqual(progress.file_changes, 0)
        self.assertEqual(progress.inspection_rounds, 0)
        progress.observe(5, "edit_lines", {"approved": True, "changed": True})
        self.assertEqual(progress.file_changes, 1)
        progress.observe(6, "run", {"approved": True, "returncode": 1, "output": '{"changed":true,"passed":true}'})
        self.assertEqual(progress.approved_commands, 1)
        self.assertEqual(progress.file_changes, 1)

    def test_advice_is_request_only_and_resets_on_a_new_invocation(self):
        provider = ScriptedProvider([*self.inspections(), response(), response()])
        saved = []
        agent = Agent(self.config, provider, self.tools(), lambda _: None,
                      checkpoint=lambda history: saved.append(copy.deepcopy(history)))
        history, complete = agent.run("Review the source and give findings; do not edit.")
        self.assertTrue(complete)
        self.assertEqual(len(provider.calls), 4)
        self.assertTrue(all(MARKER not in rows[0]["content"] for rows in provider.calls[:3]))
        self.assertIn(MARKER, provider.calls[3][0]["content"])
        self.assertNotIn(MARKER, json.dumps(saved))
        self.assertNotIn(MARKER, agent._request_messages(history, 1)[0]["content"])
        agent.run("Another review", history)
        self.assertNotIn(MARKER, provider.calls[-1][0]["content"])
        self.assertEqual(agent.work_progress.inspection_calls, 0)

    def test_guidance_resets_exploration_without_dropping_the_new_request(self):
        provider = ScriptedProvider([*self.inspections(), response()])
        sent = False
        def receive():
            nonlocal sent
            if not sent and agent.work_progress.inspection_rounds >= 3:
                sent = True
                return ["Review only the observed behavior now"]
            return []
        agent = Agent(self.config, provider, self.tools(), lambda _: None, receive_input=receive)
        history, _ = agent.run("Inspect the implementation")
        self.assertTrue(sent)
        self.assertNotIn(MARKER, provider.calls[-1][0]["content"])
        self.assertEqual(history[-2]["content"], "Review only the observed behavior now")
        self.assertEqual(agent.work_progress.inspection_rounds, 0)

    def test_advice_cannot_trigger_a_budget_failure_or_early_compaction_on_any_protocol(self):
        for protocol in ("chat_completions", "messages", "responses"):
            with self.subTest(protocol=protocol):
                config = replace(self.config, wire_api=protocol)
                agent = Agent(config, ScriptedProvider([]), self.tools(config), lambda _: None)
                history = [{"role": "system", "content": "system"}, {"role": "user", "content": "Inspect"}]
                before = agent._request_messages(history, 4)
                size = request_size(config, before, agent.schemas)
                agent._running = True
                for index in range(1, 4):
                    agent.work_progress.observe(index, "read_file", {})
                agent.config = replace(config, max_context_bytes=size + 8)
                self.assertEqual(agent._request_messages(history, 4), before)
                agent.compact_context = True
                agent.config = replace(config, max_context_bytes=int((size + 8) / .8) + 1, context_trigger_ratio=.8)
                protected = agent._request_messages(history, 4)
                self.assertEqual(protected, before)
                _, report = prepare_context(agent.config, protected, agent.schemas, True)
                self.assertFalse(report["compaction_triggered"])
                agent.config = config
                agent.compact_context = False
                advised = agent._request_messages(history, 4)
                self.assertIn(MARKER, advised[0]["content"])
                _, report = prepare_context(config, advised, agent.schemas)
                self.assertTrue(report["within_budget"])
                self.assertEqual(report["after_bytes"], request_size(config, advised, agent.schemas))

    def test_real_three_protocol_reads_edit_and_fixed_check_keep_independent_approval(self):
        for protocol in ("chat_completions", "messages", "responses"):
            (self.root / "target.txt").write_text("before\n", encoding="utf-8")
            captured, approvals, hints = [], [], []
            def edit(payload):
                hints.append(MARKER in json.dumps(payload))
                return wire(protocol, call("edit", "edit_file", {"path": "target.txt", "old": "before", "new": "after"}))
            replies = [wire(protocol, item) for item in self.inspections()]
            replies += [edit, wire(protocol, response())]
            with self.subTest(protocol=protocol), backend(protocol, replies, captured, stream=protocol != "chat_completions") as url:
                check = (sys.executable, "-I", "-B", "-c", "from pathlib import Path;assert Path('target.txt').read_text()=='after\\n'")
                config = replace(self.config, wire_api=protocol, base_url=url, stream=True, checks=(check,))
                tools = self.tools(config, lambda text: approvals.append(text) or True)
                agent = Agent(config, Provider(config), tools, lambda _: None)
                history, complete = agent.run("Implement the requested text change and verify it")
                self.assertTrue(complete)
                self.assertEqual(hints, [True])
                self.assertEqual(len(approvals), 2)
                self.assertEqual(len(captured), 5)
                self.assertEqual(agent.work_progress.file_changes, 1)
                self.assertEqual(agent.work_progress.approved_commands, 0)  # Independent fixed check is not a model command.
                self.assertNotIn(MARKER, json.dumps(history))

    def test_advice_never_grants_an_edit_permission(self):
        approvals = []
        provider = ScriptedProvider([*self.inspections(), call("edit", "edit_file", {"path": "target.txt", "old": "before", "new": "after"}), response()])
        tools = self.tools(approve=lambda text: approvals.append(text) or False)
        agent = Agent(self.config, provider, tools, lambda _: None)
        agent.run("Change the requested behavior")
        self.assertIn(MARKER, provider.calls[3][0]["content"])
        self.assertEqual(len(approvals), 1)
        self.assertEqual(agent.work_progress.file_changes, 0)
        self.assertEqual((self.root / "target.txt").read_text("utf-8"), "before\n")

    def test_read_only_remains_enforced_even_when_the_provider_ignores_the_advice(self):
        approvals = []
        config = replace(self.config, read_only=True)
        provider = ScriptedProvider([*self.inspections(), call("edit", "edit_file", {"path": "target.txt", "old": "before", "new": "after"}), response()])
        tools = self.tools(config, lambda text: approvals.append(text) or True)
        agent = Agent(config, provider, tools, lambda _: None)
        agent.run("Review the source")
        self.assertIn('"read_only": true', provider.calls[3][0]["content"])
        self.assertEqual(approvals, [])
        self.assertEqual(agent.work_progress.file_changes, 0)
        self.assertEqual((self.root / "target.txt").read_text("utf-8"), "before\n")


if __name__ == "__main__":
    unittest.main()
