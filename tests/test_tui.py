"""Exercise the actual Textual app and real tool side effects without an API."""

import asyncio
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from textual.widgets import Input, Static, TextArea
from textual.css.query import NoMatches
from sprig_agent import cli, session
from sprig_agent.config import Config
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tui import ApprovalScreen, SprigApp


def response(text="Done"):
    return {"role": "assistant", "content": text}


def call(identity, name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class ScriptedProvider:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.calls = []

    def complete(self, messages, tools, on_text=None):
        self.calls.append(copy.deepcopy(messages))
        message = next(self.messages)
        if on_text and message.get("content"):
            on_text(message["content"])
        return message


class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Debug stack capture is particularly expensive on a WSL-mounted venv.
        asyncio.get_running_loop().set_debug(False)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "test", base_url="http://localhost/v1", stream=True, timeout=5)

    def app(self, provider, **kwargs):
        app = SprigApp(self.config, session_dir=self.parent / "sessions", provider=provider, **kwargs)
        self.addCleanup(app.close)
        return app

    async def send_text(self, pilot, text):
        editor = pilot.app.query_one("#task", TextArea)
        editor.load_text(text)
        editor.focus()
        await pilot.press("enter")

    async def test_slash_controls_are_local_and_config_keys_are_withheld(self):
        from dataclasses import replace
        self.config = replace(self.config, api_key="fixture-private-config-key")
        (self.root / "sample.py").write_text("def selected_function():\n    pass\n", encoding="utf-8")
        provider = ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            for command in ("/help", "/config", "/status", "/usage", "/context", "/not-a-command"):
                await self.send_text(pilot, command)
            await self.send_text(pilot, "/map selected_function")
            await self.until(pilot, lambda: not app.busy and "selected_function" in app.transcript)
            self.assertNotIn(self.config.api_key, app.transcript)
            self.assertIn('"api_key_present": true', app.transcript)
            self.assertIn("未知本地命令", app.transcript)
            self.assertEqual(provider.calls, [])
            self.assertIsNone(app.messages)

    async def test_slash_readonly_cannot_bypass_disabled_buttons_or_approval(self):
        from dataclasses import replace
        self.config = replace(self.config, read_only=True)
        provider = ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            for command in ("/diff", "/git", "/undo", "/redo"):
                await self.send_text(pilot, command)
                self.assertFalse(app.busy)
                self.assertNotIsInstance(app.screen, ApprovalScreen)
            self.assertIn("只读模式禁止", app.transcript)
            self.assertEqual(provider.calls, [])

    async def test_new_conversation_confirms_detaches_autosave_and_releases_lease(self):
        provider = ScriptedProvider([response("first result"), response("second result")])
        app = self.app(provider, save_session="original")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.send_text(pilot, "first task")
            await self.until(pilot, lambda: not app.busy)
            saved = copy.deepcopy(app.messages)
            await self.send_text(pilot, "/new")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("enter")
            self.assertEqual(app.messages, saved)
            self.assertEqual(app.session_name, "original")
            await self.send_text(pilot, "/new")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            self.assertIsNone(app.messages)
            self.assertIsNone(app.session_name)
            self.assertEqual(app.pending_tasks, [])
            other = SessionStore(self.parent / "sessions", self.config)
            try:
                self.assertEqual(other.load("original"), saved)  # Lease was released.
                await self.send_text(pilot, "second task")
                await self.until(pilot, lambda: not app.busy)
                self.assertNotIn("first task", json.dumps(provider.calls[-1]))
                self.assertEqual(other.load("original"), saved)  # Old autosave was detached.
            finally:
                other.close()
            self.assertEqual(len(provider.calls), 2)

    async def test_save_as_command_forks_transcript_without_overwriting_old_session(self):
        provider = ScriptedProvider([response("first"), response("second")])
        app = self.app(provider, save_session="original")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.send_text(pilot, "first task")
            await self.until(pilot, lambda: not app.busy)
            saved = copy.deepcopy(app.messages)
            await self.send_text(pilot, "/save fork")
            self.assertEqual(app.session_name, "fork")
            await self.send_text(pilot, "next task")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.store.load("original"), saved)
            self.assertEqual(app.store.load("fork"), app.messages)
            self.assertEqual(len(provider.calls), 2)

    async def test_local_commands_during_generation_do_not_become_model_guidance(self):
        entered, gate = threading.Event(), threading.Event()
        provider = ScriptedProvider([response("cancelled")])
        original = provider.complete

        def complete(messages, tools, on_text=None):
            entered.set()
            gate.wait(15)
            return original(messages, tools, on_text)

        provider.complete = complete
        app = self.app(provider, task="original")
        try:
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, entered.is_set)
                await self.send_text(pilot, "/status")
                await self.send_text(pilot, "/new")
                self.assertEqual(app.pending_tasks, [])
                self.assertNotIsInstance(app.screen, ApprovalScreen)
                await self.send_text(pilot, "/cancel")
                gate.set()
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(app.last_status, 130)
                self.assertNotIn("/cancel", json.dumps(app.messages))
        finally:
            gate.set()

    async def until(self, pilot, predicate):
        for _ in range(150):
            try:
                if predicate():
                    await pilot.pause()  # Drain mount/layout events before interacting with a new screen.
                    if predicate():
                        return  # The condition must still hold after queued UI events.
            except NoMatches:
                pass  # Screen is on the stack before its children have mounted.
            await pilot.pause(0.02)
        self.fail("UI condition did not become true")

    async def click(self, pilot, selector):
        # Focus/scroll callbacks can be deferred past a refresh. Settle them
        # before computing a point, then click the button interior (its border
        # can be clipped by the scrolling viewport).
        button = pilot.app.screen.query_one(selector)
        button.focus(scroll_visible=False)
        await self.until(pilot, lambda: pilot.app.screen.focused is button)
        await pilot.wait_for_scheduled_animations()
        # Textual suppresses clicks while the previous activation effect is
        # active. A hit-tested pointer click alone does not imply Pressed.
        await self.until(pilot, lambda: not button.has_class("-active"))
        button.scroll_visible(animate=False, immediate=True)
        await pilot.pause()
        # Widget.size excludes the border; use region so a one-row button's
        # content is clicked instead of its clipped top border.
        offset = (button.region.width // 2, button.region.height // 2)
        point = button.region.offset + offset
        self.assertIs(pilot.app.get_widget_at(*point)[0], button, "button center must be visible: " + selector)
        self.assertTrue(await pilot.click(selector, offset=offset), "button must be visible: " + selector)

    async def test_scoped_rules_replan_before_showing_fresh_edit_approval(self):
        (self.root / "src").mkdir()
        (self.root / "src/a.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("Use the project checks.", encoding="utf-8")
        (self.root / "src/AGENTS.md").write_text("SRC SPECIFIC RULE", encoding="utf-8")
        arguments = {"path": "src/a.py", "old": "1", "new": "2"}
        provider = ScriptedProvider([call("early", "edit_file", arguments),
                                     call("fresh", "edit_file", arguments), response("Stopped after rejection")])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            app.query_one("#task", TextArea).load_text("Propose the value fix")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertEqual(len(provider.calls), 2)
            self.assertNotIn("SRC SPECIFIC RULE", provider.calls[0][0]["content"])
            self.assertIn("SRC SPECIFIC RULE", provider.calls[1][0]["content"])
            self.assertIn("instructions are new or changed", app.messages[3]["content"])
            self.assertEqual((self.root / "src/a.py").read_text(), "value = 1\n")
            await pilot.press("enter")  # Fresh guidance cannot turn default rejection into approval.
            await self.until(pilot, lambda: not app.busy)
            self.assertFalse(json.loads(app.messages[5]["content"])["approved"])
            self.assertEqual((self.root / "src/a.py").read_text(), "value = 1\n")

    async def test_local_undo_redo_confirm_diff_and_mark_saved_acceptance_stale(self):
        provider = ScriptedProvider([call("write", "edit_file", {"path": "new.txt", "old": "", "new": "created content"}), response("Done")])
        app = self.app(provider, save_session="undo")
        async with app.run_test(size=(110, 36)) as pilot:
            app.query_one("#task", TextArea).load_text("create a file")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            calls = len(provider.calls)
            await self.click(pilot, "#changes")
            await self.until(pilot, lambda: not app.busy and '"record_limit": 16' in app.transcript)
            await self.click(pilot, "#undo")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("UNDO", app.screen.query_one("#approval-detail", TextArea).text)
            self.assertIn("-created content", app.screen.query_one("#approval-detail", TextArea).text)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertTrue((self.root / "new.txt").exists())
            await self.click(pilot, "#undo")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertFalse((self.root / "new.txt").exists())
            self.assertEqual(app.last_status, 2)
            self.assertIn("stale", app.store.load("undo")[-1]["content"])
            await self.click(pilot, "#redo")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual((self.root / "new.txt").read_text(), "created content")
            self.assertEqual(len(provider.calls), calls)
            self.assertEqual(validate_history(app.messages), app.messages)

    async def test_context_preview_requires_explicit_confirmation_and_readonly_stays_disabled(self):
        self.config = Config(self.root, "test", read_only=True, stream=True)
        provider = ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            for identity in ("diff", "git", "undo", "redo"):
                self.assertTrue(app.query_one("#" + identity).disabled)
            await self.click(pilot, "#map")
            await self.until(pilot, lambda: not app.busy)
            for identity in ("diff", "git", "undo", "redo"):
                self.assertTrue(app.query_one("#" + identity).disabled)
            await self.click(pilot, "#context-budget")
            await self.click(pilot, "#usage")
            await self.until(pilot, lambda: '"budget_bytes"' in app.transcript)
            await self.click(pilot, "#compact")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("已批准完成的旧编辑正文", app.screen.description)
            await pilot.press("enter")
            await self.until(pilot, lambda: not isinstance(app.screen, ApprovalScreen))
            self.assertFalse(app.agent.compact_context)
            await self.click(pilot, "#compact")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not isinstance(app.screen, ApprovalScreen))
            self.assertTrue(app.agent.compact_context)
            self.assertEqual(provider.calls, [])

    async def test_stream_is_visible_before_finish_and_history_continues(self):
        gate = threading.Event()
        provider = ScriptedProvider([response("first"), response("second")])
        original = provider.complete

        def complete(messages, tools, on_text=None):
            if not provider.calls:
                on_text("first")
                gate.wait(30)
            return original(messages, tools, on_text)

        provider.complete = complete
        app = self.app(provider)
        try:
            async with app.run_test(size=(110, 36)) as pilot:
                app.query_one("#task", TextArea).load_text("one\nwith multiple lines")
                await pilot.press("enter")
                await self.until(pilot, lambda: "first" in app.transcript)
                self.assertTrue(app.busy)
                self.assertFalse(app.query_one("#send").disabled)
                self.assertFalse(app.query_one("#task").disabled)
                await pilot.pause(0.01)
                gate.set()
                await self.until(pilot, lambda: not app.busy)
                app.query_one("#task", TextArea).load_text("two")
                await pilot.press("enter")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(provider.calls[1][-2:], [response("first"), {"role": "user", "content": "two"}])
                self.assertIn("second", app.transcript)
        finally:
            gate.set()

    async def test_small_stream_fragments_are_coalesced_visible_and_ordered(self):
        gate = threading.Event()
        text = "中文🙂" * 500
        provider = ScriptedProvider([])

        def complete(messages, tools, on_text=None):
            provider.calls.append(copy.deepcopy(messages))
            for character in text:
                on_text(character)
            gate.wait(10)
            return response(text)

        provider.complete = complete
        app = self.app(provider, task="burst")
        rendered = []
        original = app._append

        def append(chunk):
            rendered.append(chunk)
            original(chunk)

        app._append = append
        try:
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, lambda: text in app.transcript)
                self.assertTrue(app.busy)
                self.assertLess(len(rendered), 50, "render batches, not each of the 1500 fragments")
                gate.set()
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(app.transcript.count(text), 1)
                self.assertLess(app.transcript.index(text), app.transcript.index("[本轮结束]"))
                self.assertEqual(app.messages[-1], response(text))
                self.assertEqual(app.query_one("#transcript", TextArea).text, app.transcript)
                self.assertEqual(app._output_size, 0)
        finally:
            gate.set()

    async def test_large_fragment_is_bounded_and_flushes_before_approval(self):
        text = "中文🙂" * 500
        message = call("edit", "edit_file", {"path": "value.txt", "old": "", "new": "approved only"})
        message["content"] = text
        app = self.app(ScriptedProvider([message, response("finished")]), task="bounded")
        app.OUTPUT_BUFFER_LIMIT = 128
        sizes = []
        original = app.flush_output

        def flush():
            with app._output_condition:
                sizes.append(app._output_size)
            original()

        app.flush_output = flush
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn(text, app.transcript)
            self.assertGreater(len([size for size in sizes if size]), 1)
            self.assertLessEqual(max(sizes), 128)
            self.assertFalse((self.root / "value.txt").exists())
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual((self.root / "value.txt").read_text(), "approved only")
            self.assertIn("finished", app.transcript)

    async def test_cancel_releases_producer_waiting_for_full_output_buffer(self):
        stopped = threading.Event()
        provider = ScriptedProvider([])

        def complete(messages, tools, on_text=None):
            try:
                on_text("x" * 1024)
                self.fail("a full buffer must apply backpressure")
            finally:
                stopped.set()

        provider.complete = complete
        app = self.app(provider, task="cancel burst")
        app.OUTPUT_BUFFER_LIMIT = 128
        app.OUTPUT_REFRESH_SECONDS = 30
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: app._output_size == 128)
            self.assertFalse(stopped.is_set())
            await pilot.press("ctrl+c")
            await self.until(pilot, lambda: not app.busy)
            self.assertTrue(stopped.is_set())
            self.assertEqual(app.last_status, 130)
            self.assertEqual(app._output_size, 0)

    async def test_unmount_releases_producer_waiting_for_full_output_buffer(self):
        stopped = threading.Event()
        provider = ScriptedProvider([])

        def complete(messages, tools, on_text=None):
            try:
                on_text("x" * 1024)
                return call("late", "edit_file", {"path": "late.txt", "old": "", "new": "bad"})
            finally:
                stopped.set()

        provider.complete = complete
        app = self.app(provider, task="close burst")
        app.OUTPUT_BUFFER_LIMIT = 128
        app.OUTPUT_REFRESH_SECONDS = 30
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: app._output_size == 128)
            self.assertFalse(stopped.is_set())
        self.assertTrue(await asyncio.to_thread(stopped.wait, 3))
        app.close()
        self.assertIsNone(app.tools.root.fd)
        self.assertFalse((self.root / "late.txt").exists())

    async def test_enter_sends_shift_enter_newline_and_empty_enter_is_ignored(self):
        provider = ScriptedProvider([response("received")])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TextArea)
            await pilot.press("enter")
            self.assertEqual(provider.calls, [])
            self.assertEqual(editor.text, "")
            await pilot.press("a", "shift+enter", "b", "ctrl+j", "c")
            self.assertEqual(editor.text, "a\nb\nc")
            self.assertEqual(provider.calls, [])
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertEqual(provider.calls[0][-1], {"role": "user", "content": "a\nb\nc"})
            self.assertEqual(editor.text, "")

    async def test_guidance_during_stream_discards_stale_tools_and_keeps_fifo_history(self):
        gate, entered = threading.Event(), threading.Event()
        snapshots, active, peak = [], 0, 0

        class Provider:
            def complete(provider, messages, tools, on_text=None):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                snapshots.append(copy.deepcopy(messages))
                try:
                    if len(snapshots) == 1:
                        on_text("original plan")
                        entered.set()
                        gate.wait(30)
                        return call("stale", "edit_file", {"path": "stale.txt", "old": "", "new": "wrong"})
                    on_text("following updated requests")
                    return response("following updated requests")
                finally:
                    active -= 1

        app = self.app(Provider(), save_session="steering")
        try:
            async with app.run_test(size=(110, 36)) as pilot:
                editor = app.query_one("#task", TextArea)
                editor.load_text("initial task")
                await pilot.press("enter")
                await self.until(pilot, entered.is_set)
                for task in ("use blue", "preserve existing tests"):
                    editor.load_text(task)
                    await pilot.press("enter")
                self.assertTrue(app.busy)
                self.assertEqual(len(snapshots), 1)
                self.assertEqual(app.pending_tasks, ["use blue", "preserve existing tests"])
                gate.set()
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(peak, 1)
                self.assertEqual(len(snapshots), 2)
                self.assertEqual([m["content"] for m in snapshots[1] if m["role"] == "user"],
                                 ["initial task", "use blue", "preserve existing tests"])
                self.assertFalse(any(m.get("tool_calls") for m in app.messages))
                self.assertFalse((self.root / "stale.txt").exists())
                self.assertEqual(app.messages, validate_history(app.messages))
                self.assertEqual(app.store.load("steering"), app.messages)
                self.assertIn("following updated requests", app.transcript)
        finally:
            gate.set()

    async def test_guidance_during_command_keeps_result_and_skips_remaining_calls(self):
        self.config = Config(self.root, "test", base_url="http://localhost/v1", stream=True, timeout=30)
        script = ("from pathlib import Path;import time,sys\n"
                  "sys.stdout.reconfigure(encoding='utf-8')\n"
                  "Path('started').write_text('yes')\n"
                  "print('COMMAND_STARTED 中文🙂', flush=True)\n"
                  "while not Path('release').exists(): time.sleep(0.01)\n"
                  "print('COMMAND_FINISHED')")
        first = call("run-first", "run", {"argv": [sys.executable, "-c", script]})
        first["tool_calls"].extend(call("stale-edit", "edit_file",
                                       {"path": "stale.txt", "old": "", "new": "wrong"})["tool_calls"])
        provider = ScriptedProvider([first, response("adjusted")])
        app = self.app(provider, task="initial task", save_session="command")
        try:
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: (self.root / "started").exists())
                await self.until(pilot, lambda: "COMMAND_STARTED 中文🙂" in app.transcript)
                self.assertTrue(app.busy)
                editor = app.query_one("#task", TextArea)
                editor.focus()
                editor.load_text("keep command evidence but change the remaining plan")
                await pilot.press("enter")
                self.assertTrue(app.busy)
                self.assertEqual(len(provider.calls), 1)
                (self.root / "release").write_text("go")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(len(provider.calls), 2)
                results = [m for m in provider.calls[1] if m["role"] == "tool"]
                self.assertEqual(json.loads(results[0]["content"])["returncode"], 0)
                self.assertIn("COMMAND_FINISHED", results[0]["content"])
                self.assertLess(app.transcript.index("COMMAND_STARTED"),
                                app.transcript.index("你> keep command evidence"))
                self.assertTrue(json.loads(results[1]["content"])["skipped"])
                self.assertEqual(provider.calls[1][-1]["role"], "user")
                self.assertFalse((self.root / "stale.txt").exists())
                self.assertEqual(app.messages, validate_history(app.messages))
                self.assertEqual(app.store.load("command"), app.messages)
        finally:
            (self.root / "release").write_text("go")

    async def test_guidance_in_approval_replans_and_requires_a_fresh_diff(self):
        file = self.root / "value.txt"
        file.write_text("old", encoding="utf-8")
        provider = ScriptedProvider([
            call("old-edit", "edit_file", {"path": "value.txt", "old": "old", "new": "red"}),
            call("new-edit", "edit_file", {"path": "value.txt", "old": "old", "new": "blue"}),
            response("blue applied")])
        app = self.app(provider, task="change color")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            editor = app.screen.query_one("#approval-task", TextArea)
            await pilot.press("ctrl+l")
            self.assertEqual(app.focused.id, "approval-task")
            editor.load_text("use blue instead")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen)
                             and "+blue" in app.screen.query_one("#approval-detail", TextArea).text)
            self.assertEqual(file.read_text(), "old")
            self.assertEqual(provider.calls[1][-1], {"role": "user", "content": "use blue instead"})
            self.assertFalse(json.loads(provider.calls[1][-2]["content"])["approved"])
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(file.read_text(), "blue")
            self.assertEqual(app.messages, validate_history(app.messages))

    async def test_guidance_during_acceptance_returns_to_model_before_rechecking(self):
        self.config = Config(self.root, "test", base_url="http://localhost/v1", stream=True,
                             checks=((sys.executable, "-c", "print('CHECK_OK')"),))
        provider = ScriptedProvider([response("first plan done"), response("updated plan done")])
        app = self.app(provider, task="verify")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            editor = app.screen.query_one("#approval-task", TextArea)
            editor.focus()
            editor.load_text("review the updated requirement first")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and len(provider.calls) == 2)
            self.assertEqual(provider.calls[1][-1]["content"], "review the updated requirement first")
            self.assertNotIn("All user checks passed", app.transcript)
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 0)
            self.assertIn("All user checks passed", app.transcript)
            self.assertEqual(app.messages, validate_history(app.messages))

    async def test_late_guidance_at_completion_reuses_worker_without_losing_request(self):
        provider = ScriptedProvider([response("initial done"), response("late request done")])
        app = self.app(provider, task="initial")
        original_finished = app.finished
        executions = []
        original_turn = app.execute_turn

        def turn(task, local):
            executions.append(threading.get_ident())
            return original_turn(task, local)

        # Directly inject the same UI handler at the completion boundary.
        def inject(status, message):
            if len(provider.calls) == 1:
                from sprig_agent.tui import TaskInput
                editor = app.query_one("#task", TaskInput)
                app.submitted(TaskInput.Submitted(editor, "late request"))
            return original_finished(status, message)

        app.execute_turn, app.finished = turn, inject
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(provider.calls[1][-1], {"role": "user", "content": "late request"})
            self.assertEqual(len(set(executions)), 1)
            self.assertEqual(app.messages, validate_history(app.messages))

    async def test_cancel_preserves_queued_guidance_for_explicit_continue(self):
        gate, entered = threading.Event(), threading.Event()
        provider = ScriptedProvider([response("cancelled response"), response("resumed with guidance")])
        original = provider.complete

        def complete(messages, tools, on_text=None):
            if not provider.calls:
                entered.set()
                gate.wait(30)
            return original(messages, tools, on_text)

        provider.complete = complete
        app = self.app(provider, task="original", save_session="cancel-guidance")
        try:
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, entered.is_set)
                app.query_one("#task", TextArea).load_text("keep this correction")
                await pilot.press("enter")
                await pilot.press("ctrl+c")
                gate.set()
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(app.last_status, 130)
                self.assertEqual(len(provider.calls), 1)
                self.assertEqual(app.messages[-1], {"role": "user", "content": "keep this correction"})
                self.assertEqual(app.store.load("cancel-guidance"), app.messages)
                await self.click(pilot, "#continue")
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(len(provider.calls), 2)
                self.assertEqual(provider.calls[1][-1]["content"], "keep this correction")
                self.assertEqual(app.messages, validate_history(app.messages))
        finally:
            gate.set()

    async def test_close_waits_for_worker_before_releasing_handles(self):
        gate, entered = threading.Event(), threading.Event()
        errors = []
        provider = ScriptedProvider([response("late")])
        original = provider.complete

        def complete(messages, tools, on_text=None):
            entered.set()
            gate.wait(10)
            return original(messages, tools, on_text)

        provider.complete = complete
        app = self.app(provider, task="original", save_session="shutdown")
        closer = None
        try:
            async with app.run_test(size=(110, 36)) as pilot:
                await self.until(pilot, entered.is_set)

            def close():
                try:
                    app.close()
                except Exception as error:
                    errors.append(error)

            closer = threading.Thread(target=close)
            closer.start()
            await asyncio.sleep(0.03)
            self.assertTrue(closer.is_alive())
            self.assertIsNotNone(app.tools.root.fd)
            gate.set()
            closer.join(5)
            self.assertFalse(closer.is_alive())
            self.assertEqual(errors, [])
            self.assertIsNone(app.tools.root.fd)
            self.assertIsNone(app.store.handle.fd)
        finally:
            gate.set()
            if closer:
                closer.join(5)

    async def test_diff_defaults_to_rejection_then_explicit_approval_writes(self):
        file = self.root / "value.txt"
        file.write_text("old", encoding="utf-8")
        arguments = {"path": "value.txt", "old": "old", "new": "new"}
        provider = ScriptedProvider([call("edit-1", "edit_file", arguments), response(),
                                     call("edit-2", "edit_file", arguments), response()])
        app = self.app(provider, task="fix")
        async with app.run_test(size=(100, 32)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            detail = app.screen.query_one("#approval-detail", TextArea).text
            self.assertIn("-old", detail)
            self.assertIn("+new", detail)
            self.assertEqual(app.focused.id, "reject")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(file.read_text(), "old")
            app.query_one("#task", TextArea).load_text("fix again")
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("ctrl+enter")  # Submission shortcuts must never approve a modal.
            self.assertEqual(file.read_text(), "old")
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(file.read_text(), "new", app.transcript + repr(app.messages))
            self.assertTrue(json.loads(app.messages[-2]["content"])["changed"])

    async def test_acceptance_command_requires_ui_approval_and_real_success(self):
        self.config = Config(self.root, "test", base_url="http://localhost/v1", stream=True,
                             checks=((sys.executable, "-c", "print('CHECK_OK')"),))
        app = self.app(ScriptedProvider([response()]), task="verify")
        async with app.run_test(size=(100, 32)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("CHECK_OK", app.screen.query_one("#approval-detail", TextArea).text)
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 0)
            self.assertIn("All user checks passed", app.transcript)
            self.assertEqual(json.loads(app.messages[-1]["content"])["returncode"], 0)

    async def test_rejecting_acceptance_marks_task_unverified(self):
        self.config = Config(self.root, "test", base_url="http://localhost/v1", stream=True,
                             checks=((sys.executable, "-c", "raise SystemExit(99)"),))
        provider = ScriptedProvider([response("All tests pass")])
        app = self.app(provider, task="verify")
        async with app.run_test(size=(100, 32)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("escape")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 2)
            self.assertNotIn("All user checks passed", app.transcript)
            self.assertEqual(len(provider.calls), 1)

    async def test_local_inspections_and_save_cannot_erase_rejected_acceptance(self):
        self.config = Config(self.root, "test", stream=True,
                             checks=((sys.executable, "-c", "raise SystemExit(99)"),))
        provider = ScriptedProvider([response("All tests pass")])
        app = self.app(provider, task="verify")
        async with app.run_test(size=(110, 40)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("escape")
            await self.until(pilot, lambda: not app.busy)
            task_message = app.task_message
            for identity in ("map", "changes", "sessions"):
                await self.click(pilot, "#" + identity)
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(app.last_operation_status, 0)
                self.assertEqual(app.last_status, 2)
                self.assertEqual(app.task_message, task_message)
            await self.click(pilot, "#context-budget")
            await self.click(pilot, "#usage")
            app.query_one("#session-name", Input).value = "unverified"
            await self.click(pilot, "#save")
            await self.until(pilot, lambda: (self.parent / "sessions/unverified.json").exists())
            self.assertEqual(app.last_status, 2)
            self.assertIn(task_message, str(app.query_one("#task-state", Static).render()))
            self.assertEqual(len(provider.calls), 1)
            await pilot.press("ctrl+q")
        self.assertEqual(app.return_value, 2)

    async def test_local_usage_shows_real_request_records_without_network_or_history_changes(self):
        from sprig_agent.provider import Provider
        from tests.test_streaming import chunk, sse

        provider = Provider(self.config)
        provider.opener = Mock()
        provider.opener.open.return_value = io.BytesIO(sse(chunk({"content": "plan"}, "stop"), {
            "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}))
        app = self.app(provider, task="inspect")
        async with app.run_test(size=(110, 40)) as pilot:
            await self.until(pilot, lambda: not app.busy and app.messages is not None)
            history, status, task_message = copy.deepcopy(app.messages), app.last_status, app.task_message
            await self.click(pilot, "#usage")
            self.assertIn('"complete_sum": 12', app.transcript)
            self.assertEqual(app.messages, history)
            self.assertEqual((app.last_status, app.task_message), (status, task_message))
            self.assertEqual(provider.opener.open.call_count, 1)

    async def test_model_failure_survives_local_view_and_next_task_can_recover(self):
        provider = ScriptedProvider([response("recovered")])
        original = provider.complete
        attempts = []

        def complete(messages, tools, on_text=None):
            attempts.append(copy.deepcopy(messages))
            if len(attempts) == 1:
                raise ValueError("fixture connection failed")
            return original(messages, tools, on_text)

        provider.complete = complete
        app = self.app(provider, task="initial request")
        async with app.run_test(size=(110, 36)) as pilot:
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 1)
            await self.click(pilot, "#map")
            editor = app.query_one("#task", TextArea)
            await self.until(pilot, lambda: not app.busy and app.screen.focused is editor)
            self.assertEqual(app.last_status, 1)
            editor.load_text("retry after fixing connection")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and len(attempts) == 2)
            self.assertEqual(app.last_status, 0)
            self.assertIn("未配置验收命令", app.task_message)
            self.assertEqual([message["content"] for message in attempts[1] if message["role"] == "user"],
                             ["initial request", "retry after fixing connection"])

    async def test_verified_result_becomes_stale_after_undo_even_if_checkpoint_fails(self):
        self.config = Config(self.root, "test", stream=True,
                             checks=((sys.executable, "-c", "from pathlib import Path;assert Path('value.txt').read_text()=='ok'"),))
        provider = ScriptedProvider([call("edit", "edit_file", {"path": "value.txt", "old": "", "new": "ok"}), response()])
        app = self.app(provider, task="create and verify", save_session="verified")
        async with app.run_test(size=(110, 40)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen)
                             and "assert Path" in app.screen.query_one("#approval-detail", TextArea).text)
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 0)
            self.assertEqual(app.task_message, "验收通过")
            with patch.object(app.store, "save", side_effect=OSError("fixture disk full")):
                await self.click(pilot, "#undo")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
            self.assertFalse((self.root / "value.txt").exists())
            self.assertEqual(app.last_operation_status, 1)
            self.assertEqual(app.last_status, 2)
            self.assertIn("验收失效", app.task_message)
            self.assertIn("stale", app.messages[-1]["content"])
            await self.click(pilot, "#map")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 2)
            self.assertEqual(len(provider.calls), 2)

    async def test_approved_local_command_invalidates_result_when_cancelled(self):
        self.config = Config(self.root, "test", stream=True, timeout=30,
                             checks=((sys.executable, "-c", "assert 1+1==2"),))
        provider = ScriptedProvider([response()])
        app = self.app(provider, task="verify", save_session="local-command")
        async with app.run_test(size=(110, 40)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 0)
            script = "from pathlib import Path;import time;Path('started').write_text('yes');time.sleep(30)"
            app.start(local=("run", {"argv": [sys.executable, "-c", script]}))
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: (self.root / "started").exists())
            self.assertEqual(app.last_status, 2)
            await pilot.press("ctrl+c")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_operation_status, 130)
            self.assertEqual(app.last_status, 2)
            self.assertIn("stale", app.store.load("local-command")[-1]["content"])
            self.assertEqual(len(provider.calls), 1)

    async def test_cancel_command_kills_descendants_and_next_turn_does_not_replay(self):
        self.config = Config(self.root, "test", base_url="http://localhost/v1", stream=True, timeout=30)
        child = ("import time;from pathlib import Path\n"
                 "while not Path('release').exists(): time.sleep(0.01)\n"
                 "Path('late').write_text('bad')")
        script = ("import subprocess,sys,time;from pathlib import Path;"
                  "subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);"
                  "Path('started').write_text('yes');time.sleep(30)")
        provider = ScriptedProvider([call("run-1", "run", {"argv": [sys.executable, "-c", script]}), response("next")])
        app = self.app(provider, task="run")
        async with app.run_test(size=(100, 32)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: (self.root / "started").exists())
            await pilot.press("ctrl+c")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 130)
            self.assertIn("NOT replayed", app.messages[-1]["content"])
            (self.root / "release").write_text("go")
            await asyncio.sleep(0.3)
            self.assertFalse((self.root / "late").exists())
            app.query_one("#task", TextArea).load_text("continue safely")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 0)
            self.assertEqual(len(provider.calls), 2)

    async def test_cancel_pending_approval_never_writes(self):
        arguments = {"path": "created.txt", "old": "", "new": "unapproved"}
        app = self.app(ScriptedProvider([call("edit", "edit_file", arguments)]), task="create")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("ctrl+c")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_status, 130)
            self.assertFalse((self.root / "created.txt").exists())
            self.assertNotIsInstance(app.screen, ApprovalScreen)
            await self.click(pilot, "#map")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.last_operation_status, 0)
            self.assertEqual(app.last_status, 130)
            await pilot.press("ctrl+q")
        self.assertEqual(app.return_value, 130)

    async def test_cancel_waiting_model_does_not_execute_late_tools(self):
        gate, entered = threading.Event(), threading.Event()
        arguments = {"path": "created.txt", "old": "", "new": "unapproved"}
        provider = ScriptedProvider([call("late-edit", "edit_file", arguments)])
        original = provider.complete

        def complete(messages, tools, on_text=None):
            entered.set()
            gate.wait(30)
            return original(messages, tools, on_text)

        provider.complete = complete
        app = self.app(provider, task="create")
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await self.until(pilot, entered.is_set)
                await pilot.press("ctrl+c")
                self.assertTrue(app.busy)
                self.assertFalse((self.root / "created.txt").exists())
                gate.set()
                await self.until(pilot, lambda: not app.busy)
                self.assertEqual(app.last_status, 130)
                self.assertFalse((self.root / "created.txt").exists())
                self.assertFalse(any(message.get("tool_calls") for message in app.messages))
        finally:
            gate.set()

    async def test_ui_save_and_restore_keep_history_without_network(self):
        self.config = Config(self.root, "test", stream=True,
                             checks=((sys.executable, "-c", "assert 1+1==2"),))
        app = self.app(ScriptedProvider([response("saved")]), task="remember")
        async with app.run_test(size=(110, 40)) as pilot:
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy)
            self.assertEqual(app.task_message, "验收通过")
            app.query_one("#session-name", Input).value = "demo"
            app.query_one("#save").scroll_visible(animate=False)
            await pilot.pause()
            await self.click(pilot, "#save")
            await self.until(pilot, lambda: (self.parent / "sessions/demo.json").exists())
            self.assertTrue((self.parent / "sessions/demo.json").exists())
            history = copy.deepcopy(app.messages)
        app.close()
        forbidden = Mock()
        restored = self.app(forbidden)
        async with restored.run_test(size=(110, 40)) as pilot:
            restored.query_one("#session-name", Input).value = "demo"
            restored.query_one("#restore").scroll_visible(animate=False)
            await pilot.pause()
            await self.click(pilot, "#restore")
            await self.until(pilot, lambda: restored.messages is not None)
            self.assertEqual(restored.messages, history)
            self.assertEqual(restored.last_status, 2)
            self.assertIn("未重新验收", restored.task_message)
            self.assertIn("saved", restored.transcript)
            forbidden.complete.assert_not_called()

    async def test_failed_save_as_keeps_current_session_and_releases_new_name(self):
        provider = ScriptedProvider([response("saved history")])
        app = self.app(provider, task="remember", save_session="active")
        other = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(other.close)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.until(pilot, lambda: not app.busy)
            history = copy.deepcopy(app.messages)
            app.query_one("#session-name", Input).value = "replacement"
            # Exercise an immediate retry inside the normal click suppression
            # window, even on fast runners; the helper must wait for readiness.
            app.query_one("#save").active_effect_duration = 1
            with patch.object(app.store.handle, "replace", side_effect=OSError("fixture disk error")) as replace:
                await self.click(pilot, "#save")
                await self.until(pilot, lambda: replace.call_count == 1)
            self.assertEqual(app.session_name, "active")
            self.assertEqual(app.messages, history)
            self.assertFalse((self.parent / "sessions/replacement.json").exists())
            with self.assertRaisesRegex(SessionError, "busy"):
                other.load("active")
            other.ensure_new("replacement")
            other.release("replacement")
            await self.click(pilot, "#save")
            await self.until(pilot, lambda: app.session_name == "replacement")
            self.assertEqual(other.load("active"), history)
            with self.assertRaisesRegex(SessionError, "busy"):
                other.load("replacement")
            self.assertEqual(app.store.load("replacement"), history)
            self.assertEqual(len(provider.calls), 1)

    async def test_save_as_sync_error_keeps_active_session_and_allows_explicit_restore(self):
        provider = ScriptedProvider([response("saved history")])
        app = self.app(provider, task="remember", save_session="active")
        other = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(other.close)
        async with app.run_test(size=(110, 40)) as pilot:
            await self.until(pilot, lambda: not app.busy)
            history = copy.deepcopy(app.messages)
            app.query_one("#session-name", Input).value = "replacement"
            with patch.object(app.store.handle, "sync", side_effect=OSError("fixture sync error")) as sync:
                await self.click(pilot, "#save")
                await self.until(pilot, lambda: sync.call_count == 1)
            self.assertEqual(app.session_name, "active")
            with self.assertRaisesRegex(SessionError, "busy"):
                other.load("active")
            self.assertEqual(other.load("replacement"), history)
            other.release("replacement")
            await self.click(pilot, "#restore")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: app.session_name == "replacement")
            self.assertEqual(app.messages, history)
            self.assertEqual(other.load("active"), history)
            self.assertEqual(app.last_status, 2)
            self.assertEqual(len(provider.calls), 1)

    async def test_session_listing_is_local_and_deletion_requires_confirmation(self):
        forbidden = Mock()
        app = self.app(forbidden)
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "private text"}]
        app.get_store().save("demo", history)
        async with app.run_test(size=(110, 44)) as pilot:
            app.query_one("#sessions").scroll_visible(animate=False)
            await pilot.pause()
            await self.click(pilot, "#sessions")
            await self.until(pilot, lambda: not app.busy)
            self.assertIn('"demo"', app.transcript)
            self.assertNotIn("private text", app.transcript)
            self.assertIsNone(app.messages)
            self.assertIn('"limited": false', app.transcript)
            app.query_one("#session-name", Input).value = "demo"
            app.query_one("#delete").scroll_visible(animate=False)
            await pilot.pause()
            await self.click(pilot, "#delete")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await pilot.press("enter")
            self.assertTrue((self.parent / "sessions/demo.json").exists())
            await self.click(pilot, "#delete")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            await self.click(pilot, "#approve")
            self.assertFalse((self.parent / "sessions/demo.json").exists())
            forbidden.complete.assert_not_called()

    async def test_session_listing_limit_is_visible_and_does_not_create_task_result(self):
        forbidden = Mock()
        app = self.app(forbidden)
        store = app.get_store()
        for index in range(10):
            store.handle.write_new(f"ignored-{index}.txt", b"unused", private=True)
        with patch.object(session, "MAX_LIST_ENTRIES", 3):
            async with app.run_test(size=(110, 40)) as pilot:
                await self.click(pilot, "#sessions")
                await self.until(pilot, lambda: not app.busy)
                self.assertIn('"limited": true', app.transcript)
                self.assertIn('"scanned_entries": 3', app.transcript)
                self.assertEqual(app.last_operation_status, 2)
                self.assertIsNone(app.task_status)
                self.assertIn("结果可能不完整", str(app.query_one("#state", Static).render()))
                forbidden.complete.assert_not_called()

    async def test_cancel_session_listing_releases_borrowed_lock_without_model_request(self):
        entered, gate = threading.Event(), threading.Event()
        forbidden = Mock()
        app = self.app(forbidden)
        store = app.get_store()
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "private transcript"}]
        store.save("demo", history)
        store.release("demo")
        other = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(other.close)
        original = store.handle.read

        def read(*args, **kwargs):
            entered.set()
            gate.wait(10)
            return original(*args, **kwargs)

        try:
            with patch.object(store.handle, "read", side_effect=read):
                async with app.run_test(size=(110, 40)) as pilot:
                    await self.click(pilot, "#sessions")
                    await self.until(pilot, entered.is_set)
                    await pilot.press("ctrl+c")
                    gate.set()
                    await self.until(pilot, lambda: not app.busy)
                    self.assertEqual(app.last_operation_status, 130)
                    self.assertEqual(app.last_status, 130)
                    self.assertEqual(other.load("demo"), history)
                    self.assertNotIn("private transcript", app.transcript)
                    self.assertIsNone(app.messages)
                    forbidden.complete.assert_not_called()
        finally:
            gate.set()

    async def test_local_map_does_not_upload_and_controls_and_key_are_hidden(self):
        (self.root / "code.py").write_text("def function():\n    pass\n")
        self.config = Config(self.root, "test", base_url="http://localhost/v1", api_key="fake-secret", stream=True)
        forbidden = Mock()
        app = self.app(forbidden)
        async with app.run_test(size=(110, 36)) as pilot:
            await self.click(pilot, "#map")
            await self.until(pilot, lambda: not app.busy)
            self.assertIn("function", app.transcript)
            app.append("fake-secret\x1b[31m")
            self.assertNotIn("fake-secret", app.transcript)
            self.assertIn("[REDACTED]\\u001b", app.transcript)
            forbidden.complete.assert_not_called()


class TuiCliTests(unittest.TestCase):
    def test_pipe_is_rejected_before_network_or_ui_is_loaded(self):
        for stdin, stdout in ((False, True), (True, False)):
            with patch.object(cli.sys.stdin, "isatty", return_value=stdin), \
                    patch.object(cli.sys.stdout, "isatty", return_value=stdout), \
                    patch.object(cli, "Provider") as provider, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--no-config", "--tui", "--model", "test"]), 1)
                provider.assert_not_called()

    def test_tui_default_stream_and_saved_session_arguments_are_forwarded(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(cli.sys.stdin, "isatty", return_value=True), \
                    patch.object(cli.sys.stdout, "isatty", return_value=True), \
                    patch("sprig_agent.tui.run_tui", return_value=0) as run, \
                    patch.dict(os.environ, {"SPRIG_API_KEY": "", "OPENAI_API_KEY": ""}):
                self.assertEqual(cli.main(["--no-config", "--tui", "--model", "test", "--workdir", root,
                                           "--base-url", "http://localhost/v1", "--resume", "demo",
                                           "--metrics", "--request-usage"]), 0)
            self.assertTrue(run.call_args.args[0].stream)
            self.assertTrue(run.call_args.args[0].request_usage)
            self.assertTrue(run.call_args.kwargs["show_metrics"])
            self.assertEqual(run.call_args.args[-1], "demo")


if __name__ == "__main__":
    unittest.main()
