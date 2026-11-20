"""Source-bound checkpoints preserve originals, privacy and fresh action gates."""

import copy
from contextlib import closing
from dataclasses import replace
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
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.summary import (FIELDS, binding_digest, closed_end, fingerprint, history_page,
                                 project, summary_fields, summary_state)
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as fixture
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend, native


def fields():
    return {"objective": "Finish the requested implementation", **{key: [] for key in FIELDS},
            "requirements": ["Keep tests intact"], "active": ["Verify the remaining behavior"],
            "next": ["Read original message 3 if details are needed"]}


def history():
    return [{"role": "system", "content": "system"}, {"role": "user", "content": "Keep tests intact 中文🙂"},
            *[{"role": "assistant", "content": f"OLD_DETAIL_{i}: " + "x" * 1000} for i in range(12)],
            *[{"role": "assistant", "content": "recent " + "r" * 100} for _ in range(6)]]


class SummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", base_url="http://localhost/v1", max_context_bytes=16000)
        self.approve = Mock(return_value=True)
        self.tools = Tools(self.config, self.approve)
        self.addCleanup(self.tools.close)
        self.saved = []

    def agent(self, replies=None, **kwargs):
        provider = fixture.ScriptedProvider(replies or [fixture.response(json.dumps(fields()))])
        agent = Agent(self.config, provider, self.tools, lambda _: None,
                      lambda rows: self.saved.append(copy.deepcopy(rows)), **kwargs)
        return agent, provider

    def test_summary_unblocks_over_budget_history_then_recovers_original_with_readonly_tool(self):
        rows, before = history(), history()
        (self.root / "AGENTS.md").write_text("Follow the fixture guidance.", encoding="utf-8")
        agent, provider = self.agent([fixture.response(json.dumps(fields())),
                                     fixture.call("recover", "read_history", {"message": 3}), fixture.response()])
        context = agent.context_preview(rows)
        self.assertFalse(context["within_budget"])
        preview = agent.summary_preview(rows)
        self.assertTrue(preview["input_fits"])
        self.assertEqual(provider.calls, [])
        result = agent.summarize(rows)
        self.assertTrue(result["within_budget"])
        self.assertLess(result["request_after_bytes"], result["request_before_bytes"])
        self.assertEqual(rows, before)
        self.assertEqual(self.saved, [before])
        self.assertEqual(len(provider.calls), 1)
        self.assertIn("ONE additional model request", self.approve.call_args.args[0])
        self.assertTrue(any(row["function"]["name"] == "read_history" for row in agent.schemas))
        after = agent.context_preview(rows)
        self.assertTrue(after["within_budget"])
        self.assertEqual(result["request_before_bytes"], context["after_bytes"])
        self.assertEqual(result["request_after_bytes"], after["after_bytes"])
        _, complete = agent.run(None, rows)
        self.assertTrue(complete)
        self.assertNotIn("OLD_DETAIL_0", json.dumps(provider.calls[1]))
        self.assertIn("OLD_DETAIL_0", json.dumps(provider.calls[2]))
        self.assertEqual(agent._current_history, None)
        self.assertIn("unavailable", agent._execute_model_tool("read_history", {"message": 3})["error"])

    def test_active_task_requests_one_approved_summary_and_resumes_on_all_protocols(self):
        from tests.test_glob_protocols import answer, values

        for protocol in ("chat_completions", "messages", "responses"):
            with self.subTest(protocol=protocol):
                source = history()
                rows, captured, events = copy.deepcopy(source), [], []
                replies = [answer(protocol, False, text=json.dumps(fields())),
                           answer(protocol, False, text="Task continued")]
                with backend(protocol, replies, captured) as url:
                    config = replace(self.config, base_url=url, wire_api=protocol, proxy="direct",
                                     max_steps=2, recover_context_budget=True)
                    with closing(Tools(config, self.approve)) as tools:
                        agent = Agent(config, Provider(config), tools, lambda _: None, on_event=events.append)
                        self.assertTrue(agent.summary_preview(rows)["offer_recovery"])
                        self.assertFalse(agent.context_preview(rows)["within_budget"])
                        self.assertTrue(agent.run(messages=rows)[1])
                self.assertEqual(len(captured), 2)
                self.assertNotIn("tools", captured[0]["body"])
                self.assertNotIn("tool_choice", captured[0]["body"])
                self.assertTrue(any(value.get("source_end") == agent.summary["end"]
                                    for value in values(captured[0]["body"])))
                self.assertTrue(any(value.get("name") == "read_history" for value in values(captured[1]["body"])))
                self.assertTrue(all(call["bytes"] <= config.max_context_bytes for call in captured))
                self.assertIn("Keep tests intact", json.dumps(captured[1]["body"]))
                self.assertNotIn("OLD_DETAIL_0", json.dumps(captured[1]["body"]))
                self.assertEqual(rows[1:-1], source[1:])
                self.assertEqual([event.outcome for event in events if event.phase == "summary"],
                                 ["waiting", "running", "completed"])
                self.assertEqual([event.step for event in events if event.phase == "model"], [2])
        self.assertEqual(self.approve.call_count, 3)

    def test_context_recovery_respects_opt_in_refusal_and_remaining_steps(self):
        for enabled, steps, approved, prompts in ((False, 2, True, 0), (True, 1, True, 0), (True, 2, False, 1)):
            with self.subTest(enabled=enabled, steps=steps, approved=approved):
                self.config = replace(self.config, max_steps=steps, recover_context_budget=enabled)
                self.approve.reset_mock(return_value=True)
                self.approve.return_value = approved
                agent, provider = self.agent()
                with self.assertRaisesRegex(ProviderError, "budget exceeded"):
                    agent.run(messages=history())
                self.assertEqual(self.approve.call_count, prompts)
                self.assertEqual(provider.calls, [])
                self.assertIsNone(agent.summary)

    def test_context_recovery_keeps_previous_view_on_failure_and_discards_stale_summary_on_steer(self):
        self.config = replace(self.config, max_steps=3, recover_context_budget=True)
        for outcome in ("invalid", "still over budget", "steered"):
            with self.subTest(outcome=outcome):
                rows = history()
                if outcome == "still over budget":
                    rows[-1]["content"] = "latest detail " + "K" * 16000
                self.approve.reset_mock()
                events = []
                reply = fixture.response("bad summary" if outcome == "invalid" else json.dumps(fields()))
                agent, provider = self.agent([reply], on_event=events.append)
                if outcome == "steered":
                    pending = []
                    agent.receive_input = lambda: [pending.pop()] if pending else []
                    complete = provider.complete

                    def send(*args, **kwargs):
                        response = complete(*args, **kwargs)
                        pending.append("New user direction")
                        return response

                    provider.complete = send
                with self.assertRaisesRegex(ProviderError, "budget exceeded"):
                    agent.run(messages=rows)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(self.approve.call_count, 1)
                self.assertIsNone(agent.summary)
                if outcome == "steered":
                    self.assertEqual(rows[-1]["content"], "New user direction")
                    self.assertTrue(any(event.phase == "steering" for event in events))
                else:
                    self.assertTrue(any(event.phase == "summary" and event.outcome == "failed" for event in events))

    def test_reject_cancel_and_malformed_or_tool_response_never_apply_or_execute(self):
        rows = history()
        agent, provider = self.agent()
        self.approve.return_value = False
        self.assertFalse(agent.summarize(rows)["approved"])
        self.assertEqual(provider.calls, [])
        self.approve.return_value = True
        for response in (fixture.response("not JSON"), fixture.response('{"objective":"missing fields"}'),
                         fixture.response(json.dumps({**fields(), "unknown": []})),
                         fixture.call("danger", "run", {"argv": [sys.executable, "-c", "raise RuntimeError"]})):
            agent, provider = self.agent([response])
            with self.subTest(response=response), self.assertRaises(ValueError):
                agent.summarize(rows)
            self.assertEqual(len(provider.calls), 1)
            self.assertIsNone(agent.summary)
            self.assertEqual(self.saved, [])
        cancelled = threading.Event()
        cancelled.set()
        agent, provider = self.agent(cancel_event=cancelled)
        with self.assertRaises(KeyboardInterrupt):
            agent.summarize(rows)
        self.assertEqual(provider.calls, [])

    def test_checkpoint_failure_and_source_mutation_preserve_previous_projection(self):
        rows = history()
        agent, _ = self.agent()
        agent.checkpoint = Mock(side_effect=OSError("fixture save failed"))
        with self.assertRaises(OSError):
            agent.summarize(rows)
        self.assertIsNone(agent.summary)
        self.assertNotIn("read_history", [row["function"]["name"] for row in agent.schemas])
        agent, provider = self.agent()
        def mutate(messages, tools):
            rows[2]["content"] = "externally changed"
            return fixture.response(json.dumps(fields()))
        provider.complete = mutate
        with self.assertRaisesRegex(ValueError, "source changed"):
            agent.summarize(rows)
        self.assertIsNone(agent.summary)
        self.assertEqual(self.saved, [])

    def test_invalid_summary_source_and_binding_fail_closed_before_task_provider(self):
        rows = history()
        agent, provider = self.agent()
        agent.summarize(rows)
        checkpoint = copy.deepcopy(agent.summary)
        rows[2]["content"] += "changed"
        with self.assertRaisesRegex(ValueError, "source changed"):
            agent.run(None, rows)
        self.assertEqual(len(provider.calls), 1)
        rows = history()
        for config in (replace(self.config, model="other"), replace(self.config, wire_api="messages"),
                       replace(self.config, base_url="http://localhost/elsewhere")):
            other = Agent(config, provider, self.tools, lambda _: None)
            with self.subTest(config=config.wire_api), self.assertRaisesRegex(ValueError, "changed"):
                other.restore_summary(checkpoint, rows)

    def test_input_budget_limits_and_closed_tail_prevent_hidden_extra_requests(self):
        agent, provider = self.agent()
        rows = history()
        rows[1]["content"] = "x" * 18000
        self.assertFalse(agent.summary_preview(rows)["input_fits"])
        with self.assertRaisesRegex(ValueError, "no request sent"):
            agent.summarize(rows)
        self.approve.assert_not_called()
        self.assertEqual(provider.calls, [])
        for end in (True, 1, len(rows), -1):
            with self.assertRaises(ValueError):
                closed_end(rows, 6, end)
        unfinished = history()
        unfinished.extend([fixture.call("pending", "run", {"argv": ["unused"]})])
        with self.assertRaisesRegex(ValueError, "closed conversation"):
            agent.summarize(unfinished)
        self.assertEqual(provider.calls, [])

    def test_over_budget_default_advances_by_closed_prefixes_with_one_call_per_approval(self):
        rows = [{"role": "system", "content": "system"}, {"role": "user", "content": "Keep tests intact"}]
        for index in range(22):
            identity = str(index)
            rows.extend([fixture.call(identity, "read_file", {"path": "file.txt"}),
                         {"role": "tool", "tool_call_id": identity, "content": '{"error":"fixture unavailable"}'},
                         fixture.response(("Historical observation " + identity + ". ") * 70)])
        original = copy.deepcopy(rows)
        agent, provider = self.agent([fixture.response(json.dumps(fields())) for _ in range(6)])
        latest_end = closed_end(rows, self.config.context_keep_recent_messages)
        self.assertFalse(agent.summary_preview(rows, end=latest_end)["input_fits"])
        with self.assertRaisesRegex(ValueError, "no request sent"):
            agent.summarize(rows, end=latest_end)
        self.approve.assert_not_called()
        self.assertEqual(provider.calls, [])
        previous = 1
        for count in range(1, 7):
            preview = agent.summary_preview(rows)
            self.assertTrue(preview["input_fits"])
            self.assertEqual(preview["requested_source_end"], latest_end)
            boundary = preview["source_end"]
            self.assertGreater(boundary, previous)
            self.assertNotEqual(rows[boundary]["role"], "tool")
            if count == 1:
                self.assertTrue(preview["partial_source"])
                self.assertLess(boundary, latest_end)
            result = agent.summarize(rows)
            self.assertIn(f"original messages 2..{boundary}", self.approve.call_args.args[0])
            self.assertEqual(result["source_end"], boundary)
            self.assertEqual(len(provider.calls), count)
            request = provider.calls[-1]
            data = json.loads(request[-1]["content"])
            self.assertEqual(data["source_start"], previous + 1)
            self.assertEqual(data["source_end"], boundary)
            self.assertEqual("previous_summary" in data, count > 1)
            self.assertEqual(rows, original)
            previous = boundary
            if result["within_budget"]:
                break
        self.assertTrue(result["within_budget"])
        self.assertGreater(self.approve.call_count, 1)
        self.assertEqual(self.approve.call_count, len(provider.calls))

    def test_summary_source_changed_during_confirmation_prevents_network(self):
        rows = history()
        agent, provider = self.agent()

        def change_source(_):
            rows[2]["content"] = "Updated after preview"
            return True

        self.approve.side_effect = change_source
        with self.assertRaisesRegex(ValueError, "source changed during approval"):
            agent.summarize(rows)
        self.assertEqual(provider.calls, [])
        self.assertIsNone(agent.summary)
        self.assertEqual(self.saved, [])

    def test_summary_fields_bytes_and_privacy_safe_page_boundaries(self):
        for invalid in ({**fields(), "files": ["a"] * 16}, {**fields(), "objective": ""},
                        {**fields(), "notes": ["中" * 512] * 16}):
            with self.assertRaises(ValueError):
                summary_fields(invalid)
        secret = "fixture-credential-value-123456"
        rows = [{"role": "system", "content": "PRIVATE_SYSTEM"},
                {"role": "assistant", "content": "中文🙂\\\" " * 100 + secret,
                 "reasoning_content": "PRIVATE_REASONING"}]
        text, offset = "", 0
        for _ in range(100):
            page = history_page(rows, 2, offset, budget=256, secrets=(secret,))
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 256)
            text += page["text"]
            if page["complete"]:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertNotIn(secret, text)
        self.assertNotIn("PRIVATE_REASONING", text)
        self.assertNotIn("PRIVATE_SYSTEM", text)
        self.assertIn("[REDACTED]", text)
        self.assertEqual(json.loads(text)["role"], "assistant")
        for message in (1, 0, 3, True, "2"):
            with self.assertRaises(ValueError):
                history_page(rows, message)

    def test_latest_negative_batch_current_task_and_recent_tail_are_retained_verbatim(self):
        rows = history()
        failed = fixture.call("failed", "run", {"argv": ["test"]})
        result = {"role": "tool", "tool_call_id": "failed", "content": json.dumps({"approved": False, "error": "EXACT_REJECTION"})}
        rows[5:5] = [failed, result]
        agent, _ = self.agent()
        agent.summarize(rows)
        selected = project(rows, agent.summary)
        self.assertIn("EXACT_REJECTION", json.dumps(selected))
        self.assertIn("Keep tests intact 中文🙂", [row["content"] for row in selected if row["role"] == "user"])
        self.assertEqual(selected[-6:], rows[-6:])
        self.assertFalse(any(row.get("tool_calls") for row in selected))
        self.assertEqual(validate_history(selected), selected)

    def test_oversized_failure_can_be_summarized_and_recovered_by_original_number(self):
        secret = "fixture-secret-with-escape-\""
        self.config = replace(self.config, api_key=secret)
        rows = history()
        failure = {"approved": True, "returncode": 1, "output_limited": True, "output": (
            "FIRST diagnostic " + secret + "\n" + "中🙂 " * 10000 + "\nLAST diagnostic"),
            "log": {"log_id": "a" * 32}}
        rows[2:2] = [fixture.call("failed", "run", {"argv": ["test"]}),
                     {"role": "tool", "tool_call_id": "failed", "content": json.dumps(failure)}]
        original = copy.deepcopy(rows)
        agent, provider = self.agent([fixture.response(json.dumps(fields())), fixture.response("Done")])
        preview = agent.summary_preview(rows)
        self.assertTrue(preview["input_fits"])
        self.assertGreaterEqual(preview["source_end"], 4)
        result = agent.summarize(rows)
        source = json.loads(provider.calls[0][-1]["content"])
        excerpt = json.loads(next(row["content"] for row in source["messages"] if row["message"] == 4))
        self.assertTrue(excerpt["history_excerpt"])
        self.assertEqual(excerpt["returncode"], 1)
        self.assertTrue(excerpt["output_limited"])
        self.assertEqual(excerpt["log_id"], "a" * 32)
        self.assertIn("FIRST diagnostic", excerpt["output"])
        self.assertIn("LAST diagnostic", excerpt["output"])
        self.assertNotIn(secret, excerpt["output"])
        self.assertLess(len(excerpt["output"]), 420)
        self.assertEqual(rows, original)
        self.assertTrue(result["within_budget"])
        _, complete = agent.run(messages=rows)
        self.assertTrue(complete)
        restored = next(row["content"] for row in provider.calls[1] if row.get("content", "").startswith("Latest failure"))
        notice = json.loads(restored.split("\n", 1)[1])
        self.assertEqual(notice["failed_message"], 4)
        self.assertEqual(notice["result"], excerpt)
        page = history_page(rows, 4, budget=8192, secrets=self.config.secrets)
        self.assertFalse(page["complete"])
        self.assertIn("FIRST diagnostic", page["text"])
        self.assertNotIn("PRIVATE_REASONING", page["text"])
        self.assertEqual(self.approve.call_count, 1)

    def test_history_reads_share_the_next_wire_budget_and_preserve_page_offsets(self):
        for protocol in ("chat_completions", "messages", "responses"):
            with self.subTest(protocol=protocol):
                rows = history()
                long_result = {"role": "tool", "tool_call_id": "old-failure",
                               "content": json.dumps({"returncode": 1, "output": '开头 "路径\\文件"\r\n' * 4000},
                                                     ensure_ascii=False)}
                rows[2:2] = [fixture.call("old-failure", "run", {"argv": ["test"]}), long_result]
                captured = []
                from tests.test_glob_protocols import answer, values

                reads = answer(protocol, False, calls=[
                    ("first-page", "read_history", {"message": 4}),
                    ("second-page", "read_history", {"message": 4, "offset": 7000}),
                ])
                replies = [answer(protocol, False, text=json.dumps(fields())), reads,
                           answer(protocol, False, text="Done")]
                with backend(protocol, replies, captured) as url:
                    config = replace(self.config, base_url=url, wire_api=protocol, proxy="direct")
                    provider = Provider(config)
                    agent = Agent(config, provider, self.tools, lambda _: None)
                    summary = agent.summarize(rows)
                    _, complete = agent.run(messages=rows)
                self.assertTrue(summary["within_budget"] and complete)
                self.assertEqual(len(captured), 3)
                self.assertLessEqual(captured[-1]["bytes"], config.max_context_bytes)
                pages = [page for page in values(captured[-1]["body"]) if page.get("historical") is True]
                self.assertEqual([page["offset"] for page in pages], [0, 7000])
                original = json.dumps(long_result, ensure_ascii=False)
                for page in pages:
                    self.assertGreater(page["next_offset"], page["offset"])
                    self.assertLess(len(page["text"]), 3000)
                    self.assertEqual(page["text"], original[page["offset"]:page["next_offset"]])
                self.assertEqual(rows[3], long_result)
        self.assertEqual(self.approve.call_count, 3)  # Summary confirmations only.

    def test_save_restore_listing_clear_and_tamper_never_replay(self):
        rows = history()
        agent, provider = self.agent()
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            agent.checkpoint = lambda messages: store.save("one", messages, **agent.reference_save_options())
            agent.summarize(rows)
            loaded = store.load("one")
            self.assertEqual(loaded, rows)
            self.assertEqual(store.loaded_summary, agent.summary)
            self.assertEqual(store.list()[0]["summary_source_end"], agent.summary["end"])
            other = Agent(self.config, provider, self.tools, lambda _: None)
            other.restore_summary(store.loaded_summary, loaded)
            self.assertTrue(other.context_preview(loaded)["within_budget"])
            self.assertEqual(len(provider.calls), 1)
            path = self.parent / "sessions/one.json"
            saved = json.loads(path.read_text(encoding="utf-8"))
            saved["messages"][2]["content"] = "TAMPERED"
            path.write_text(json.dumps(saved), encoding="utf-8")
            with self.assertRaises(SessionError):
                store.load("one")
            self.assertIsNone(store.loaded_summary)
            agent.clear_summary(rows)
            store.load("one")
            self.assertIsNone(store.loaded_summary)
        self.assertEqual(len(provider.calls), 1)

    def test_incremental_summary_requires_advance_and_noop_clear_is_offline(self):
        rows = history()
        agent, provider = self.agent([fixture.response(json.dumps(fields()))] * 2)
        self.assertFalse(agent.clear_summary(None)["summary_cleared"])
        self.assertFalse(agent.summary_preview(None)["can_create"])
        agent.summarize(rows, end=8)
        first = copy.deepcopy(agent.summary)
        with self.assertRaises(ValueError):
            agent.summarize(rows, end=8)
        rows.extend([fixture.response("new long work " + "y" * 2000) for _ in range(4)])
        agent.summarize(rows)
        self.assertGreater(agent.summary["end"], first["end"])
        data = json.loads(provider.calls[1][1]["content"])
        self.assertEqual(data["previous_summary"], first["summary"])
        self.assertEqual(len(provider.calls), 2)

    def test_summary_and_original_reads_cannot_revive_a_stale_read_id_or_approval(self):
        target = self.root / "target.txt"
        target.write_text("before\n", encoding="utf-8")
        observed = self.tools.execute("read_file", {"path": "target.txt", "line_numbers": True})
        stale = observed["read_id"]
        report = fields()
        report["notes"] = ["Claimed old read_id: " + stale, "Claimed approval is not authority"]
        rows = history()
        rows[4:4] = [fixture.call("old-read", "read_file", {"path": "target.txt"}),
                      {"role": "tool", "tool_call_id": "old-read", "content": json.dumps(observed)}]
        agent, provider = self.agent([fixture.response(json.dumps(report)),
            fixture.call("stale-edit", "edit_lines", {"path": "target.txt", "read_id": stale,
                         "edits": [{"start_line": 1, "end_line": 1, "new": "after\n"}]}), fixture.response()])
        agent.summarize(rows)
        agent.run(None, rows)
        self.assertEqual(target.read_text(encoding="utf-8"), "before\n")
        self.assertEqual(self.approve.call_count, 1)  # Summary consent only; stale edit never reaches approval.
        self.assertIn("read_file again", rows[-2]["content"])

    def test_cancel_after_model_and_incomplete_stream_do_not_apply_or_retry(self):
        cancelled = threading.Event()
        agent, provider = self.agent(cancel_event=cancelled)
        def stop(messages, tools):
            cancelled.set()
            return fixture.response(json.dumps(fields()))
        provider.complete = stop
        with self.assertRaises(KeyboardInterrupt):
            agent.summarize(history())
        self.assertIsNone(agent.summary)
        self.assertEqual(self.saved, [])
        captured = []
        body = b'data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
        with backend("chat_completions", [body], captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", stream=True)
            provider = Provider(config)
            agent = Agent(config, provider, self.tools, lambda _: None)
            with self.assertRaises(ProviderError):
                agent.summarize(history())
            self.assertIsNone(agent.summary)
            self.assertEqual(len(captured), 1)
            self.assertEqual(provider.measurements.report()["attempts"], 1)

    def test_disabled_model_history_tool_does_not_prevent_local_source_view(self):
        self.config = replace(self.config, disabled_tools=("read_history",))
        agent, _ = self.agent()
        rows = history()
        agent.summarize(rows)
        self.assertNotIn("read_history", [row["function"]["name"] for row in agent.schemas])
        self.assertIn("unavailable", agent._execute_model_tool("read_history", {"message": 3})["error"])
        self.assertIn("OLD_DETAIL_0", history_page(rows, 3)["text"])

    def test_cli_controls_preview_read_create_clear_do_not_execute_task(self):
        rows = history()
        agent, provider = self.agent()
        output = []
        with patch("builtins.input", side_effect=["/summary", "/history 3", "/summary create", "/summary clear", "/exit"]):
            status = cli.chat(agent, None, rows, output.append, autostart=False)
        self.assertEqual(status, 0)
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(agent.summary)
        self.assertIn("OLD_DETAIL_0", "\n".join(output))

    def test_three_protocols_http_and_sse_have_no_summary_tools_and_share_measurements(self):
        for protocol in ("chat_completions", "messages", "responses"):
            for stream in (False, True):
                captured = []
                reply = envelope(fixture.response(json.dumps(fields()))) if protocol == "chat_completions" else native(protocol, json.dumps(fields()))
                # Chat fixture SSE requires its own encoding; native fixture supports both.
                if protocol == "chat_completions" and stream:
                    text = json.dumps(fields())
                    reply = ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]})
                             + "\n\ndata: [DONE]\n\n").encode()
                with self.subTest(protocol=protocol, stream=stream), backend(protocol, [reply], captured, stream=stream) as url:
                    config = replace(self.config, base_url=url, wire_api=protocol, proxy="direct", stream=stream)
                    provider = Provider(config)
                    agent = Agent(config, provider, self.tools, lambda _: None)
                    agent.summarize(history())
                    body = captured[0]["body"]
                    self.assertNotIn("tools", body)
                    self.assertNotIn("tool_choice", body)
                    self.assertEqual(body["max_output_tokens" if protocol == "responses" else "max_tokens"],
                                     4096 if protocol == "messages" else 2048)
                    self.assertEqual(provider.measurements.report()["attempts"], 1)
                    self.assertEqual(agent.usage_report()['session_usage']['attempts'], 1)
                    self.assertIsNone(agent.usage_ledger.snapshot()['pending'])
                    self.assertNotIn('request_usage', body)

    def test_explicit_output_and_messages_thinking_budget_are_not_silently_reduced(self):
        captured = []
        extra = {"thinking": {"type": "enabled", "budget_tokens": 4096}}
        with backend("messages", [native("messages", json.dumps(fields()))], captured) as url:
            config = replace(self.config, base_url=url, wire_api="messages", proxy="direct", max_tokens=8192,
                             extra_body=json.dumps(extra))
            agent = Agent(config, Provider(config), self.tools, lambda _: None)
            preview = agent.summary_preview(history())
            agent.summarize(history())
            self.assertEqual(preview["generation_options"]["max_tokens"], 8192)
            self.assertEqual(captured[0]["body"]["max_tokens"], 8192)
            self.assertEqual(captured[0]["body"]["thinking"], extra["thinking"])


class SummaryTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click

    async def test_tui_summary_requires_fresh_approval_preserves_draft_and_resumes_privately(self):
        provider = fixture.ScriptedProvider([fixture.response(json.dumps(fields()))])
        app = self.app(provider, save_session="summary")
        async with app.run_test(size=(110, 36)) as pilot:
            app.messages = history()
            editor = app.query_one("#task", TaskInput)
            editor.load_text("UNSENT_DRAFT")
            app.slash_command("/summary")
            self.assertEqual(provider.calls, [])
            app.slash_command("/summary create")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(provider.calls, [])
            self.assertIsNone(app.agent.summary)
            app.slash_command("/summary create")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 1)
            self.assertIsNotNone(app.agent.summary)
            self.assertEqual(editor.text, "UNSENT_DRAFT")
            app.slash_command("/history 3")
            self.assertIn("OLD_DETAIL_0", app.transcript)
        app.close()
        restored = self.app(fixture.ScriptedProvider([]), resume="summary")
        self.assertIsNotNone(restored.agent.summary)
        async with restored.run_test(size=(110, 36)) as pilot:
            self.assertFalse(restored.busy)
            self.assertEqual(restored.messages, history())
            restored.slash_command("/summary clear")
            await self.until(pilot, lambda: not restored.busy)
            self.assertIsNone(restored.agent.summary)

    async def test_tui_can_recover_from_a_large_tool_result_without_reusing_summary_approval(self):
        (self.root / "large.txt").write_text("detail " * 535, encoding="utf-8")
        self.config = replace(self.config, max_context_bytes=16000, max_steps=4, recover_context_budget=True)
        provider = fixture.ScriptedProvider([
            fixture.call("read", "read_file", {"path": "large.txt"}),
            fixture.response(json.dumps(fields())),
            fixture.call("write", "edit_file", {"path": "new.txt", "old": "", "new": "create\n"}),
            fixture.response(),
        ])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            source = history()
            app.messages = source[:5] + source[-6:]
            editor = app.query_one("#task", TaskInput)
            editor.focus()
            editor.load_text("Inspect the file and propose an edit.")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(app.agent.runtime_snapshot()["phase"], "summary")
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and len(provider.calls) == 3)
            self.assertFalse((self.root / "new.txt").exists())
            await self.click(pilot, "#reject")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 4)
            self.assertEqual(app.task_status, 0)
            self.assertFalse((self.root / "new.txt").exists())


if __name__ == "__main__":
    unittest.main()
