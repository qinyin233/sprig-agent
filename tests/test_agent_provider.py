"""Offline boundary and control-flow tests; no API key or installation needed."""

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import http.client
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent import cli
from sprig_agent.agent import Agent, SYSTEM_PROMPT
from sprig_agent.config import Config, endpoint, redact, terminal_text
from sprig_agent.provider import (
    MAX_RESPONSE, MAX_TOOL_CALLS, NoRedirect, Provider, ProviderError,
    validate_message,
)
from sprig_agent.tools import TOOL_SCHEMAS


def call(call_id="call-1", name="read_file", arguments='{"path": "a.py"}'):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": arguments}}


def assistant(content=None, calls=None):
    message = {"role": "assistant", "content": content}
    if calls is not None:
        message["tool_calls"] = calls
    return message


def envelope(message=None, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason,
                         "message": message if message is not None else assistant("Done")}]}


class ConfigCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def config(self, **overrides):
        values = {"workdir": self.root, "model": "offline-test"}
        values.update(overrides)
        return Config(**values)


class ConfigTests(ConfigCase):
    def test_normalizes_workspace_endpoint_and_binding_without_secret(self):
        config = self.config(workdir=self.root / ".", base_url="https://api.example/v1///",
                             api_key="known-secret")
        self.assertEqual(config.workdir, self.root.resolve())
        self.assertEqual(config.base_url, "https://api.example/v1")
        self.assertEqual(config.binding(), {
            "workdir": str(self.root.resolve()), "model": "offline-test",
            "base_url": "https://api.example/v1", "command_output_limit": 1048576})
        self.assertNotIn("known-secret", repr(config))
        self.assertNotIn("api_key", config.binding())

    def test_workdir_must_exist_and_be_a_directory(self):
        with self.assertRaises(OSError):
            self.config(workdir=self.root / "missing")
        file = self.root / "plain.txt"
        file.write_text("text", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "directory"):
            self.config(workdir=file)

    def test_numeric_boundaries(self):
        for values in ({"max_steps": 1, "timeout": 0.001, "max_output": 256},
                       {"max_steps": 100, "timeout": 120, "max_output": 1000000}):
            with self.subTest(values=values):
                self.config(**values)
        for field, values in {
            "max_steps": (0, 101, True),
            "timeout": (0, -1, 121, float("nan"), float("inf")),
            "max_output": (255, 1000001),
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    self.config(**{field: value})

    def test_rejects_missing_invalid_model_and_key_controls(self):
        for model in ("", "x" * 201, "model\nname", "model\x00name"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                self.config(model=model)
        for key in ("secret\nheader", "secret\rheader", "secret\x00value"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.config(api_key=key)

    def test_http_allowed_only_for_explicit_loopback_hosts(self):
        for base in ("http://localhost:8080/v1/", "http://127.0.0.1:1234", "http://[::1]:8000/v1"):
            with self.subTest(base=base):
                self.assertEqual(endpoint(base), base.rstrip("/"))
        for base in ("http://example.com", "http://127.0.0.2", "http://localhost.example.com",
                     "ftp://localhost/v1", "file:///tmp/api", "https:///missing-host"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                endpoint(base)

    def test_endpoint_rejects_credentials_query_fragment_controls_and_bad_ports(self):
        for base in ("https://user:pass@example.com/v1", "https://user@example.com/v1",
                     "https://example.com/v1?key=x", "https://example.com/v1#part",
                     "https://example.com/with space", "https://example.com/\npath",
                     "https://example.com:invalid", "https://example.com:99999", None):
            with self.subTest(base=base), self.assertRaises(ValueError):
                endpoint(base)

    def test_redaction_recurses_without_mutating_source_or_claiming_unknown_secrets(self):
        source = {"secret-key": ["prefix secret-key suffix", {"nested": "other-value"}], "count": 2}
        original = copy.deepcopy(source)
        self.assertEqual(redact(source, "secret-key"), {
            "[REDACTED]": ["prefix [REDACTED] suffix", {"nested": "other-value"}], "count": 2})
        self.assertEqual(source, original)
        self.assertEqual(redact(source, ""), source)

    def test_terminal_controls_and_bidi_are_visible_but_text_is_preserved(self):
        self.assertEqual(terminal_text("hello\n世界\x1b[31m\r\t\x00\u202e"),
                         "hello\n世界\\u001b[31m\\u000d\\u0009\\u0000\\u202e")
        self.assertEqual(terminal_text(42), "42")


class MessageValidationTests(unittest.TestCase):
    def test_text_and_calls_are_normalized(self):
        message = assistant("Inspecting", [dict(call(), extra="discard")])
        message["extra"] = "discard"
        self.assertEqual(validate_message(message), assistant("Inspecting", [call()]))

    def test_refusal_becomes_text_when_no_content_or_calls(self):
        self.assertEqual(validate_message({"role": "assistant", "content": None, "refusal": "No"}),
                         assistant("No"))

    def test_null_tool_calls_are_absent_but_reasoning_only_is_not_completion(self):
        self.assertEqual(validate_message({"role": "assistant", "content": "Done", "tool_calls": None}),
                         assistant("Done"))
        self.assertEqual(validate_message({"role": "assistant", "content": None, "tool_calls": None, "refusal": "No"}),
                         assistant("No"))
        with self.assertRaises(ProviderError):
            validate_message({"role": "assistant", "content": None, "tool_calls": None, "reasoning_content": "private"})

    def test_rejects_nontext_empty_and_wrong_role_messages(self):
        for message in (None, [], "text", {"role": "user", "content": "text"},
                        assistant([]), assistant({"text": "x"}), assistant(42),
                        assistant(""), assistant(None),
                        {"role": "assistant", "refusal": {"text": "No"}}):
            with self.subTest(message=message), self.assertRaises(ProviderError):
                validate_message(message)

    def test_rejects_invalid_or_duplicate_tool_calls(self):
        invalid = [None, "call", {}, call(call_id=""), call(call_id=12),
                   dict(call(), type="other"), dict(call(), function=None),
                   call(name=None), call(arguments={})]
        for item in invalid:
            with self.subTest(call=item), self.assertRaises(ProviderError):
                validate_message(assistant(calls=[item]))
        for calls in (None, {}, "calls", [call(), call()],
                      [call(str(i)) for i in range(MAX_TOOL_CALLS + 1)]):
            with self.subTest(calls=calls), self.assertRaises(ProviderError):
                validate_message({"role": "assistant", "content": None, "tool_calls": calls})

    def test_maximum_tool_calls_is_allowed(self):
        calls = [call(str(i)) for i in range(MAX_TOOL_CALLS)]
        self.assertEqual(validate_message(assistant(calls=calls)), assistant(calls=calls))


class ProviderTests(ConfigCase):
    def provider(self, raw=None, error=None, **config):
        opener = Mock()
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = raw if raw is not None else json.dumps(envelope()).encode()
        opener.open.side_effect = error
        opener.open.return_value = response
        with patch("sprig_agent.provider.urllib.request.build_opener", return_value=opener) as build:
            provider = Provider(self.config(**config))
        self.assertIsInstance(build.call_args.args[0], NoRedirect)
        return provider, opener, response

    def test_posts_chat_completions_body_headers_timeout_and_redacted_messages(self):
        key = "known-api-key"
        provider, opener, response = self.provider(api_key=key, timeout=7,
                                                   base_url="https://api.example/v1/")
        messages = [{"role": "user", "content": "do not send " + key}]
        original = copy.deepcopy(messages)
        result = provider.complete(messages, TOOL_SCHEMAS)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + key)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), {
            "model": "offline-test", "messages": [{"role": "user", "content": "do not send [REDACTED]"}],
            "tools": TOOL_SCHEMAS, "tool_choice": "auto"})
        opener.open.assert_called_once_with(request, timeout=7)
        response.read.assert_called_once_with(MAX_RESPONSE + 1)
        response.__exit__.assert_called_once()
        self.assertEqual(messages, original)
        self.assertEqual(result, assistant("Done"))

    def test_no_authorization_header_for_keyless_local_provider(self):
        provider, opener, _ = self.provider(base_url="http://localhost:8080/v1")
        provider.complete([], [])
        self.assertIsNone(opener.open.call_args.args[0].get_header("Authorization"))

    def test_nullable_tool_calls_response_keeps_only_final_text(self):
        message = {"role": "assistant", "content": "Done", "tool_calls": None,
                   "reasoning_content": "private reasoning"}
        provider, opener, response = self.provider(raw=json.dumps(envelope(message)).encode())
        displayed = []
        self.assertEqual(provider.complete([], [], on_text=displayed.append), assistant("Done"))
        self.assertNotIn("private reasoning", json.dumps(displayed))
        self.assertEqual(opener.open.call_count, 1)
        response.read.assert_called_once_with(MAX_RESPONSE + 1)

    def test_known_key_is_redacted_from_response_text_and_tool_arguments(self):
        message = assistant("key=secret", [call(arguments='{"path":"secret.txt"}')])
        provider, _, _ = self.provider(raw=json.dumps(envelope(message, "tool_calls")).encode(), api_key="secret")
        result = provider.complete([], [])
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(result["content"], "key=[REDACTED]")
        self.assertEqual(result["tool_calls"][0]["function"]["arguments"], '{"path":"[REDACTED].txt"}')

    def test_invalid_json_and_response_shapes_raise_provider_error(self):
        invalid = [b"not-json", b"\xff", b"null", b"[]", b"{}", b'{"choices": []}',
                   b'{"choices": null}', b'{"choices": [null]}', b'{"choices": [42]}',
                   b'{"choices": [[]]}', b'{"choices": [{"finish_reason":"stop"}]}',
                   json.dumps(envelope(assistant([{"text": "image"}]))).encode()]
        for raw in invalid:
            with self.subTest(raw=raw):
                provider, _, _ = self.provider(raw=raw)
                with self.assertRaises(ProviderError):
                    provider.complete([], [])

    def test_deeply_nested_response_json_becomes_provider_error(self):
        raw = ("[" * 2000 + "0" + "]" * 2000).encode()
        provider, opener, _ = self.provider(raw=raw)
        # JSON's native recursion threshold differs between Python versions.
        # Inject the parser failure without changing the process recursion limit.
        with patch("sprig_agent.provider.json.loads", side_effect=RecursionError()) as parse:
            with self.assertRaises(ProviderError):
                provider.complete([], [])
        parse.assert_called_once_with(raw)
        self.assertEqual(opener.open.call_count, 1)

    def test_real_hostile_nested_response_becomes_provider_error(self):
        raw = ("[" * 50000 + "0" + "]" * 50000).encode()
        self.assertLess(len(raw), MAX_RESPONSE)
        provider, opener, _ = self.provider(raw=raw)
        with self.assertRaises(ProviderError):
            provider.complete([], [])
        self.assertEqual(opener.open.call_count, 1)

    def test_incomplete_and_unknown_finish_reasons_fail_closed(self):
        for reason in ("length", "content_filter", "function_call", None, "unknown"):
            with self.subTest(reason=reason):
                provider, _, _ = self.provider(raw=json.dumps(envelope(finish_reason=reason)).encode())
                with self.assertRaisesRegex(ProviderError, "complete response"):
                    provider.complete([], [])

    def test_response_size_is_bounded(self):
        provider, _, response = self.provider(raw=b"x" * (MAX_RESPONSE + 1))
        with self.assertRaisesRegex(ProviderError, "2 MiB"):
            provider.complete([], [])
        response.read.assert_called_once_with(MAX_RESPONSE + 1)

    def test_http_error_does_not_read_or_log_server_body_or_reason(self):
        body = Mock()
        error = urllib.error.HTTPError("https://api.example", 401, "secret in reason", {}, body)
        provider, opener, _ = self.provider(error=error, api_key="secret")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(ProviderError) as caught:
            provider.complete([], [])
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        body.read.assert_not_called()
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        self.assertEqual(opener.open.call_count, 1)

    def test_network_timeout_and_os_errors_are_sanitized_and_not_retried(self):
        for error in (urllib.error.URLError("secret"), TimeoutError("secret"), OSError("secret")):
            with self.subTest(error=type(error).__name__):
                provider, opener, _ = self.provider(error=error)
                with self.assertRaisesRegex(ProviderError, "no automatic retry") as caught:
                    provider.complete([], [])
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(opener.open.call_count, 1)

    def test_http_protocol_errors_are_sanitized_without_retry(self):
        key = "configured-fake-key"
        for phase, error in (("open", http.client.BadStatusLine("unexpected " + key)),
                             ("read", http.client.IncompleteRead(key.encode(), 100))):
            with self.subTest(error=type(error).__name__):
                provider, opener, response = self.provider(api_key=key)
                if phase == "open":
                    opener.open.side_effect = error
                else:
                    response.read.side_effect = error
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr), self.assertRaises(ProviderError) as caught:
                    provider.complete([], [])
                self.assertNotIn(key, str(caught.exception))
                self.assertIn("no automatic retry", str(caught.exception))
                self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
                self.assertEqual(opener.open.call_count, 1)
                if phase == "read":
                    response.__exit__.assert_called_once()
                else:
                    response.read.assert_not_called()

    def test_redirect_handler_refuses_to_forward_request(self):
        request = urllib.request.Request("https://original.example", headers={"Authorization": "Bearer secret"})
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), self.assertRaisesRegex(ProviderError, "redirects are disabled"):
                NoRedirect().redirect_request(request, None, status, "moved", {}, "https://other.example")
        provider, opener, _ = self.provider(error=ProviderError("HTTP redirects are disabled"))
        with self.assertRaisesRegex(ProviderError, "redirects are disabled"):
            provider.complete([], [])
        self.assertEqual(opener.open.call_count, 1)


class AgentTests(ConfigCase):
    def runner(self, replies, results=(), **config):
        provider, tools = Mock(), Mock()
        self.requests, self.checkpoints, self.emitted = [], [], []
        replies = iter(replies)

        def complete(messages, schemas):
            self.requests.append(copy.deepcopy(messages))
            self.assertIs(schemas, agent.schemas)
            return next(replies)

        provider.complete.side_effect = complete
        tools.execute.side_effect = list(results)
        agent = Agent(self.config(**config), provider, tools, self.emitted.append,
                      lambda messages: self.checkpoints.append(copy.deepcopy(messages)))
        return agent, provider, tools

    def test_multi_step_loop_tool_results_and_checkpoints(self):
        agent, provider, tools = self.runner([
            assistant("Reading", [call("read"), call("list", "list_files", "{}")]),
            assistant(calls=[call("run", "run", '{"argv":["python","-V"]}')]),
            assistant("Verified"),
        ], [{"content": "pass"}, {"entries": []}, {"exit_code": 0, "stdout": "Python"}])
        messages, complete = agent.run("Check this project")
        self.assertTrue(complete)
        self.assertEqual(provider.complete.call_count, 3)
        self.assertEqual([args.args for args in tools.execute.call_args_list], [
            ("read_file", {"path": "a.py"}), ("list_files", {}), ("run", {"argv": ["python", "-V"]})])
        self.assertEqual([len(history) for history in self.checkpoints], [2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(self.checkpoints[0][0], {"role": "system", "content": SYSTEM_PROMPT})
        self.assertEqual([m["role"] for m in self.requests[1]], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["read", "list", "run"])
        self.assertIn('"exit_code": 0', "\n".join(self.emitted))
        self.assertEqual(self.emitted[-1], "Verified")

    def test_rejected_actions_and_tool_errors_are_returned_to_model(self):
        agent, _, _ = self.runner([
            assistant(calls=[call("reject", "edit_file", "{}"), call("bad", "missing_tool", "{}")]),
            assistant("No change made"),
        ], [{"approved": False}, {"error": "unknown tool"}])
        messages, complete = agent.run("Try an edit")
        self.assertTrue(complete)
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        self.assertEqual(results, [{"approved": False}, {"error": "unknown tool"}])
        self.assertIn("[rejected] No action taken", self.emitted)
        self.assertIn("[tool error] unknown tool", self.emitted)
        self.assertEqual(self.requests[1][-2:], messages[3:5])

    def test_invalid_json_arguments_do_not_execute_and_become_error_result(self):
        agent, _, tools = self.runner([assistant(calls=[call(arguments="{broken")]), assistant("Stopped")])
        messages, complete = agent.run("Read")
        self.assertTrue(complete)
        tools.execute.assert_not_called()
        self.assertEqual(json.loads(messages[-2]["content"]), {"error": "tool arguments are not valid JSON"})

    def test_deeply_nested_argument_json_becomes_error_without_execution(self):
        arguments = "[" * 2000 + "0" + "]" * 2000
        agent, _, tools = self.runner([
            assistant(calls=[call(arguments=arguments)]), assistant("Stopped")
        ])
        with patch("sprig_agent.agent.json.loads", side_effect=RecursionError()) as parse:
            messages, complete = agent.run("Read")
        parse.assert_called_once_with(arguments)
        self.assertTrue(complete)
        tools.execute.assert_not_called()
        self.assertEqual(json.loads(messages[-2]["content"]),
                         {"error": "tool arguments are not valid JSON"})
        self.assertEqual(self.requests[1][-1], messages[-2])

    def test_real_hostile_nested_arguments_become_error_without_execution(self):
        arguments = "[" * 50000 + "0" + "]" * 50000
        agent, _, tools = self.runner([
            assistant(calls=[call(arguments=arguments)]), assistant("Stopped")
        ])
        messages, complete = agent.run("Read")
        self.assertTrue(complete)
        tools.execute.assert_not_called()
        self.assertEqual(json.loads(messages[-2]["content"]),
                         {"error": "tool arguments are not valid JSON"})
        self.assertEqual(self.requests[1][-1], messages[-2])

    def test_nonobject_arguments_are_passed_to_tool_validation(self):
        agent, _, tools = self.runner([
            assistant(calls=[call(arguments="null")]), assistant("Stopped")
        ], [{"error": "tool arguments must be an object"}])
        messages, complete = agent.run("Read")
        self.assertTrue(complete)
        tools.execute.assert_called_once_with("read_file", None)
        self.assertIn("must be an object", messages[-2]["content"])

    def test_max_steps_returns_incomplete_with_last_tool_result(self):
        agent, provider, tools = self.runner([assistant(calls=[call()])], [{"content": "data"}], max_steps=1)
        messages, complete = agent.run("Read")
        self.assertFalse(complete)
        self.assertEqual(provider.complete.call_count, 1)
        tools.execute.assert_called_once()
        self.assertEqual(messages[-1]["role"], "tool")
        self.assertEqual(self.checkpoints[-1], messages)
        self.assertIn("Maximum model steps reached", self.emitted[-1])

    def test_request_budget_advances_and_resets_without_polluting_saved_history(self):
        agent, _, _ = self.runner([
            assistant(calls=[call()]), assistant("Findings"), assistant("Updated findings"),
        ], [{"content": "evidence"}], max_steps=2)
        messages, complete = agent.run("Review the file")
        self.assertTrue(complete)
        self.assertIn("model request 1 of 2; 1 further", self.requests[0][0]["content"])
        self.assertIn("model request 2 of 2; 0 further", self.requests[1][0]["content"])
        self.assertTrue(agent.run("Continue the review", messages)[1])
        self.assertIn("model request 1 of 2; 1 further", self.requests[2][0]["content"])
        for request in self.requests:
            self.assertEqual(request[0]["content"].count("Current run:"), 1)
        for history in [messages, *self.checkpoints]:
            self.assertEqual(history[0]["content"], SYSTEM_PROMPT)
            self.assertNotIn("Current run:", history[0]["content"])

    def test_duplicate_ids_in_one_response_never_execute(self):
        agent, _, tools = self.runner([assistant(calls=[call(), call()])])
        with self.assertRaisesRegex(ProviderError, "duplicate"):
            agent.run("Read")
        tools.execute.assert_not_called()
        self.assertEqual(len(self.checkpoints), 1)

    def test_reused_id_in_later_step_is_not_replayed(self):
        agent, _, tools = self.runner([assistant(calls=[call()]), assistant(calls=[call()])], [{"content": "data"}])
        with self.assertRaisesRegex(ProviderError, "reused"):
            agent.run("Read")
        tools.execute.assert_called_once()
        self.assertEqual(self.checkpoints[-1][-1]["role"], "tool")

    def test_resumed_historical_ids_cannot_be_replayed(self):
        history = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "Read"},
                   assistant(calls=[call()]), {"role": "tool", "tool_call_id": "call-1", "content": "{}"}]
        original = copy.deepcopy(history)
        agent, _, tools = self.runner([assistant(calls=[call()])])
        with self.assertRaisesRegex(ProviderError, "reused"):
            agent.run(messages=history)
        tools.execute.assert_not_called()
        self.assertEqual(history, original)

    def test_resume_uses_supplied_history_and_optional_new_task(self):
        history = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": "Old task"}]
        agent, _, tools = self.runner([assistant("Continued")])
        messages, complete = agent.run("New task", history)
        self.assertTrue(complete)
        self.assertIs(messages, history)
        self.assertEqual(self.requests[0][-1], {"role": "user", "content": "New task"})
        tools.execute.assert_not_called()

    def test_new_session_requires_task_before_request_or_checkpoint(self):
        agent, provider, _ = self.runner([])
        with self.assertRaisesRegex(ValueError, "task is required"):
            agent.run()
        provider.complete.assert_not_called()
        self.assertEqual(self.checkpoints, [])

    def test_checkpoint_intent_precedes_action_and_failure_blocks_action(self):
        agent, _, tools = self.runner([assistant(calls=[call()])], [{"content": "data"}])
        def checkpoint(messages):
            if messages[-1]["role"] == "assistant":
                tools.execute.assert_not_called()
                raise OSError("disk full")
        agent.checkpoint = checkpoint
        with self.assertRaisesRegex(OSError, "disk full"):
            agent.run("Read")
        tools.execute.assert_not_called()

    def test_interrupted_tool_leaves_checkpointed_intent_without_fake_result(self):
        agent, _, tools = self.runner([assistant(calls=[call()])])
        tools.execute.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            agent.run("Read")
        self.assertEqual(self.checkpoints[-1][-1], assistant(calls=[call()]))
        self.assertEqual(len(self.checkpoints), 2)

    def test_known_key_is_removed_from_task_assistant_and_saved_results(self):
        agent, _, _ = self.runner([
            assistant("secret", [call()]), assistant("Done")
        ], [{"content": "secret"}], api_key="secret")
        messages, complete = agent.run("Inspect secret")
        self.assertTrue(complete)
        self.assertNotIn("secret", json.dumps(messages))
        self.assertNotIn("secret", json.dumps(self.checkpoints))
        self.assertNotIn("secret", "\n".join(self.emitted))
        self.assertIn("[REDACTED]", self.emitted)


class ApprovalTests(unittest.TestCase):
    def approve(self, response=None, interactive=True, error=None):
        stdin = Mock()
        stdin.isatty.return_value = interactive
        stderr, stdout = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, "stdin", stdin), patch("builtins.input", return_value=response, side_effect=error) as prompt, \
                redirect_stderr(stderr), redirect_stdout(stdout):
            result = cli.approve("Action\x1b[31m\u202e")
        return result, prompt, stderr.getvalue()

    def test_only_explicit_yes_approves(self):
        for response in ("y", "Y", "yes", " YES "):
            with self.subTest(response=response):
                self.assertTrue(self.approve(response)[0])
        for response in ("", " ", "n", "no", "true", "yep", "yes please"):
            with self.subTest(response=response):
                self.assertFalse(self.approve(response)[0])

    def test_eof_defaults_to_no(self):
        self.assertFalse(self.approve(error=EOFError())[0])

    def test_interrupt_propagates_to_stop_the_cli(self):
        with self.assertRaises(KeyboardInterrupt):
            self.approve(error=KeyboardInterrupt())

    def test_noninteractive_input_rejects_without_reading(self):
        approved, prompt, stderr = self.approve("yes", interactive=False)
        self.assertFalse(approved)
        prompt.assert_not_called()
        self.assertIn("Non-interactive input: action rejected", stderr)

    def test_confirmation_escapes_untrusted_terminal_controls(self):
        _, _, stderr = self.approve("")
        self.assertNotIn("\x1b", stderr)
        self.assertNotIn("\u202e", stderr)
        self.assertIn("\\u001b", stderr)
        self.assertIn("\\u202e", stderr)


class CLITests(ConfigCase):
    def invoke(self, args=(), env=None, complete=True, error=None, platform="posix"):
        stdout, stderr = io.StringIO(), io.StringIO()
        # Replace cli's os reference, rather than os.name globally: pathlib must
        # retain its real host platform, including when tests run on Windows.
        fake_os = SimpleNamespace(name=platform, environ=env or {})
        with patch.object(cli, "os", fake_os), patch.object(cli, "Tools") as tools, \
                patch.object(cli, "Provider") as provider, patch.object(cli, "Agent") as agent, \
                patch.object(cli, "SessionStore") as store, redirect_stdout(stdout), redirect_stderr(stderr):
            agent.return_value.run.return_value = ([], complete)
            agent.return_value.run.side_effect = error
            store.return_value.pending_inputs.return_value = {"version": 1, "pending": []}
            result = cli.main(["--no-config", "--workdir", str(self.root), *args])
        return SimpleNamespace(result=result, out=stdout.getvalue(), err=stderr.getvalue(),
                               tools=tools, provider=provider, agent=agent, store=store)

    def test_help_exits_before_initializing_tools_or_network(self):
        out = io.StringIO()
        with patch.object(cli, "Tools") as tools, patch.object(cli, "Provider") as provider, redirect_stdout(out):
            with self.assertRaises(SystemExit) as caught:
                cli.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        self.assertIn("--model", out.getvalue())
        self.assertIn("--resume", out.getvalue())
        tools.assert_not_called()
        provider.assert_not_called()

    def test_missing_task_or_model_fail_before_execution(self):
        for args in (("--model", "test"), ("Do work",)):
            with self.subTest(args=args):
                outcome = self.invoke(args)
                self.assertEqual(outcome.result, 1)
                self.assertIn("Error:", outcome.err)
                outcome.provider.assert_not_called()
                outcome.tools.assert_not_called()

    def test_openai_requires_key_and_local_provider_can_be_keyless(self):
        outcome = self.invoke(["Task", "--model", "test"])
        self.assertEqual(outcome.result, 1)
        self.assertIn("SPRIG_API_KEY", outcome.err)
        outcome.provider.assert_not_called()
        outcome = self.invoke(["Task", "--model", "test", "--base-url", "http://localhost:8080/v1"])
        self.assertEqual(outcome.result, 0)
        outcome.tools.return_value.close.assert_called_once()

    def test_environment_defaults_and_primary_key_precedence(self):
        outcome = self.invoke(["Task"], env={"SPRIG_MODEL": "env-model", "SPRIG_API_KEY": "primary-key",
                                           "OPENAI_API_KEY": "fallback-key", "SPRIG_BASE_URL": "https://api.example/v1"})
        self.assertEqual(outcome.result, 0)
        config = outcome.provider.call_args.args[0]
        self.assertEqual((config.model, config.api_key, config.base_url),
                         ("env-model", "primary-key", "https://api.example/v1"))
        self.assertIn("NOT sandboxed", outcome.out)
        self.assertNotIn("primary-key", outcome.out + outcome.err)
        outcome.store.assert_not_called()

    def test_fallback_key_and_explicit_model_endpoint_override_environment(self):
        outcome = self.invoke(["Task", "--model", "explicit", "--base-url", "https://explicit.example/v1"],
                              env={"SPRIG_MODEL": "env-model", "OPENAI_API_KEY": "fallback-key",
                                   "SPRIG_BASE_URL": "https://environment.example/v1"})
        config = outcome.provider.call_args.args[0]
        self.assertEqual((config.model, config.api_key, config.base_url),
                         ("explicit", "fallback-key", "https://explicit.example/v1"))

    def test_completion_incomplete_errors_and_interrupt_exit_codes_close_tools(self):
        for complete, error, expected in ((True, None, 0), (False, None, 2),
                                          (True, ProviderError("failed"), 1), (True, KeyboardInterrupt(), 130)):
            with self.subTest(expected=expected):
                outcome = self.invoke(["Task", "--model", "test", "--base-url", "http://localhost/v1"],
                                      complete=complete, error=error)
                self.assertEqual(outcome.result, expected)
                outcome.tools.return_value.close.assert_called_once()

    def test_errors_redact_key_and_escape_terminal_controls(self):
        outcome = self.invoke(["Task", "--model", "test"], env={"SPRIG_API_KEY": "secret"},
                              error=ProviderError("failed secret\x1b[31m"))
        self.assertEqual(outcome.result, 1)
        self.assertNotIn("secret", outcome.err)
        self.assertNotIn("\x1b", outcome.err)
        self.assertIn("[REDACTED]\\u001b", outcome.err)

    def test_save_session_is_explicit_and_checkpoint_is_wired(self):
        outcome = self.invoke(["Task", "--model", "test", "--base-url", "http://localhost/v1",
                               "--save-session", "demo"])
        self.assertEqual(outcome.result, 0)
        outcome.store.return_value.ensure_new.assert_called_once_with("demo")
        outcome.store.return_value.load.assert_not_called()
        checkpoint = outcome.agent.call_args.args[4]
        checkpoint([{"role": "user", "content": "x"}])
        outcome.store.return_value.save.assert_called_once_with("demo", [{"role": "user", "content": "x"}])
        outcome.store.return_value.close.assert_called_once()
        self.assertIn("not encrypted", outcome.out)

    def test_resume_can_omit_task_and_loads_history_without_replaying_in_cli(self):
        outcome = self.invoke(["--resume", "demo", "--model", "test", "--base-url", "http://localhost/v1"])
        self.assertEqual(outcome.result, 0)
        outcome.store.return_value.load.assert_called_once_with("demo")
        outcome.store.return_value.ensure_new.assert_not_called()
        outcome.agent.return_value.run.assert_called_once_with(None, outcome.store.return_value.load.return_value)
        outcome.store.return_value.close.assert_called_once()

    def test_unsupported_platform_fails_before_tools_or_provider(self):
        outcome = self.invoke(["Task", "--model", "test"], platform="unsupported")
        self.assertEqual(outcome.result, 1)
        self.assertIn("Windows 10+", outcome.err)
        outcome.tools.assert_not_called()
        outcome.provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
