"""Clarification is human data; sibling proposals and authority stay blocked."""

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
from textual.widgets import Input, Static, TextArea
from textual.events import Paste
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.questions import answer_result, terminal_questions, validate_questions
from sprig_agent.question_ui import QuestionOptions, QuestionScreen
from sprig_agent.session import SessionStore, validate_history
from sprig_agent.summary import latest_failure
from sprig_agent.tools import Tools
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as fixture
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend, native, stream_events, sse
from tests.test_provider_recovery import stream


def prompts():
    return [{"header": "Direction", "question": "Which direction? 中文🙂", "options": [
        {"label": "A", "description": "First route"}, {"label": "B", "description": "Second route"}]}]


def question_call(identity="question", questions=None):
    return fixture.call(identity, "question", {"questions": prompts() if questions is None else questions})


def mixed_call():
    value = fixture.call("before", "edit_file", {"path": "old-before.txt", "old": "", "new": "NO"})
    value["tool_calls"] += question_call()["tool_calls"]
    value["tool_calls"] += fixture.call("after", "run", {"argv": [sys.executable, "-c", "raise SystemExit('NO')"]})["tool_calls"]
    return value


class QuestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", max_steps=3)

    def tools(self, ask=None, config=None, approve=None, cancel=None):
        tools = Tools(config or self.config, approve or (lambda _: self.fail("unexpected action approval")), cancel, ask=ask)
        self.addCleanup(tools.close)
        return tools

    def test_strict_question_fields_counts_unicode_controls_and_post_redaction_labels(self):
        selected = validate_questions(prompts())
        self.assertFalse(selected[0]["multiple"])
        self.assertEqual(validate_questions([{"header": "Text", "question": "Explain"}])[0]["options"], [])
        bad = [[], prompts() * 4, [{"header": "x", "question": "q", "unknown": True}],
               [{"header": "x" * 31, "question": "q"}], [{"header": "x", "question": "\x1b[2J"}],
               [{"header": "x", "question": "q", "multiple": 1}],
               [{"header": "x", "question": "q", "options": [{"label": "a", "description": "x"}] * 2}],
               [{"header": "x", "question": "\ud800"}]]
        bad.append([{"header": "x", "question": "中" * 512,
                     "options": [{"label": str(i) + "中" * 60, "description": "中" * 160} for i in range(6)]}] * 3)
        for value in bad:
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                validate_questions(value)
        with self.assertRaises(ValueError):
            validate_questions([{"header": "x", "question": "q", "options": [
                {"label": "first-secret", "description": "x"}, {"label": "second-secret", "description": "y"}]}],
                               ("first-secret", "second-secret"))

    def test_answer_shape_exact_json_budget_and_redaction(self):
        questions = validate_questions(prompts())
        result = answer_result(questions, [["user custom 中文🙂"]], 256)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)
        self.assertTrue(result["not_authorization"])
        with self.assertRaises(ValueError):
            answer_result(questions, [["user custom 中文🙂"]], len(json.dumps(result, ensure_ascii=False).encode()) - 1)
        self.assertNotIn("approved", result)
        for answers in (None, [], [[]], [["a", "b"]], [[False]], [["\x1b"]], [["x" * 2049]], {"approved": True}):
            with self.subTest(answers=answers), self.assertRaises(ValueError):
                answer_result(questions, answers)
        with self.assertRaises(ValueError):
            answer_result(questions, [["中" * 200]], 256)
        self.assertEqual(answer_result(questions, [["fixture-key-123456"]], secrets=("fixture-key-123456",))["answers"], [["[REDACTED]"]])
        multiple = validate_questions([{**prompts()[0], "multiple": True}])
        self.assertEqual(answer_result(multiple, [["A", "B", "custom"]])["answers"], [["A", "B", "custom"]])
        with self.assertRaises(ValueError):
            answer_result(multiple, [["A", "A"]])

    def test_cli_explicit_selection_multiple_custom_eof_and_no_tty_default(self):
        questions = validate_questions([*prompts(), {"header": "Targets", "question": "Which?", "multiple": True,
                                                   "options": prompts()[0]["options"]}, {"header": "Note", "question": "Note?"}])
        replies = iter(["", "9", "2", "1,2", ":  user note"])
        self.assertEqual(terminal_questions(questions, read=lambda _: next(replies), emit=lambda _: None),
                         [["B"], ["A", "B"], ["user note"]])
        combined = iter(["1,2,: custom extra"])
        self.assertEqual(terminal_questions([questions[1]], read=lambda _: next(combined), emit=lambda _: None), [["A", "B", "custom extra"]])
        self.assertIsNone(terminal_questions(questions, read=lambda _: "/cancel", emit=lambda _: None))
        with patch("sprig_agent.questions.sys.stdin.isatty", return_value=False), patch("builtins.input") as read:
            self.assertIsNone(terminal_questions(questions))
            read.assert_not_called()
        with self.assertRaises(KeyboardInterrupt):
            terminal_questions(questions, read=Mock(side_effect=KeyboardInterrupt), emit=lambda _: None)
        self.assertIsNone(terminal_questions(questions, read=Mock(side_effect=EOFError), emit=lambda _: None))

    def test_unavailable_disabled_closed_and_invalid_never_open_ui(self):
        ask = Mock(return_value=[["A"]])
        absent = self.tools()
        self.assertFalse(absent.execute("question", {"questions": prompts()})["answered"])
        self.assertNotIn("question", [row["function"]["name"] for row in Agent(self.config, None, absent, lambda _: None).schemas])
        disabled = self.tools(ask, replace(self.config, disabled_tools=("question",)))
        self.assertIn("error", disabled.execute("question", {"questions": prompts()}))
        tools = self.tools(ask)
        self.assertIn("error", tools.execute("question", {"questions": []}))
        tools.close()
        self.assertIn("error", tools.execute("question", {"questions": prompts()}))
        ask.assert_not_called()

    def test_secret_prompts_and_answers_are_redacted_before_ui_and_checkpoint(self):
        key = "fixture-question-key-123456"
        config = replace(self.config, api_key=key)
        seen = []
        def ask(questions):
            seen.append(questions)
            self.assertNotIn(key, json.dumps(questions))
            return [[key]]
        tools = self.tools(ask, config)
        provider = fixture.ScriptedProvider([question_call(questions=[{"header": "Text", "question": "Model echo " + key}]), fixture.response("done")])
        with closing(SessionStore(self.parent / "sessions", config)) as store:
            agent = Agent(config, provider, tools, lambda _: None, checkpoint=lambda rows: store.save("dialog", rows))
            rows, _ = agent.run("task")
            self.assertNotIn(key, json.dumps(rows))
            self.assertNotIn(key, (store.directory / "dialog.json").read_text(encoding="utf-8"))
            self.assertEqual(len(seen), 1)

    def test_mixed_batch_skips_earlier_and_later_proposals_before_fresh_response(self):
        approvals, events = [], []
        tools = self.tools(lambda _: [["yes, but this is not approval"]], approve=lambda text: approvals.append(text) or False)
        provider = fixture.ScriptedProvider([mixed_call(), fixture.call("fresh", "edit_file", {"path": "fresh.txt", "old": "", "new": "NO"}), fixture.response("stopped")])
        rows, _ = Agent(self.config, provider, tools, lambda _: None, on_event=events.append).run("task")
        self.assertEqual(len(approvals), 1)
        self.assertFalse((self.root / "old-before.txt").exists())
        self.assertFalse((self.root / "fresh.txt").exists())
        results = {row["tool_call_id"]: json.loads(row["content"]) for row in rows if row["role"] == "tool"}
        self.assertTrue(results["before"]["skipped"])
        self.assertTrue(results["after"]["skipped"])
        self.assertTrue(results["question"]["not_authorization"])
        self.assertTrue(any(row.phase == "question" and row.outcome == "waiting" for row in events))
        self.assertTrue(any(row.phase == "question" and row.outcome == "delivered" for row in events))
        self.assertEqual(rows, validate_history(rows))

    def test_dismissal_or_invalid_answer_stops_model_and_all_siblings(self):
        for answer in (None, [], {"approved": True}, [["x" * 2049]]):
            with self.subTest(answer=answer):
                provider = fixture.ScriptedProvider([question_call() if answer is None else mixed_call(), fixture.response("MUST_NOT_REQUEST")])
                tools = self.tools(lambda _: answer)
                agent = Agent(self.config, provider, tools, lambda _: None)
                rows, complete = agent.run("task")
                self.assertFalse(complete)
                self.assertEqual(agent._stop_reason, "question")
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(rows, validate_history(rows))
                self.assertTrue(latest_failure(rows, len(rows)))

    def test_new_guidance_after_dismissal_replans_without_using_old_proposals(self):
        queued = []
        def ask(_):
            queued.append("updated request C")
            return None
        def receive():
            values = list(queued)
            queued.clear()
            return values
        provider = fixture.ScriptedProvider([mixed_call(), fixture.response("following C")])
        tools = self.tools(ask)
        rows, complete = Agent(self.config, provider, tools, lambda _: None, receive_input=receive).run("task")
        self.assertTrue(complete)
        self.assertIn("updated request C", json.dumps(provider.calls[-1]))
        self.assertFalse((self.root / "old-before.txt").exists())

    def test_cancel_after_callback_never_commits_answer_or_replays_on_load(self):
        cancelled = threading.Event()
        def ask(_):
            cancelled.set()
            return [["PRIVATE_NOT_SUBMITTED"]]
        tools = self.tools(ask, cancel=cancelled)
        provider = fixture.ScriptedProvider([mixed_call()])
        rows = [{"role": "system", "content": "system"}]
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            agent = Agent(self.config, provider, tools, lambda _: None, cancel_event=cancelled,
                          checkpoint=lambda history: store.save("pending", history))
            with self.assertRaises(KeyboardInterrupt):
                agent.run("task", rows)
            loaded = store.load("pending")
            self.assertNotIn("PRIVATE_NOT_SUBMITTED", json.dumps(loaded))
            self.assertIn("NOT replayed", loaded[-1]["content"])
        self.assertFalse((self.root / "old-before.txt").exists())

    def test_question_is_allowed_in_read_only_but_cannot_run_checks_or_commands(self):
        config = replace(self.config, read_only=True)
        ask = Mock(return_value=[["B"]])
        tools = self.tools(ask, config)
        provider = fixture.ScriptedProvider([question_call(), fixture.response("reviewed")])
        rows, complete = Agent(config, provider, tools, lambda _: None).run("review")
        self.assertTrue(complete)
        self.assertEqual(ask.call_count, 1)
        self.assertTrue(json.loads(rows[3]["content"])["not_authorization"])

    def test_question_answer_cannot_approve_independent_fixed_acceptance(self):
        approvals = []
        marker = self.root / "check-ran"
        config = replace(self.config, checks=((sys.executable, "-c", "from pathlib import Path;Path('check-ran').write_text('NO')"),))
        tools = self.tools(lambda _: [["yes, approved"]], config, lambda text: approvals.append(text) or False)
        provider = fixture.ScriptedProvider([question_call(), fixture.response("all checks passed")])
        agent = Agent(config, provider, tools, lambda _: None)
        rows, complete = agent.run("task")
        self.assertFalse(complete)
        self.assertEqual(len(approvals), 1)
        self.assertFalse(marker.exists())
        self.assertNotEqual(agent.runtime_state.outcome, "passed")

    def test_checkpoint_failure_before_question_prevents_ui(self):
        for timing in ("before", "after"):
            with self.subTest(timing=timing):
                ask = Mock(return_value=[["A"]])
                tools = self.tools(ask)
                provider = fixture.ScriptedProvider([mixed_call(), fixture.response("MUST_NOT_REQUEST")])
                def checkpoint(rows):
                    if ((timing == "before" and rows[-1].get("tool_calls"))
                            or (timing == "after" and rows[-1].get("tool_call_id") == "question")):
                        raise OSError("fixture checkpoint failure")
                with self.assertRaises(OSError):
                    Agent(self.config, provider, tools, lambda _: None, checkpoint=checkpoint).run("task")
                self.assertEqual(ask.call_count, int(timing == "after"))
                self.assertEqual(len(provider.calls), 1)

    def test_incomplete_protocol_stream_never_opens_question_or_actions(self):
        for protocol in ("chat_completions", "messages", "responses"):
            with self.subTest(protocol=protocol):
                message = question_call()
                if protocol == "chat_completions":
                    delta = {**message, "tool_calls": [{"index": 0, **message["tool_calls"][0]}]}
                    body = stream(delta, "tool_calls", done=False)
                else:
                    data = native(protocol, None, [("question", "question", {"questions": prompts()})], reasoning=False)
                    body = sse(stream_events(protocol, data)[:-1])
                captured = []
                with backend(protocol, [body], captured) as url:
                    config = replace(self.config, base_url=url, proxy="direct", wire_api=protocol, stream=True)
                    ask = Mock(return_value=[["A"]])
                    tools = self.tools(ask, config)
                    with self.assertRaises(ProviderError):
                        Agent(config, Provider(config), tools, lambda _: None).run("task")
                    ask.assert_not_called()
                    self.assertEqual(len(captured), 1)

    def test_protocol_http_and_sse_answers_reach_next_request_with_fresh_approval(self):
        def wire(protocol, message):
            calls = [(row["id"], row["function"]["name"], json.loads(row["function"]["arguments"])) for row in message.get("tool_calls", [])]
            return envelope(message) if protocol == "chat_completions" else native(protocol, message.get("content"), calls, reasoning=False)
        for protocol in ("chat_completions", "messages", "responses"):
            for streamed in (False, True):
                with self.subTest(protocol=protocol, streamed=streamed):
                    captured, approvals = [], []
                    def next_reply(data):
                        self.assertIn("answers", json.dumps(data))
                        self.assertIn("B", json.dumps(data))
                        return wire(protocol, fixture.call("fresh", "edit_file", {"path": "fresh.txt", "old": "", "new": "requested"}))
                    values = [wire(protocol, mixed_call()), next_reply, wire(protocol, fixture.response("done"))]
                    if streamed and protocol == "chat_completions":
                        def chunks(message):
                            delta = {**message}
                            if "tool_calls" in delta:
                                delta["tool_calls"] = [{"index": index, **row} for index, row in enumerate(delta["tool_calls"])]
                            return stream(delta, "tool_calls" if message.get("tool_calls") else "stop")
                        values = [chunks(mixed_call()), lambda data: chunks(fixture.call("fresh", "edit_file", {"path": "fresh.txt", "old": "", "new": "requested"})), chunks(fixture.response("done"))]
                    with backend(protocol, values, captured, stream=streamed and protocol != "chat_completions") as url:
                        config = replace(self.config, base_url=url, wire_api=protocol, proxy="direct", stream=streamed)
                        tools = self.tools(lambda _: [["B"]], config, lambda text: approvals.append(text) or True)
                        rows, complete = Agent(config, Provider(config), tools, lambda _: None).run("task")
                    self.assertTrue(complete)
                    self.assertEqual(len(captured), 3)
                    self.assertEqual(len(approvals), 1)
                    self.assertEqual((self.root / "fresh.txt").read_text(), "requested")
                    def objects(value):
                        if isinstance(value, dict):
                            yield value
                            for child in value.values():
                                yield from objects(child)
                        elif isinstance(value, list):
                            for child in value:
                                yield from objects(child)
                        elif isinstance(value, str) and value.startswith("{"):
                            try:
                                yield from objects(json.loads(value))
                            except ValueError:
                                pass
                    result = next(row for row in objects(captured[1]["body"]) if row.get("answered") is True)
                    self.assertEqual(result["answers"], [["B"]])
                    self.assertTrue(result["not_authorization"])
                    (self.root / "fresh.txt").unlink()
                    self.assertFalse((self.root / "old-before.txt").exists())


class QuestionTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    setUp = fixture.TuiTests.setUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click

    async def test_single_multiple_custom_and_fresh_action_approval_preserve_main_draft(self):
        questions = [*prompts(), {"header": "Targets", "question": "Which targets?", "options": prompts()[0]["options"], "multiple": True},
                     {"header": "Note", "question": "Any constraints?"}]
        first = mixed_call()
        first["tool_calls"][1] = question_call(questions=questions)["tool_calls"][0]
        provider = fixture.ScriptedProvider([first, fixture.call("fresh", "edit_file", {"path": "fresh.txt", "old": "", "new": "requested"}), fixture.response("done")])
        app = self.app(provider, task="start", save_session="answers")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, QuestionScreen))
            editor = app.query_one("#task", TaskInput)
            editor.load_text("UNSENT_DRAFT")
            self.assertFalse((self.root / "old-before.txt").exists())
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(app.agent.runtime_state.phase, "question")
            await pilot.press("down", "enter")
            await self.until(pilot, lambda: app.screen.index == 1)
            app.screen.query_one("#question-options", QuestionOptions).highlighted = 0
            await pilot.press("space", "down", "space", "enter")
            await self.until(pilot, lambda: app.screen.index == 2)
            app.screen.query_one("#question-answer", Input).value = "keep tests intact"
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertFalse((self.root / "fresh.txt").exists())
            answer = next(json.loads(row["content"]) for row in provider.calls[1] if row.get("tool_call_id") == "question")
            self.assertEqual(answer["answers"], [["B"], ["A", "B"], ["keep tests intact"]])
            self.assertTrue(answer["not_authorization"])
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual((self.root / "fresh.txt").read_text(), "requested")
            self.assertEqual(editor.text, "UNSENT_DRAFT")
            self.assertIsNone(app.question)
            self.assertEqual(app.store.load("answers"), app.messages)

    async def test_partial_form_dismissal_does_not_send_drafts_or_continue_model(self):
        provider = fixture.ScriptedProvider([question_call(questions=[*prompts(), {"header": "Text", "question": "More?"}]), fixture.response("new task completed")])
        app = self.app(provider, task="start", save_session="dismissed")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, QuestionScreen))
            await pilot.press("enter")
            await self.until(pilot, lambda: app.screen.index == 1)
            app.screen.query_one("#question-answer", Input).value = "PRIVATE_NOT_SUBMITTED"
            await pilot.press("escape")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(app.task_status, 2)
            self.assertNotIn("PRIVATE_NOT_SUBMITTED", json.dumps(app.messages))
            self.assertNotIn("PRIVATE_NOT_SUBMITTED", (app.session_dir / "dismissed.json").read_text(encoding="utf-8"))
            editor = app.query_one("#task", TaskInput)
            editor.load_text("explicit new task")
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(provider.calls) == 2)
            self.assertIn("new task completed", app.transcript)

    async def test_previous_question_preserves_drafts_and_pending_restore_does_not_reopen(self):
        questions = [*prompts(), {"header": "Text", "question": "More?"}]
        provider = fixture.ScriptedProvider([question_call(questions=questions), fixture.response("done")])
        app = self.app(provider, task="start", save_session="pending-question")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, QuestionScreen))
            await pilot.press("down", "enter")
            await self.until(pilot, lambda: app.screen.index == 1)
            app.screen.query_one("#question-answer", Input).value = "custom draft"
            await self.click(pilot, "#question-back")
            self.assertEqual(app.screen.index, 0)
            self.assertEqual(app.screen.answers[0], ["B"])
            self.assertEqual(app.screen.selected[0], {1})
            await self.click(pilot, "#question-next")
            self.assertEqual(app.screen.answers[0], ["B"])
            self.assertEqual(app.screen.query_one("#question-answer", Input).value, "custom draft")
            # Saved intent contains no partially completed form answers.
            raw = (app.session_dir / "pending-question.json").read_text(encoding="utf-8")
            self.assertNotIn("custom draft", raw)
        app.close()
        restored_provider = fixture.ScriptedProvider([])
        restored = self.app(restored_provider, resume="pending-question")
        async with restored.run_test(size=(110, 36)) as pilot:
            self.assertFalse(restored.busy)
            self.assertIsNone(restored.question)
            self.assertEqual(restored_provider.calls, [])
            self.assertIn("NOT replayed", restored.messages[-1]["content"])

    async def test_mid_question_guidance_closes_old_form_and_replans(self):
        provider = fixture.ScriptedProvider([mixed_call(), fixture.response("following C")])
        app = self.app(provider, task="start")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, QuestionScreen))
            await pilot.press("ctrl+l")
            self.assertEqual(app.focused.id, "question-guidance")
            app.screen.query_one("#question-guidance", Input).value = "new request C"
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 2)
            self.assertIn("new request C", json.dumps(provider.calls[-1]))
            self.assertFalse((self.root / "old-before.txt").exists())
            self.assertIsNone(app.question)

    async def test_ctrl_c_and_unmount_release_waiting_worker_without_answers(self):
        for mode in ("cancel", "unmount"):
            provider = fixture.ScriptedProvider([mixed_call()])
            app = self.app(provider, task="start")
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, lambda: isinstance(app.screen, QuestionScreen))
                if mode == "cancel":
                    await pilot.press("ctrl+c")
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual(app.last_status, 130)
            app.close()
            self.assertFalse(app.agent._running)
            self.assertIsNone(app.agent._current_history)
            self.assertFalse((self.root / "old-before.txt").exists())

    async def test_oversized_answer_stays_in_form_without_network_and_can_be_corrected(self):
        self.config = replace(self.config, max_output=256)
        provider = fixture.ScriptedProvider([question_call(questions=[{"header": "Text", "question": "Explain?"}]), fixture.response("done")])
        app = self.app(provider, task="start")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, QuestionScreen))
            editor = app.screen.query_one("#question-answer", Input)
            editor.value = "中" * 200
            await pilot.press("enter")
            self.assertIsInstance(app.screen, QuestionScreen)
            self.assertEqual(editor.value, "中" * 200)
            self.assertEqual(len(provider.calls), 1)
            editor.value = "short answer"
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 2)

    async def test_small_terminal_can_reach_guidance_and_replan(self):
        long_question = "\n".join("长问题段落 " + str(i) for i in range(30))
        provider = fixture.ScriptedProvider([question_call(questions=[{**prompts()[0], "question": long_question}]), fixture.response("small terminal continued")])
        app = self.app(provider, task="start")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, QuestionScreen))
            prompt = app.screen.query_one("#question-prompt", TextArea)
            self.assertEqual(prompt.text, long_question)
            prompt.focus()
            prompt.move_cursor((29, 0))
            await self.until(pilot, lambda: prompt.scroll_y > 0)
            await pilot.press("ctrl+l")
            guide = app.screen.query_one("#question-guidance", Input)
            await self.until(pilot, lambda: app.focused is guide and guide.region.y >= 0 and guide.region.bottom <= app.size.height)
            guide.value = "follow new small-terminal request"
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertIn("small terminal continued", app.transcript)

    async def test_paste_rejection_preserves_complete_draft_and_does_not_submit(self):
        provider = fixture.ScriptedProvider([question_call(), fixture.response("done")])
        app = self.app(provider, task="start")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, QuestionScreen))
            for selector in ("#question-answer", "#question-guidance"):
                editor = app.screen.query_one(selector, Input)
                editor.value = "KEEP"
                editor.selection = (0, 4)
                for text in ("first\nLOST_SECOND", "line\r\nnext", "first\u2028second", "x" * (editor.max_length + 1)):
                    editor.post_message(Paste(text))
                    await pilot.pause()
                    self.assertEqual(editor.value, "KEEP")
                    self.assertEqual(len(provider.calls), 1)
                editor.post_message(Paste("whole answer"))
                await pilot.pause()
                self.assertEqual(editor.value, "whole answer")
            await pilot.press("escape")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 1)


if __name__ == "__main__":
    unittest.main()
