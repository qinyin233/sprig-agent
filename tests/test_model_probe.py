"""User entry points probe the real HTTP adapters with disposable fixtures."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sprig_agent import cli
from sprig_agent.config import Config
from sprig_agent.model_probe import TOOL_INPUT, probe_model
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as ui
from tests.test_glob_protocols import answer, values
from tests.test_native_protocols import backend, sse, stream_events


TOOL_CALL = [("probe", "sprig_model_probe", TOOL_INPUT)]
SECRET = "disposable-probe-credential"
RAW_TEXT = "SUPPRESSED_RESPONSE " + SECRET


def fixtures(protocol):
    calls = answer(protocol, True, calls=TOOL_CALL)
    if protocol == "chat_completions":
        arguments = json.dumps(TOOL_INPUT)
        start = {"index": 0, "id": "probe", "type": "function", "function": {
            "name": "sprig_model_probe", "arguments": arguments[:6]}}
        end = {"index": 0, "function": {"arguments": arguments[6:]}}
        events = [{"choices": [{"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": reason}]}
                  for call, reason in [(start, None), (end, "tool_calls")]]
        calls = b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"
    else:
        events = stream_events(protocol, calls)
        kind, key = (("content_block_delta", "partial_json") if protocol == "messages"
                     else ("response.function_call_arguments.delta", "delta"))
        at = next(index for index, event in enumerate(events) if event["type"] == kind)
        original = events[at]
        payload = original["delta"] if protocol == "messages" else original
        arguments = payload[key]
        parts = [{**payload, key: part} for part in (arguments[:6], arguments[6:])]
        events[at:at + 1] = ([{**original, "delta": part} for part in parts] if protocol == "messages" else parts)
        calls = sse(events)
    return [answer(protocol, False, text=RAW_TEXT), answer(protocol, True, text=RAW_TEXT),
            answer(protocol, False, calls=TOOL_CALL), calls]


class ModelProbeTests(unittest.TestCase):
    def invoke(self, root, endpoint, protocol, *, approve=None, extra=()):
        output, errors = io.StringIO(), io.StringIO()
        argv = ["--no-config", "--workdir", str(root), "--model", "fixture", "--base-url", endpoint,
                "--wire-api", protocol, "--proxy", "direct", "--retry-attempts", "5", "--max-tokens", "1536",
                "--session-dir", str(root / "sessions"), *extra]
        with patch.dict(os.environ, {"SPRIG_API_KEY": SECRET}, clear=True), redirect_stdout(output), redirect_stderr(errors):
            with patch.object(cli.sys.stdin, "isatty", return_value=approve is not None), patch("builtins.input", return_value=approve):
                code = cli.main(argv)
        combined = output.getvalue() + errors.getvalue()
        self.assertNotIn(SECRET, combined)
        self.assertNotIn("SUPPRESSED_RESPONSE", combined)
        return code, output.getvalue(), errors.getvalue()

    def test_cli_tests_all_four_shapes_and_reassembles_tool_fragments_for_three_protocols(self):
        for protocol, path, cap in [("chat_completions", "chat/completions", "max_tokens"),
                                    ("messages", "messages", "max_tokens"), ("responses", "responses", "max_output_tokens")]:
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as temporary:
                root, captured = Path(temporary), []
                with backend(protocol, fixtures(protocol), captured, stream=None) as url:
                    code, output, approval = self.invoke(root, url, protocol, approve="yes",
                                                         extra=("--probe-model", "--request-usage"))
                result = json.loads(output)
                self.assertEqual(code, 0)
                self.assertEqual((result["attempts"], result["requested_output_token_limit"]), (4, 1024))
                self.assertEqual([row["status"] for row in result["checks"]], ["passed"] * 4)
                self.assertGreater(result["checks"][1]["text_chunks"], 0)
                self.assertIn("up to 4 model requests", approval)
                self.assertEqual([item["path"] for item in captured], [f"/v1/{path}"] * 4)
                self.assertEqual([item["body"].get("stream", False) for item in captured], [False, True, False, True])
                for index, item in enumerate(captured):
                    self.assertEqual(item["body"][cap], 1024)
                    self.assertEqual(bool(item["body"].get("tools")), index >= 2)
                    if protocol == "chat_completions":
                        self.assertEqual("stream_options" in item["body"], index % 2 == 1)
                    self.assertIn(SECRET, item["headers"].values() if protocol == "messages"
                                  else [value.removeprefix("Bearer ") for value in item["headers"].values()])
                self.assertEqual(list(root.iterdir()), [])

    def test_decline_noninteractive_and_offline_doctor_never_send(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, captured = Path(temporary), []
            with backend("chat_completions", [], captured, stream=None) as url:
                for answer_text in (None, "", "no"):
                    code, output, _ = self.invoke(root, url, "chat_completions", approve=answer_text, extra=("--probe-model",))
                    self.assertEqual(code, 2)
                    self.assertEqual([row["status"] for row in json.loads(output)["checks"]], ["not_run"] * 4)
                code, output, _ = self.invoke(root, url, "chat_completions", extra=("--doctor",))
                self.assertEqual(code, 0)
                self.assertFalse(json.loads(output)["network_contacted"])
            self.assertEqual(captured, [])

    def test_incomplete_stream_and_wrong_tool_keep_failure_and_auth_stops_remaining(self):
        protocol = "chat_completions"
        responses = [answer(protocol, False, text=RAW_TEXT), answer(protocol, True, text=RAW_TEXT).split(b"data: [DONE]")[0],
                     answer(protocol, False, calls=[("unexpected", "run", {"argv": ["never-run"]})]), (429, {"message": RAW_TEXT})]
        with tempfile.TemporaryDirectory() as temporary:
            root, captured = Path(temporary), []
            with backend(protocol, responses, captured, stream=None) as url:
                code, output, _ = self.invoke(root, url, protocol, approve="y", extra=("--probe-model",))
            self.assertEqual(code, 2)
            self.assertEqual(len(captured), 4)
            report = json.loads(output)
            self.assertEqual([row.get("reason") for row in report["checks"]],
                             [None, "incomplete_stream", "unexpected_tool_call", "http_error"])
            self.assertEqual(report["checks"][-1]["request"]["http_status"], 429)
            self.assertNotIn("never-run", output)
            captured.clear()
            with backend(protocol, [(401, {"message": RAW_TEXT})], captured) as url:
                code, output, _ = self.invoke(root, url, protocol, approve="y", extra=("--probe-model",))
            self.assertEqual(code, 2)
            self.assertEqual(len(captured), 1)
            self.assertEqual([row["status"] for row in json.loads(output)["checks"]], ["failed"] + ["not_run"] * 3)

    def test_cancelled_probe_preserves_observed_rows_and_does_not_retry(self):
        protocol = "chat_completions"
        with tempfile.TemporaryDirectory() as temporary:
            captured = []
            def cancel_after_first():
                if captured:
                    raise KeyboardInterrupt
            with backend(protocol, fixtures(protocol), captured, stream=None) as url:
                config = Config(Path(temporary), "fixture", url, proxy="direct", retry_attempts=5)
                result = probe_model(config, lambda _: True, check_cancelled=cancel_after_first)
        self.assertTrue(result["cancelled"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(result["attempts"], 1)
        self.assertEqual([row["status"] for row in result["checks"]], ["cancelled"] + ["not_run"] * 3)


class ModelProbeTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_enter_then_approval_uses_current_endpoint_without_touching_saved_history(self):
        protocol, captured, original = "responses", [], ui.ScriptedProvider([])
        with backend(protocol, fixtures(protocol), captured, stream=None) as url:
            self.config = replace(self.config, base_url=url, wire_api=protocol, api_key=SECRET, proxy="direct")
            app = self.app(original, save_session="probe")
            async with app.run_test(size=(80, 24)) as pilot:
                session = self.parent / "sessions/probe.json"
                app.messages = [{"role": "system", "content": "LOCAL INSTRUCTIONS"},
                                {"role": "user", "content": "PRIVATE USER MESSAGE"}]
                app.checkpoint(app.messages)
                history = session.read_bytes()
                app.set_task_result(2, "Original task status")
                editor = app.query_one("#task", TaskInput)
                editor.load_text("/probe-model")
                editor.focus()
                await pilot.press("enter")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                await self.click(pilot, "#reject")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(captured, [])
                editor.load_text("Unsent draft kept")
                self.assertTrue(app.slash_command("/probe-model"))
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                self.assertIn("up to 4 model requests", app.screen.description)
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(app.last_operation_status, 0)
                self.assertEqual(app.task_status, 2)
                self.assertEqual(editor.text, "Unsent draft kept")
                self.assertEqual(session.read_bytes(), history)
                self.assertEqual(original.calls, [])
                self.assertNotIn(SECRET, app.transcript)
                self.assertNotIn("SUPPRESSED_RESPONSE", app.transcript)
        self.assertEqual(len(captured), 4)
        self.assertFalse(any("PRIVATE USER MESSAGE" in json.dumps(body) for item in captured for body in values(item["body"])))
