"""Opt-in reasoning metadata and application-check projection, without paid APIs."""

import asyncio
from contextlib import closing, contextmanager
import copy
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
from sprig_agent.context import prepare_context, provider_messages, request_body, request_size
from sprig_agent.provider import MAX_RESPONSE, Provider, ProviderError, validate_message
from sprig_agent.session import SessionError, SessionStore, application_check, validate_history
from sprig_agent.settings import load_settings, public_config
from sprig_agent.streaming import complete_stream
from sprig_agent.tools import Tools
from sprig_agent.tui import SprigApp
from textual.widgets import TextArea
from tests import test_settings as settings_fixtures
from tests.test_streaming import Fragmented, chunk, sse


def call(identity, name, arguments, reasoning="PRIVATE_PROTOCOL_REASON"):
    return {"role": "assistant", "content": None, "reasoning_content": reasoning, "tool_calls": [{
        "id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def done(reasoning="PRIVATE_FINAL_REASON"):
    return {"role": "assistant", "content": "Done", "reasoning_content": reasoning}


def wire(message, *, streaming=False, reason=None, complete=True):
    reason = reason or ("tool_calls" if message.get("tool_calls") else "stop")
    if not streaming:
        return json.dumps({"choices": [{"message": message, "finish_reason": reason}]}).encode()
    pieces = []
    if "reasoning_content" in message:
        value = message["reasoning_content"]
        if isinstance(value, str):
            boundary = len(value) // 2
            pieces.extend([chunk({"reasoning_content": value[:boundary]}), chunk({"reasoning_content": value[boundary:]})])
        else:
            pieces.append(chunk({"reasoning_content": value}))
    if message.get("content"):
        pieces.append(chunk({"content": message["content"]}))
    if message.get("tool_calls"):
        pieces.append(chunk({"tool_calls": [{"index": index, **item} for index, item in enumerate(message["tool_calls"])]}))
    pieces.append(chunk(reason=reason))
    return sse(*pieces, done=complete)


@contextmanager
def backend(responses, captured, *, streaming=False, strict=False):
    responses, failures = iter(responses), []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(raw)
            captured.append((payload, len(raw)))
            try:
                if strict:
                    for message in payload["messages"]:
                        if message["role"] == "assistant":
                            assert "reasoning_content" in message, "missing reasoning_content"
                            assert not any(item["id"].startswith("sprig-") for item in message.get("tool_calls", [])), "synthetic tool call"
                        assert "sprig_check" not in message, "internal marker leaked to transport"
                value = next(responses)
                value = value(payload) if callable(value) else value
                body = value if isinstance(value, bytes) else wire(value, streaming=streaming)
                status = 200
            except Exception as error:
                failures.append(str(error))
                status, body = 400, b"fixture protocol mismatch"
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if streaming else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", failures
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


class ReasoningProtocolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", base_url="http://127.0.0.1:1/v1", proxy="direct")

    def test_defaults_keep_wire_binding_and_reasoning_discard_unchanged(self):
        self.assertFalse(self.config.preserve_reasoning)
        self.assertEqual(self.config.check_history_format, "tool_calls")
        self.assertEqual(self.config.binding(), {"model": "fixture", "base_url": self.config.base_url, "workdir": str(self.root)})
        self.assertEqual(validate_message(done()), {"role": "assistant", "content": "Done"})
        payload = json.loads(request_body(self.config, [done()], []))
        self.assertNotIn("reasoning_content", payload["messages"][0])
        self.assertNotIn("preserve_reasoning", payload)
        self.assertNotIn("check_history_format", payload)

    def test_text_empty_null_and_absent_reasoning_roundtrip_without_becoming_content(self):
        for value in ("reason 中文🙂", "", None):
            message = done(value)
            result = validate_message(message, preserve_reasoning=True)
            self.assertEqual(result["reasoning_content"], value)
            self.assertEqual(result["content"], "Done")
            self.assertEqual(validate_history([{"role": "system", "content": "system"}, result])[-1], result)
        self.assertNotIn("reasoning_content", validate_message({"role": "assistant", "content": "Done"}, preserve_reasoning=True))

    def test_reasoning_is_assistant_only_and_not_an_executable_tool_or_final_answer(self):
        config = replace(self.config, preserve_reasoning=True)
        messages = [{"role": "system", "content": "system", "reasoning_content": "spoof"},
                    {"role": "user", "content": "task", "reasoning_content": "spoof"}, done("real")]
        projected = provider_messages(config, messages)
        self.assertTrue(all("reasoning_content" not in item for item in projected[:2]))
        self.assertEqual(projected[-1]["reasoning_content"], "real")
        with self.assertRaises(ProviderError):
            validate_message({"role": "assistant", "content": None, "reasoning_content": '{"tool_calls":[]}'}, preserve_reasoning=True)

    def test_invalid_reasoning_types_unicode_and_size_fail_with_fixed_diagnostics(self):
        for value in (True, 3, ["private"], {"private": "secret"}, "\ud800"):
            with self.assertRaises(ProviderError) as captured:
                validate_message(done(value), preserve_reasoning=True)
            self.assertNotIn("secret", str(captured.exception))
        for value in ("x" * (MAX_RESPONSE + 1), "中" * (MAX_RESPONSE // 3 + 1)):
            with self.assertRaises(ProviderError) as captured:
                validate_message(done(value), preserve_reasoning=True)
            self.assertEqual(captured.exception.code, "response_limit")

    def test_oversized_scalar_is_rejected_before_allocating_its_utf8_copy(self):
        class Unencodable(str):
            def encode(self, *args, **kwargs):
                raise AssertionError("should not encode oversized reasoning")
        with self.assertRaises(ProviderError):
            validate_message(done(Unencodable("x" * (MAX_RESPONSE + 1))), preserve_reasoning=True)

    def test_fragmented_sse_keeps_exact_reasoning_and_never_emits_it(self):
        message = call("inspect", "read_file", {"path": "a.py"}, "PRIVATE_REASON 中文🙂")
        for width in (1, 2, 13, 8192):
            displayed, deltas = [], []
            result = complete_stream(Fragmented(wire(message, streaming=True), width), displayed.append, "",
                                     preserve_reasoning=True, on_delta=lambda: deltas.append(True))
            self.assertEqual(result, message)
            self.assertEqual(displayed, [])
            self.assertEqual(deltas, [])  # Silent reasoning does not change visible-text timing.

    def test_streamed_empty_null_and_null_after_text_are_distinguished(self):
        for value in ("", None):
            result = complete_stream(Fragmented(wire(done(value), streaming=True)), None, "", preserve_reasoning=True)
            self.assertEqual(result["reasoning_content"], value)
        raw = sse(chunk({"reasoning_content": "first"}), chunk({"reasoning_content": None}),
                  chunk({"content": "Done"}, "stop"))
        self.assertEqual(complete_stream(Fragmented(raw), None, "", preserve_reasoning=True)["reasoning_content"], "first")

    def test_reasoning_and_visible_text_redact_credentials_without_printing_reasoning(self):
        secret = "fixture-private-credential"
        message = done("PRIVATE " + secret)
        message["content"] = "Visible " + secret
        displayed = []
        result = complete_stream(Fragmented(wire(message, streaming=True), 2), displayed.append, secret, preserve_reasoning=True)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["reasoning_content"], "PRIVATE [REDACTED]")
        self.assertEqual("".join(displayed), "Visible [REDACTED]")

    def test_budget_counts_retained_reasoning_and_cannot_drop_it_to_send(self):
        config = replace(self.config, max_context_bytes=4096, preserve_reasoning=True)
        messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}, done("x" * 5000)]
        original = copy.deepcopy(messages)
        selected, report = prepare_context(config, messages, [], True)
        self.assertFalse(report["within_budget"])
        self.assertEqual(selected[-1]["reasoning_content"], "x" * 5000)
        self.assertEqual(messages, original)
        provider = Provider(config)
        provider.opener = Mock()
        with self.assertRaises(ProviderError):
            provider.complete(messages, [])
        provider.opener.open.assert_not_called()
        self.assertLess(request_size(self.config, messages, []), 4096)

    def test_compaction_preserves_all_reasoning_fields_and_does_not_fold_their_protocol(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]
        for number in range(10):
            history.extend([call("read-" + str(number), "list_files", {}, "PRIVATE_" + str(number) + "x" * 500),
                {"role": "tool", "tool_call_id": "read-" + str(number), "content": '{"entries":[],"truncated":false}'}])
        history.extend({"role": "user", "content": "later"} for _ in range(8))
        original = copy.deepcopy(history)
        config = replace(self.config, preserve_reasoning=True, max_context_bytes=4096)
        projected, report = prepare_context(config, history, [], True)
        self.assertFalse(report["within_budget"])
        self.assertEqual(report["folded_inspections"], [])
        self.assertEqual([item.get("reasoning_content") for item in projected if item["role"] == "assistant"],
                         [item.get("reasoning_content") for item in history if item["role"] == "assistant"])
        self.assertEqual(history, original)

    def test_failed_session_checkpoint_blocks_model_write_when_reasoning_exceeds_saved_budget(self):
        (self.root / "a.py").write_text("before", encoding="utf-8")
        config = replace(self.config, preserve_reasoning=True)
        proposal = call("edit", "edit_file", {"path": "a.py", "old": "before", "new": "after"}, "x" * 10000)
        approval, provider = Mock(return_value=True), Mock(complete=Mock(return_value=proposal))
        with closing(Tools(config, approval)) as tools, closing(SessionStore(self.parent / "budget-sessions", config)) as store:
            agent = Agent(config, provider, tools, lambda _: None, lambda history: store.save("budget", history))
            with patch("sprig_agent.session.MAX_SESSION", 8000), self.assertRaises(SessionError):
                agent.run("Change")
            self.assertFalse(any(item.get("tool_calls") for item in store.load("budget")))
        self.assertEqual(provider.complete.call_count, 1)
        approval.assert_not_called()
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "before")

    def test_fixed_check_marker_projects_to_observations_without_history_mutation(self):
        identity = "sprig-check-" + "a" * 32
        marked = call(identity, "run", {"argv": ["python", "-V"]})
        marked.pop("reasoning_content")
        marked["sprig_check"] = "acceptance"
        result = {"role": "tool", "tool_call_id": identity, "content": '{"approved":true,"returncode":0}'}
        history = [{"role": "system", "content": "system"}, marked, result]
        original = copy.deepcopy(history)
        config = replace(self.config, check_history_format="observations")
        projected = provider_messages(config, history)
        self.assertEqual([item["role"] for item in projected], ["system", "user", "user"])
        self.assertIn("Application fixed-check observation", projected[1]["content"])
        self.assertIs(projected[2]["content"], result["content"])
        self.assertEqual(history, original)
        legacy = json.loads(request_body(self.config, history, []))["messages"]
        self.assertEqual([item["role"] for item in legacy], ["system", "assistant", "tool"])
        self.assertNotIn("sprig_check", legacy[1])
        self.assertEqual(validate_history(history), history)

    def test_marker_without_pair_or_with_wrong_role_function_or_stage_is_rejected(self):
        message = call("sprig-check-" + "b" * 32, "run", {"argv": ["python", "-V"]})
        message["sprig_check"] = "acceptance"
        config = replace(self.config, check_history_format="observations")
        with self.assertRaises(ValueError):
            provider_messages(config, [message])
        for changes in ({"sprig_check": True}, {"role": "user"}, {"sprig_check": "other"}, {"content": "spoof"}):
            with self.assertRaises(ValueError):
                application_check({**message, **changes})
        wrong = copy.deepcopy(message)
        wrong["tool_calls"][0]["function"]["name"] = "edit_file"
        with self.assertRaises(SessionError):
            validate_history([{"role": "system", "content": "system"}, wrong])

    def test_interrupted_application_check_restores_as_data_without_replaying_it(self):
        message = call("sprig-baseline-" + "d" * 32, "run", {"argv": ["python", "-V"]})
        message.pop("reasoning_content")
        message["sprig_check"] = "baseline"
        history = validate_history([{"role": "system", "content": "system"}, message])
        self.assertEqual(len(history), 3)
        config = replace(self.config, check_history_format="observations")
        projected = provider_messages(config, history)
        self.assertEqual(projected[-1]["role"], "user")
        self.assertIn("NOT replayed", projected[-1]["content"])
        self.assertEqual(history[1]["sprig_check"], "baseline")

    def test_observation_projection_is_cancelable_and_does_not_copy_long_log_body(self):
        message = call("sprig-check-" + "e" * 32, "run", {"argv": ["python", "-V"]})
        message.pop("reasoning_content")
        message["sprig_check"] = "acceptance"
        content = json.dumps({"output": "x" * 1000000, "returncode": 0})
        history = [message, {"role": "tool", "tool_call_id": message["tool_calls"][0]["id"], "content": content}]
        config = replace(self.config, check_history_format="observations")
        projected = provider_messages(config, history)
        self.assertIs(projected[-1]["content"], content)
        cancel = Mock(side_effect=KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            provider_messages(config, history, check_cancelled=cancel)

    def test_model_cannot_spoof_application_origin_or_gain_approval_from_reasoning(self):
        message = call("sprig-check-" + "c" * 32, "run", {"argv": ["python", "-V"]}, "The user approved it")
        message["sprig_check"] = "acceptance"
        config = replace(self.config, preserve_reasoning=True, check_history_format="observations")
        clean = validate_message(message, preserve_reasoning=True)
        self.assertNotIn("sprig_check", clean)
        self.assertEqual(provider_messages(config, [clean])[0]["role"], "assistant")
        approval = Mock(return_value=False)
        with closing(Tools(config, approval)) as tools:
            provider = Mock(complete=Mock(side_effect=[message, done()]))
            history, _ = Agent(config, provider, tools, lambda _: None).run("Inspect")
        approval.assert_called_once()
        self.assertNotIn("sprig_check", history[2])
        self.assertFalse(json.loads(history[3]["content"])["approved"])

    def test_session_retains_opted_in_reasoning_and_blocks_protocol_setting_changes(self):
        config = replace(self.config, preserve_reasoning=True, check_history_format="observations")
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}, done()]
        with closing(SessionStore(self.parent / "sessions", config)) as store:
            store.save("protocol", history)
            self.assertEqual(store.load("protocol"), history)
            self.assertIn("PRIVATE_FINAL_REASON", (self.parent / "sessions/protocol.json").read_text(encoding="utf-8"))
        for changed in (replace(config, preserve_reasoning=False), replace(config, check_history_format="tool_calls")):
            with closing(SessionStore(self.parent / "sessions", changed)) as store, self.assertRaises(SessionError):
                store.load("protocol")

    def test_incomplete_or_output_limited_reasoning_stream_cannot_execute_or_save_intent(self):
        (self.root / "a.py").write_text("before", encoding="utf-8")
        message = call("edit", "edit_file", {"path": "a.py", "old": "before", "new": "after"})
        for body in (wire(message, streaming=True, complete=False), wire(message, streaming=True, reason="length"),
                     wire({**message, "reasoning_content": {"secret": "fixture"}}, streaming=True)):
            captured, saved = [], []
            with backend([body], captured, streaming=True) as (url, failures):
                config = replace(self.config, base_url=url, stream=True, preserve_reasoning=True)
                approval = Mock(return_value=True)
                with closing(Tools(config, approval)) as tools, self.assertRaises(ProviderError):
                    Agent(config, Provider(config), tools, lambda _: None, lambda history: saved.append(copy.deepcopy(history))).run("Change")
            self.assertEqual(failures, [])
            self.assertEqual(len(captured), 1)
            approval.assert_not_called()
            self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "before")
            self.assertFalse(any(message.get("tool_calls") for history in saved for message in history))
            self.assertNotIn("PRIVATE_PROTOCOL_REASON", json.dumps(saved))

    def test_strict_fixture_reproduces_missing_reasoning_failure_without_retry_or_writes(self):
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        captured = []
        with backend([call("read", "read_file", {"path": "a.py"}), done()], captured, strict=True) as (url, failures):
            config = replace(self.config, base_url=url)
            approval = Mock()
            with closing(Tools(config, approval)) as tools, self.assertRaises(ProviderError) as error:
                Agent(config, Provider(config), tools, lambda _: None).run("Inspect")
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(failures, ["missing reasoning_content"])
        self.assertEqual(len(captured), 2)
        approval.assert_not_called()

    def test_cancelled_reasoning_response_does_not_save_or_execute_tool_intent(self):
        (self.root / "a.py").write_text("before", encoding="utf-8")
        cancel, captured, saved = threading.Event(), [], []
        def response_after_cancel(payload):
            cancel.set()
            return call("edit", "edit_file", {"path": "a.py", "old": "before", "new": "after"})
        with backend([response_after_cancel], captured, streaming=True) as (url, failures):
            config = replace(self.config, base_url=url, preserve_reasoning=True, stream=True)
            approval = Mock(return_value=True)
            with closing(Tools(config, approval)) as tools, self.assertRaises(KeyboardInterrupt):
                Agent(config, Provider(config), tools, lambda _: None, lambda messages: saved.append(copy.deepcopy(messages)),
                      cancel_event=cancel).run("Change")
        self.assertEqual(failures, [])
        self.assertEqual(len(captured), 1)
        self.assertNotIn("PRIVATE_PROTOCOL_REASON", json.dumps(saved))
        self.assertFalse(any(message.get("tool_calls") for history in saved for message in history))
        approval.assert_not_called()
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "before")

    def test_midrun_guidance_discards_stale_reasoning_and_tools(self):
        config = replace(self.config, preserve_reasoning=True, max_steps=2)
        saved, sent, steered = [], [], False
        def complete(messages, tools):
            sent.append(copy.deepcopy(messages))
            return call("stale", "edit_file", {"path": "stale.py", "old": "", "new": "wrong"}, "PRIVATE_STALE_REASON") if len(sent) == 1 else done("PRIVATE_CURRENT_REASON")
        def guidance():
            nonlocal steered
            if len(sent) == 1 and not steered:
                steered = True
                return ["Use new requirements"]
            return []
        approval = Mock()
        with closing(Tools(config, approval)) as tools:
            history, complete = Agent(config, Mock(complete=Mock(side_effect=complete)), tools, lambda _: None,
                lambda messages: saved.append(copy.deepcopy(messages)), receive_input=guidance).run("Original task")
        self.assertTrue(complete)
        self.assertEqual(sent[-1][-1]["content"], "Use new requirements")
        self.assertNotIn("PRIVATE_STALE_REASON", json.dumps([history, saved, sent[-1]]))
        self.assertEqual(history[-1]["reasoning_content"], "PRIVATE_CURRENT_REASON")
        approval.assert_not_called()
        self.assertFalse((self.root / "stale.py").exists())

    def test_strict_http_and_sse_repair_stage_final_and_next_turn_preserve_all_model_metadata(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
                captured, approvals, displayed = [], [], []
                def edit(payload):
                    result = json.loads(next(message["content"] for message in reversed(payload["messages"]) if message["role"] == "tool"))
                    return call("edit", "edit_lines", {"path": "a.py", "read_id": result["read_id"],
                        "edits": [{"start_line": 1, "end_line": 1, "new": "value = 2\n"}]}, "PRIVATE_REASON_B")
                messages = [call("read", "read_file", {"path": "a.py", "line_numbers": True}, "PRIVATE_REASON_A"),
                            edit, done("PRIVATE_REASON_C"), done("PRIVATE_REASON_D")]
                with backend(messages, captured, streaming=streaming, strict=True) as (url, failures):
                    config = replace(self.config, base_url=url, preserve_reasoning=True, check_history_format="observations",
                        stream=streaming, max_steps=3, max_context_bytes=32768, check_before_task=True, check_after_edit=True,
                        checks=((sys.executable, "-I", "-B", "-c", "from pathlib import Path; assert Path('a.py').read_text().strip()=='value = 2'"),))
                    with closing(Tools(config, lambda prompt: approvals.append(prompt) or True)) as tools, \
                            closing(SessionStore(self.parent / ("sessions-" + str(streaming)), config)) as store:
                        agent = Agent(config, Provider(config), tools, displayed.append, lambda history: store.save("turn", history), emit_delta=displayed.append)
                        history, complete = agent.run("Change value to 2")
                        self.assertTrue(complete)
                        self.assertEqual(agent.edit_check_status, "passed")
                        history, complete = agent.run("Verify again", history)
                        self.assertTrue(complete)
                        self.assertEqual(store.load("turn"), history)
                        self.assertEqual(sum("sprig_check" in message for message in history), 5)
                self.assertEqual(failures, [])
                self.assertEqual(len(captured), 4)
                self.assertTrue(all(size <= 32768 for _, size in captured))
                self.assertEqual(len(approvals), 6)
                self.assertNotIn("PRIVATE_REASON", "".join(displayed))
                forwarded = [message.get("reasoning_content") for message in captured[-1][0]["messages"] if message["role"] == "assistant"]
                self.assertEqual(forwarded, ["PRIVATE_REASON_A", "PRIVATE_REASON_B", "PRIVATE_REASON_C"])
                self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "value = 2\n")


class ReasoningSettingsTests(unittest.TestCase):
    setUp = settings_fixtures.SettingsTests.setUp
    write = settings_fixtures.SettingsTests.write
    resolve = settings_fixtures.SettingsTests.resolve
    invoke = settings_fixtures.SettingsTests.invoke

    def test_provider_flags_profile_selection_cli_override_and_public_sources(self):
        self.write('provider="x"\n[providers.x]\nmodel="fixture"\nbase_url="http://127.0.0.1:1/v1"\n'
            'preserve_reasoning=true\ncheck_history_format="observations"\n'
            '[providers.x.generation.extra_body.thinking]\ntype="enabled"\n')
        config, details = self.resolve(load_settings(self.path))
        self.assertTrue(config.preserve_reasoning)
        self.assertEqual(config.check_history_format, "observations")
        self.assertEqual(config.generation_options(), {"thinking": {"type": "enabled"}})
        visible = public_config(config, details)
        self.assertTrue(visible["transport"]["preserve_reasoning"])
        self.assertEqual(visible["setting_sources"]["preserve_reasoning"], "provider")
        status, output, _ = self.invoke(["--config", str(self.path), "--cli", "--no-preserve-reasoning", "--check-history-format", "tool_calls", "--show-config"])
        self.assertEqual(status, 0)
        visible = json.loads(output)
        self.assertFalse(visible["transport"]["preserve_reasoning"])
        self.assertEqual(visible["setting_sources"]["preserve_reasoning"], "CLI")

    def test_invalid_flags_and_project_overrides_are_rejected_before_provider(self):
        for value in (None, 0, 1, "yes", []):
            with self.assertRaises(ValueError):
                Config(self.root, "fixture", preserve_reasoning=value)
        for value in (None, True, "other", []):
            with self.assertRaises(ValueError):
                Config(self.root, "fixture", check_history_format=value)
        project = self.write('[generation]\npreserve_reasoning=true\n', self.parent / "project.toml")
        with self.assertRaises(ValueError):
            load_settings().with_project(project)

    def test_profile_selects_provider_protocol_and_offline_doctor_never_contacts_it(self):
        self.write('provider="plain"\n[providers.plain]\nmodel="fixture"\nbase_url="http://127.0.0.1:1/v1"\n'
            '[providers.reasoned]\nmodel="fixture"\nbase_url="http://127.0.0.1:1/v1"\n'
            'preserve_reasoning=true\ncheck_history_format="observations"\n[profiles.reasoned]\nprovider="reasoned"\n')
        config, _ = self.resolve(load_settings(self.path), profile="reasoned")
        self.assertTrue(config.preserve_reasoning)
        self.assertEqual(config.check_history_format, "observations")
        with patch.object(cli, "Provider") as provider:
            status, output, _ = self.invoke(["--config", str(self.path), "--cli", "--profile", "reasoned", "--doctor"])
        self.assertEqual(status, 0)
        self.assertFalse(json.loads(output)["network_contacted"])
        provider.assert_not_called()


class ReasoningTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_reasoning_is_private_saved_metadata_and_never_rendered_on_run_or_restore(self):
        asyncio.get_running_loop().set_debug(False)
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "workspace"
            root.mkdir()
            config = Config(root, "fixture", base_url="http://localhost/v1", preserve_reasoning=True, read_only=True)
            provider = Mock(complete=Mock(return_value=done("PRIVATE_TUI_REASON")))
            app = SprigApp(config, provider=provider, session_dir=parent / "sessions", save_session="private")
            try:
                async with app.run_test(size=(110, 36)) as pilot:
                    editor = app.query_one("#task", TextArea)
                    editor.load_text("Explain")
                    editor.focus()
                    await pilot.press("enter")
                    for _ in range(300):
                        if not app.busy:
                            break
                        await pilot.pause(0.02)
                    self.assertFalse(app.busy)
                    self.assertIn("Done", app.transcript)
                    self.assertNotIn("PRIVATE_TUI_REASON", app.transcript)
                    self.assertIn("一并保存", app.transcript)
                    saved = (parent / "sessions/private.json").read_text(encoding="utf-8")
                    self.assertIn("PRIVATE_TUI_REASON", saved)
            finally:
                app.close()
            provider.complete.reset_mock()
            restored = SprigApp(config, provider=provider, session_dir=parent / "sessions", resume="private")
            try:
                async with restored.run_test(size=(110, 36)) as pilot:
                    await pilot.pause()
                    self.assertNotIn("PRIVATE_TUI_REASON", restored.transcript)
                    self.assertEqual(restored.messages[-1]["reasoning_content"], "PRIVATE_TUI_REASON")
                    provider.complete.assert_not_called()
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
