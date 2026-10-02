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

from textual.widgets import Input, TextArea
from textual.css.query import NoMatches
from sprig_agent import cli
from sprig_agent.config import Config
from sprig_agent.session import validate_history
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

    async def until(self, pilot, predicate):
        for _ in range(150):
            try:
                if predicate():
                    await pilot.pause()  # Drain mount/layout events before interacting with a new screen.
                    return
            except NoMatches:
                pass  # Screen is on the stack before its children have mounted.
            await pilot.pause(0.02)
        self.fail("UI condition did not become true")

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
        script = ("from pathlib import Path;import time\n"
                  "Path('started').write_text('yes')\n"
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
                await pilot.click("#approve")
                await self.until(pilot, lambda: (self.root / "started").exists())
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
            await pilot.click("#approve")
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
            await pilot.click("#approve")
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
                await pilot.click("#continue")
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
            await pilot.click("#approve")
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
            await pilot.click("#approve")
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
            await pilot.click("#approve")
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
        app = self.app(ScriptedProvider([response("saved")]), task="remember")
        async with app.run_test(size=(110, 40)) as pilot:
            await self.until(pilot, lambda: not app.busy)
            app.query_one("#session-name", Input).value = "demo"
            await pilot.click("#save")
            self.assertTrue((self.parent / "sessions/demo.json").exists())
            history = copy.deepcopy(app.messages)
        app.close()
        forbidden = Mock()
        restored = self.app(forbidden)
        async with restored.run_test(size=(110, 40)) as pilot:
            restored.query_one("#session-name", Input).value = "demo"
            await pilot.click("#restore")
            self.assertEqual(restored.messages, history)
            self.assertIn("saved", restored.transcript)
            forbidden.complete.assert_not_called()

    async def test_local_map_does_not_upload_and_controls_and_key_are_hidden(self):
        (self.root / "code.py").write_text("def function():\n    pass\n")
        self.config = Config(self.root, "test", base_url="http://localhost/v1", api_key="fake-secret", stream=True)
        forbidden = Mock()
        app = self.app(forbidden)
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.click("#map")
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
                self.assertEqual(cli.main(["--tui", "--model", "test"]), 1)
                provider.assert_not_called()

    def test_tui_default_stream_and_saved_session_arguments_are_forwarded(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(cli.sys.stdin, "isatty", return_value=True), \
                    patch.object(cli.sys.stdout, "isatty", return_value=True), \
                    patch("sprig_agent.tui.run_tui", return_value=0) as run, \
                    patch.dict(os.environ, {"SPRIG_API_KEY": "", "OPENAI_API_KEY": ""}):
                self.assertEqual(cli.main(["--tui", "--model", "test", "--workdir", root,
                                           "--base-url", "http://localhost/v1", "--resume", "demo"]), 0)
            self.assertTrue(run.call_args.args[0].stream)
            self.assertEqual(run.call_args.args[-1], "demo")


if __name__ == "__main__":
    unittest.main()
