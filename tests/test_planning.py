"""Task progress survives real sessions without granting execution or acceptance."""

import copy
from contextlib import closing, contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.context import prepare_context, request_body, request_payload
from sprig_agent.planning import TaskPlan, validate_plan
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.tools import TOOL_SCHEMAS, ToolError, Tools


def call(identity, plan):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function",
        "function": {"name": "update_plan", "arguments": json.dumps({"plan": plan})}}]}


@contextmanager
def replies(messages, captured, reason=None):
    responses = iter(messages)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            captured.append((json.loads(raw), len(raw)))
            message = next(responses)
            body = json.dumps({"choices": [{"message": message, "finish_reason": reason or (
                "tool_calls" if message.get("tool_calls") else "stop")}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class PlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", max_steps=2)
        self.steps = [{"step": "Inspect implementation", "status": "in_progress"},
                      {"step": "Implement and verify", "status": "pending"}]

    def tools(self, config=None, approve=None, cancel=None):
        tool = Tools(config or self.config, approve or Mock(return_value=True), cancel_event=cancel)
        self.addCleanup(tool.close)
        return tool

    def test_real_http_plan_update_in_readonly_changes_no_files_and_needs_no_approval(self):
        captured, approve = [], Mock(return_value=True)
        with replies([call("plan", self.steps), {"role": "assistant", "content": "Findings"}], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", read_only=True)
            tools = self.tools(config, approve)
            history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Review")
        self.assertTrue(complete)
        self.assertEqual(tools.plan.snapshot()["plan"], self.steps)
        self.assertEqual(len(captured), 2)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn("update_plan", {entry["function"]["name"] for entry in captured[0][0]["tools"]})
        self.assertNotIn("run", {entry["function"]["name"] for entry in captured[0][0]["tools"]})
        self.assertEqual(json.loads(captured[1][0]["messages"][-1]["content"])["plan"], self.steps)
        self.assertEqual(validate_history(history), history)
        approve.assert_not_called()

    def test_invalid_updates_cancellation_and_output_limit_preserve_prior_plan(self):
        config = replace(self.config, api_key="fixture-private-model-key")
        cancelled = threading.Event()
        tools = self.tools(config, cancel=cancelled)
        result = tools.update_plan([{"step": "Avoid fixture-private-model-key", "status": "pending"}])
        self.assertNotIn(config.api_key, json.dumps(result) + json.dumps(tools.plan.snapshot()))
        before = tools.plan.snapshot()
        invalid = ["text", [{"step": "a", "status": "unknown"}], [{"step": "", "status": "pending"}],
                   [{"step": "a", "status": "in_progress"}] * 2, [{"step": "a\nsecret", "status": "pending"}],
                   [{"step": "a", "status": "pending", "approved": True}], self.steps * 9,
                   [{"step": "中" * 256, "status": "pending"}] * 16]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__):
                self.assertIn("error", tools.execute("update_plan", {"plan": value}))
                self.assertEqual(tools.plan.snapshot(), before)
        cancelled.set()
        with self.assertRaises(KeyboardInterrupt):
            tools.update_plan(self.steps)
        self.assertEqual(tools.plan.snapshot(), before)
        small = self.tools(replace(self.config, max_output=256))
        self.assertIn("error", small.execute("update_plan", {"plan": [{"step": "x" * 256, "status": "pending"}]}))
        self.assertEqual(small.plan.snapshot()["plan"], [])

    def test_disabled_and_closed_tool_reject_dispatch_and_direct_updates(self):
        config = replace(self.config, disabled_tools=("update_plan",))
        tools = self.tools(config)
        agent = Agent(config, Mock(), tools, lambda _: None)
        self.assertNotIn("update_plan", {entry["function"]["name"] for entry in agent.schemas})
        self.assertIn("error", tools.execute("update_plan", {"plan": self.steps}))
        with self.assertRaises(ToolError):
            tools.update_plan(self.steps)
        ordinary = self.tools()
        ordinary.close()
        self.assertIn("error", ordinary.execute("update_plan", {"plan": self.steps}))

    def test_reported_completed_plan_does_not_skip_failing_final_acceptance(self):
        config = replace(self.config, checks=((sys.executable, "-I", "-B", "-c", "raise SystemExit(1)"),))
        approve = Mock(return_value=True)
        tools = self.tools(config, approve)
        provider = Mock()
        provider.complete.side_effect = [call("complete-plan", [{"step": "All done", "status": "completed"}]),
                                         {"role": "assistant", "content": "Done"}]
        history, complete = Agent(config, provider, tools, lambda _: None).run("Fix")
        self.assertFalse(complete)
        self.assertEqual(tools.plan.snapshot()["plan"][0]["status"], "completed")
        approve.assert_called_once()
        self.assertTrue(any(message.get("role") == "tool" and '"returncode": 1' in message["content"] for message in history))

    def test_private_resume_keeps_last_completed_plan_and_never_replays_pending_update(self):
        result = validate_plan(self.steps)
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}, call("old-plan", self.steps),
                   {"role": "tool", "tool_call_id": "old-plan", "content": json.dumps(result)},
                   call("interrupted-plan", [{"step": "Never replay", "status": "completed"}])]
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            store.ensure_new("progress")
            store.save("progress", history)
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            loaded = store.load("progress")
        tools, provider = self.tools(), Mock()
        agent = Agent(self.config, provider, tools, lambda _: None)
        agent.restore_plan(loaded)
        self.assertEqual(agent.plan_snapshot(), result)
        self.assertIn("NOT replayed", loaded[-1]["content"])
        snapshot = agent.plan_snapshot()
        snapshot["plan"].clear()
        self.assertEqual(agent.plan_snapshot(), result)
        provider.complete.assert_not_called()
        cleared = [*loaded, call("clear-plan", []), {"role": "tool", "tool_call_id": "clear-plan", "content": json.dumps(validate_plan([]))}]
        agent.restore_plan(cleared)
        self.assertEqual(agent.plan_snapshot(), validate_plan([]))
        agent.restore_plan(None)
        self.assertEqual(agent.plan_snapshot()["plan"], [])
        self.assertFalse(agent.plan_snapshot()["plan_updated"])

    def test_compaction_elides_old_plan_arguments_and_results_but_keeps_latest_and_errors(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        for index in range(4):
            steps = [{"step": str(index) + "x" * 200, "status": "pending"}] * 8
            history.extend([call(str(index), steps), {"role": "tool", "tool_call_id": str(index),
                                                    "content": json.dumps(validate_plan(steps))}])
        history.extend([call("invalid", self.steps), {"role": "tool", "tool_call_id": "invalid", "content": '{"error":"invalid plan"}'}])
        for index in range(4):
            history.extend([{"role": "user", "content": "guidance " + str(index)}, {"role": "assistant", "content": "acknowledged"}])
        original = copy.deepcopy(history)
        config = replace(self.config, max_context_bytes=16384)
        selected, report = prepare_context(config, history, TOOL_SCHEMAS, compact=True)
        self.assertTrue(report["within_budget"])
        self.assertLess(report["after_bytes"], report["before_bytes"])
        self.assertTrue(json.loads(selected[2]["tool_calls"][0]["function"]["arguments"])["context_elided"])
        self.assertTrue(json.loads(selected[3]["content"])["context_elided"])
        self.assertEqual(selected[8:12], original[8:12])
        self.assertEqual(selected[-6:], original[-6:])
        self.assertEqual(history, original)
        self.assertEqual(validate_history(selected), selected)
        restored = TaskPlan()
        restored.restore(selected)
        self.assertEqual(restored.snapshot(), validate_plan(json.loads(original[8]["tool_calls"][0]["function"]["arguments"])["plan"]))

    def test_truncated_http_plan_response_never_updates_state(self):
        captured = []
        with replies([call("truncated", self.steps)], captured, reason="length") as url:
            config = replace(self.config, base_url=url, proxy="direct", max_tokens=64)
            tools = self.tools(config)
            with self.assertRaises(ProviderError):
                Agent(config, Provider(config), tools, lambda _: None).run("Plan")
            self.assertEqual(tools.plan.snapshot()["plan"], [])
        self.assertEqual(len(captured), 1)

    def test_chat_plan_is_local_and_wire_json_preserves_content(self):
        tools, provider, emitted = self.tools(), Mock(), []
        agent = Agent(self.config, provider, tools, emitted.append)
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "a b 中文"}, call("plan", self.steps),
                   {"role": "tool", "tool_call_id": "plan", "content": json.dumps(validate_plan(self.steps))}]
        with patch("builtins.input", side_effect=["/plan", "/exit"]):
            self.assertEqual(cli.chat(agent, None, history, emitted.append, autostart=False), 0)
        self.assertIn("Inspect implementation", " ".join(emitted))
        provider.complete.assert_not_called()
        wire = request_body(self.config, history, TOOL_SCHEMAS)
        ordinary = json.dumps(request_payload(self.config, history, TOOL_SCHEMAS)).encode()
        self.assertEqual(json.loads(wire), json.loads(ordinary))
        self.assertLess(len(wire), len(ordinary))
