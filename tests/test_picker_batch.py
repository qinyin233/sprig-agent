"""Cross-query file batches use the actual TUI, atomic backends and model wires."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Input, OptionList, Static
from sprig_agent.completion import file_candidates
from sprig_agent.filesystem import Directory
from sprig_agent.picker import PickerScreen
from sprig_agent.provider import Provider
from sprig_agent.tui import TaskInput
from tests import test_completion as completion
from tests import test_tui as fixture
from tests.test_generation_options import stream_body
from tests.test_native_protocols import backend, native


class BatchPickerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp
    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click

    async def ready_picker(self, pilot):
        try:
            return await completion.CompletionTuiTests.ready_picker(self, pilot)
        except AssertionError:
            picker = pilot.app.screen
            if isinstance(picker, PickerScreen):
                self.fail(f"Picker not ready: generation={picker.generation}, candidates={picker.values!r}, "
                          f"status={picker.query_one('#picker-status', Static).render()}")
            raise

    def setUp(self):
        fixture.TuiTests.setUp(self)
        # Positive admission cases should not depend on the shared runner's
        # 250 ms scheduling window. Exercise the real scanner with a controlled
        # fixture budget; zero-budget UI behavior is checked separately below.
        scanner = patch("sprig_agent.picker.file_candidates", side_effect=lambda *args: file_candidates(*args, seconds=5))
        scanner.start()
        self.addCleanup(scanner.stop)

    async def choose(self, pilot, path):
        picker = pilot.app.screen
        picker.query_one("#picker-query", Input).value = path
        await self.until(pilot, lambda: path in picker.values)
        picker.query_one("#picker-options", OptionList).highlighted = picker.values.index(path)
        picker.query_one("#picker-query", Input).focus()
        await pilot.press("ctrl+space")
        self.assertIn(path, picker.chosen)
        return picker

    async def test_live_and_snapshot_batches_reach_three_http_streams_only_after_send(self):
        names = ("说明 file.txt", "beta.txt")
        (self.root / "unselected.txt").write_text("DO_NOT_SEND_UNSELECTED", encoding="utf-8")
        for mode in ("references", "attach"):
            for wire in ("chat_completions", "messages", "responses"):
                with self.subTest(mode=mode, wire=wire):
                    for i, name in enumerate(names):
                        (self.root / name).write_text(f"BEFORE_{i}", encoding="utf-8")
                    captured = []
                    reply = stream_body({"role": "assistant", "content": "Done"}) if wire == "chat_completions" else native(wire, "Done")
                    with backend(wire, [reply], captured, stream=True) as url:
                        self.config = replace(self.config, base_url=url, wire_api=wire, proxy="direct", read_only=True)
                        app = self.app(Provider(self.config))
                        async with app.run_test(size=(80, 24)) as pilot:
                            draft = app.query_one("#task", TaskInput)
                            draft.load_text("Explain both files")
                            await self.click(pilot, "#files")
                            await self.ready_picker(pilot)
                            await self.choose(pilot, names[0])
                            picker = await self.choose(pilot, names[1])
                            self.assertEqual(tuple(picker.chosen), names)
                            self.assertGreaterEqual(picker.query_one("#picker-options").region.height, 1)
                            self.assertIn("已勾选 2/16", str(picker.query_one("#picker-selection", Static).render()))
                            self.assertEqual(captured, [])
                            if mode == "attach":
                                await self.click(pilot, "#picker-add")
                            else:
                                await pilot.press("enter")
                            await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
                            self.assertIsNotNone(app.messages)
                            self.assertEqual(draft.text, "Explain both files")
                            self.assertEqual(captured, [])
                            self.assertEqual(app.agent.reference_state()["files"], list(names) if mode == "references" else [])
                            for i, name in enumerate(names):
                                (self.root / name).write_text(f"AFTER_{i}", encoding="utf-8")
                            draft.focus()
                            await pilot.press("enter")
                            await self.until(pilot, lambda: not app.busy and len(captured) == 1)
                            body = json.dumps(captured[0]["body"], ensure_ascii=False)
                            for i in range(2):
                                self.assertIn(f"AFTER_{i}" if mode == "references" else f"BEFORE_{i}", body)
                                self.assertNotIn(f"BEFORE_{i}" if mode == "references" else f"AFTER_{i}", body)
                            self.assertNotIn("DO_NOT_SEND_UNSELECTED", body)
                            self.assertLessEqual(captured[0]["bytes"], self.config.max_context_bytes)
                        app.close()

    async def test_toggle_clear_query_spaces_empty_results_and_escape_preserve_intent(self):
        for name in ("a.txt", "b.txt", "other.txt"):
            (self.root / name).write_text("body", encoding="utf-8")
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(100, 32)) as pilot:
            draft = app.query_one("#task", TaskInput)
            draft.load_text("UNSENT_DRAFT")
            app.open_picker()
            picker = await self.ready_picker(pilot)
            await self.choose(pilot, "a.txt")
            await pilot.press("ctrl+space")
            self.assertEqual(picker.chosen, {})
            await self.click(pilot, "#picker-toggle")
            self.assertIn("a.txt", picker.chosen)
            query = picker.query_one("#picker-query", Input)
            query.value = ""
            query.focus()
            await pilot.press("a", "space", "b")
            await pilot.pause()
            self.assertEqual(query.value, "a b")
            self.assertEqual(tuple(picker.chosen), ("a.txt",))
            await pilot.press("ctrl+r")
            self.assertEqual(picker.chosen, {})
            await self.choose(pilot, "b.txt")
            await pilot.press("escape")
            self.assertIsNone(app.messages)
            self.assertEqual(app.agent.reference_state()["files"], [])
            self.assertEqual(draft.text, "UNSENT_DRAFT")
            app.open_picker()
            await self.ready_picker(pilot)
            await self.choose(pilot, "a.txt")
            picker = await self.choose(pilot, "b.txt")
            query = picker.query_one("#picker-query", Input)
            query.value = "no matches"
            await self.until(pilot, lambda: picker.values == [])
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
            self.assertEqual(app.agent.reference_state()["files"], ["a.txt", "b.txt"])
            self.assertEqual(provider.calls, [])

    async def test_selection_limits_count_bytes_and_complete_mode_remains_single(self):
        for i in range(17):
            (self.root / f"file-{i:02}.txt").write_text("body", encoding="utf-8")
        app = self.app(fixture.ScriptedProvider([]))
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_picker()
            picker = await self.ready_picker(pilot)
            for i in range(17):
                picker.query_one("#picker-options", OptionList).highlighted = i
                picker.action_toggle_selection()
            self.assertEqual(len(picker.chosen), 16)
            self.assertNotIn(picker.values[16], picker.chosen)
            await pilot.press("escape")
            values = [letter * 1200 + ".txt" for letter in "中另第"]
            with patch("sprig_agent.picker.file_candidates", return_value={"files": values, "entries_scanned": 3, "limited": False}):
                app.open_picker()
                picker = await self.ready_picker(pilot)
                for i in range(3):
                    picker.query_one("#picker-options", OptionList).highlighted = i
                    picker.action_toggle_selection()
                self.assertEqual(tuple(picker.chosen), tuple(values[:2]))
                await pilot.press("escape")
            draft = app.query_one("#task", TaskInput)
            draft.load_text("/ref file-00")
            draft.focus()
            await pilot.press("tab")
            picker = await self.ready_picker(pilot)
            self.assertFalse(picker.multiple)
            await pilot.press("ctrl+space", "enter")
            await self.until(pilot, lambda: not isinstance(app.screen, PickerScreen))
            self.assertEqual(draft.text, "/ref file-00.txt")
            self.assertIsNone(app.messages)
            with patch("sprig_agent.picker.file_candidates", side_effect=lambda *args: file_candidates(*args, seconds=0)):
                app.open_picker()
                await self.until(pilot, lambda: isinstance(app.screen, PickerScreen)
                                 and "上限" in str(app.screen.query_one("#picker-status", Static).render()))
                picker = app.screen
                self.assertEqual(picker.values, [])
                await pilot.press("ctrl+space", "enter")
                self.assertIs(app.screen, picker)
                self.assertEqual(picker.chosen, {})
                self.assertIsNone(app.messages)
                await pilot.press("escape")

    async def test_batch_rechecks_privacy_and_binary_failure_without_partial_state(self):
        for mode in ("references", "attach"):
            for failure in ("privacy", "binary"):
                with self.subTest(mode=mode, failure=failure):
                    (self.root / ".sprigignore").write_text("", encoding="utf-8")
                    for name in ("keep.txt", "a.txt", "b.txt"):
                        (self.root / name).write_text("PRIVATE_BATCH_BODY_" + name, encoding="utf-8")
                    provider = fixture.ScriptedProvider([])
                    app = self.app(provider, save_session=mode + failure)
                    async with app.run_test(size=(100, 32)) as pilot:
                        app.start(local=("references", {"paths": ["keep.txt"], "operation": "add"}))
                        await self.until(pilot, lambda: not app.busy)
                        before, references = copy.deepcopy(app.messages), app.agent.reference_state()
                        draft = app.query_one("#task", TaskInput)
                        draft.load_text("PRESERVED_DRAFT")
                        app.open_picker()
                        await self.ready_picker(pilot)
                        await self.choose(pilot, "a.txt")
                        await self.choose(pilot, "b.txt")
                        if failure == "privacy":
                            (self.root / ".sprigignore").write_text("b.txt\n", encoding="utf-8")
                        else:
                            (self.root / "b.txt").write_bytes(b"zero\0bytes")
                        await self.click(pilot, "#picker-add" if mode == "attach" else "#picker-ref")
                        await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
                        self.assertNotEqual(app.last_operation_status, 0)
                        self.assertEqual(app.messages, before)
                        self.assertEqual(app.agent.reference_state(), references)
                        self.assertEqual(app.store.load(mode + failure), before)
                        self.assertEqual(draft.text, "PRESERVED_DRAFT")
                        self.assertEqual(provider.calls, [])
                        self.assertNotIn("PRIVATE_BATCH_BODY_a.txt", app.transcript)
                    app.close()

    async def test_combined_context_budget_rejects_batch_and_keeps_draft(self):
        probe = self.app(fixture.ScriptedProvider([]))
        cap = probe.agent.context_preview(None)["after_bytes"] + 4500
        probe.close()
        self.config = replace(self.config, max_context_bytes=cap)
        for name in ("a.txt", "b.txt"):
            (self.root / name).write_text("x" * 4000, encoding="utf-8")
        for mode in ("references", "attach"):
            with self.subTest(mode=mode):
                provider = fixture.ScriptedProvider([])
                app = self.app(provider)
                async with app.run_test(size=(100, 32)) as pilot:
                    draft = app.query_one("#task", TaskInput)
                    draft.load_text("BUDGET_DRAFT")
                    app.open_picker()
                    await self.ready_picker(pilot)
                    await self.choose(pilot, "a.txt")
                    await self.choose(pilot, "b.txt")
                    await self.click(pilot, "#picker-add" if mode == "attach" else "#picker-ref")
                    await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
                    self.assertIsNone(app.messages)
                    self.assertEqual(app.agent.reference_state()["files"], [])
                    self.assertEqual(draft.text, "BUDGET_DRAFT")
                    self.assertEqual(provider.calls, [])
                    self.assertIn("budget", app.transcript)
                app.close()

    async def test_metadata_cancel_and_mid_batch_cancellation_do_not_commit(self):
        for name in ("a.txt", "b.txt"):
            (self.root / name).write_text("BATCH_BODY_" + name, encoding="utf-8")
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(100, 32)) as pilot:
            reads, original = [], Directory.read
            def observe(directory, name, *args, **kwargs):
                reads.append(name)
                return original(directory, name, *args, **kwargs)
            with patch.object(Directory, "read", observe):
                app.open_picker()
                await self.ready_picker(pilot)
                await self.choose(pilot, "a.txt")
                await self.choose(pilot, "b.txt")
                await pilot.press("escape")
            self.assertTrue(all(name == ".sprigignore" for name in reads))
            self.assertIsNone(app.messages)
            draft = app.query_one("#task", TaskInput)
            draft.load_text("CANCEL_DRAFT")
            app.open_picker()
            await self.ready_picker(pilot)
            await self.choose(pilot, "a.txt")
            await self.choose(pilot, "b.txt")
            entered, release = threading.Event(), threading.Event()
            execute = app.tools.execute
            def block(name, arguments):
                result = execute(name, arguments)
                if name == "read_file" and arguments.get("path") == "a.txt":
                    entered.set()
                    release.wait(10)
                return result
            try:
                with patch.object(app.tools, "execute", side_effect=block):
                    await self.click(pilot, "#picker-add")
                    await self.until(pilot, entered.is_set)
                    app.action_cancel()
                    release.set()
                    await self.until(pilot, lambda: not app.busy)
                self.assertEqual(app.last_operation_status, 130)
                self.assertIsNone(app.messages)
                self.assertEqual(draft.text, "CANCEL_DRAFT")
                self.assertEqual(provider.calls, [])
            finally:
                release.set()

    async def test_committed_batch_restores_privately_without_reading_or_replaying(self):
        names = (" leading.txt", "说明 file.txt")
        for name in names:
            (self.root / name).write_text("BODY_NOT_IN_REFERENCE_HISTORY", encoding="utf-8")
        provider = fixture.ScriptedProvider([])
        app = self.app(provider, save_session="batch")
        async with app.run_test(size=(80, 24)) as pilot:
            app.open_picker()
            await self.ready_picker(pilot)
            for name in names:
                await self.choose(pilot, name)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
            history = copy.deepcopy(app.messages)
            self.assertEqual(app.agent.reference_state()["files"], list(names))
            self.assertEqual(app.store.load("batch"), history)
            self.assertNotIn("BODY_NOT_IN_REFERENCE_HISTORY", json.dumps(history))
            self.assertEqual(provider.calls, [])
        app.close()
        restored_provider = fixture.ScriptedProvider([])
        original = Directory.read
        def read(directory, name, *args, **kwargs):
            if name in names:
                raise AssertionError("restore must not read selected workspace bodies")
            return original(directory, name, *args, **kwargs)
        with patch.object(Directory, "read", read):
            restored = self.app(restored_provider, resume="batch")
            async with restored.run_test(size=(80, 24)) as pilot:
                self.assertEqual(restored.messages, history)
                self.assertEqual(restored.agent.reference_state()["files"], list(names))
                self.assertEqual(restored_provider.calls, [])
                self.assertFalse(restored.busy)


if __name__ == "__main__":
    unittest.main()
