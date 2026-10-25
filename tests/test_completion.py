"""Local discovery and real TUI selection preserve privacy, intent and Enter."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textual.widgets import Input, Static, TextArea
from sprig_agent.completion import file_candidates
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent.picker import PickerScreen
from tests import test_tui as fixture


class FileCandidateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = Config(self.root, "fixture")
        self.directory = Directory(self.root)
        self.addCleanup(self.directory.close)
        self.cancelled = threading.Event()

    def find(self, query="", **kwargs):
        return file_candidates(self.config, self.directory, query, self.cancelled, seconds=5, **kwargs)

    def test_metadata_only_discovery_reuses_privacy_and_skips_dependency_trees(self):
        for name in (".git", ".venv", ".venv-wsl", "node_modules", "private", "src"):
            (self.root / name).mkdir()
            (self.root / name / "value.txt").write_text("never read", encoding="utf-8")
        for name in (".env", "config.toml", "credential-fixture-value.txt", "说明 file.txt"):
            (self.root / name).write_text("never read", encoding="utf-8")
        (self.root / ".sprigignore").write_text("private/\n.sprigignore\n", encoding="utf-8")
        self.config = replace(self.config, config_path=self.root / "config.toml", api_key="credential-fixture-value")
        reads = []
        original = Directory.read
        def read(directory, name, *args, **kwargs):
            reads.append(name)
            return original(directory, name, *args, **kwargs)
        with patch.object(Directory, "read", read):
            result = self.find()
        self.assertEqual(set(result["files"]), {"src/value.txt", "说明 file.txt"})
        self.assertEqual(reads, [".sprigignore"])
        self.assertNotIn("credential-fixture-value", json.dumps(result))

    def test_fuzzy_names_ranking_and_result_and_scan_limits(self):
        (self.root / "src").mkdir()
        (self.root / "src/value_test.py").write_text("", encoding="utf-8")
        (self.root / "value.py").write_text("", encoding="utf-8")
        for i in range(30):
            (self.root / f"prefix-value-{i}.py").write_text("", encoding="utf-8")
        result = self.find("VALUE")
        self.assertEqual(result["files"][0], "value.py")
        self.assertEqual(len(result["files"]), 20)
        self.assertTrue(result["limited"])
        self.assertIn("src/value_test.py", self.find("vlt")["files"])
        self.assertIn("src/value_test.py", self.find("src\\vl")["files"])
        result = self.find(max_entries=3)
        self.assertLessEqual(result["entries_scanned"], 3)
        self.assertTrue(result["limited"])
        self.assertEqual(self.find("../../outside")["files"], [])

    def test_cancel_disabled_invalid_and_closed_do_not_produce_candidates(self):
        (self.root / "value.txt").write_text("body", encoding="utf-8")
        self.cancelled.set()
        with patch.object(Directory, "scandir", side_effect=AssertionError("must not scan")):
            self.assertTrue(self.find()["cancelled"])
        self.cancelled.clear()
        for name in ("list_files", "read_file"):
            self.config = replace(self.config, disabled_tools=(name,))
            self.assertIn("error", self.find())
        self.config = replace(self.config, disabled_tools=())
        self.assertIn("error", self.find("x" * 257))
        self.assertIn("error", self.find("x\n"))
        limited = file_candidates(self.config, self.directory, "", self.cancelled, seconds=0)
        self.assertEqual(limited["entries_scanned"], 0)
        self.assertTrue(limited["limited"])
        self.directory.close()
        with self.assertRaises(OSError):
            self.find()

    def test_symlink_directory_and_file_do_not_reveal_external_names(self):
        outside = self.root / "outside"
        workspace = self.root / "workspace"
        outside.mkdir()
        workspace.mkdir()
        (outside / "external-secret-name.txt").write_text("body", encoding="utf-8")
        try:
            (workspace / "alias").symlink_to(outside, target_is_directory=True)
            (workspace / "linked.txt").symlink_to(outside / "external-secret-name.txt")
        except OSError:
            self.skipTest("native symlink creation unavailable")
        with Directory(workspace) as directory:
            result = file_candidates(replace(self.config, workdir=workspace), directory, "", self.cancelled)
        self.assertEqual(result["files"], [])


class CompletionTuiTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.TuiTests.asyncSetUp

    def setUp(self):
        fixture.TuiTests.setUp(self)
        # Selection/attachment guards have a separate discovery budget. The
        # production 250 ms cooperative bound is covered by FileCandidateTests.
        self.enterContext(patch('sprig_agent.picker.file_candidates', side_effect=lambda config, directory, query, cancelled:
            file_candidates(config, directory, query, cancelled, seconds=5)))

    app = fixture.TuiTests.app
    until = fixture.TuiTests.until
    click = fixture.TuiTests.click
    send_text = fixture.TuiTests.send_text

    async def ready_picker(self, pilot):
        await self.until(pilot, lambda: isinstance(pilot.app.screen, PickerScreen)
                         and bool(pilot.app.screen.values))
        return pilot.app.screen

    async def test_tab_command_completion_never_executes_and_enter_stays_submit(self):
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(100, 32)) as pilot:
            editor = app.query_one("#task", TextArea)
            editor.load_text("/sta")
            await pilot.press("tab")
            picker = await self.ready_picker(pilot)
            self.assertEqual(picker.values, ["/status"])
            self.assertIsNone(app.messages)
            await pilot.press("enter")
            await self.until(pilot, lambda: not isinstance(app.screen, PickerScreen))
            self.assertEqual(editor.text, "/status ")
            self.assertEqual(provider.calls, [])
            await pilot.press("enter")
            self.assertIn("本地状态", app.transcript)
            self.assertEqual(provider.calls, [])
            editor.load_text("/con")
            await pilot.press("tab")
            await self.ready_picker(pilot)
            await pilot.press("escape")
            self.assertEqual(editor.text, "/con")

    async def test_ref_argument_completion_preserves_unicode_and_spaces_then_refreshes(self):
        path = self.root / "说明 file.txt"
        path.write_text("snapshot one", encoding="utf-8")
        provider = fixture.ScriptedProvider([fixture.response()])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TextArea)
            editor.load_text("/ref 说明")
            await pilot.press("tab")
            picker = await self.ready_picker(pilot)
            self.assertEqual(picker.values, ["说明 file.txt"])
            self.assertIsNone(app.messages)
            await pilot.press("enter")
            await self.until(pilot, lambda: not isinstance(app.screen, PickerScreen))
            self.assertEqual(editor.text, "/ref 说明 file.txt")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(app.agent.reference_state()["files"]))
            self.assertEqual(provider.calls, [])
            self.assertNotIn("snapshot one", json.dumps(app.messages))
            path.write_text("fresh two", encoding="utf-8")
            await self.send_text(pilot, "Explain")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertIn("fresh two", json.dumps(provider.calls[0]))
            self.assertNotIn("snapshot one", json.dumps(provider.calls[0]))

    async def test_picker_snapshot_preserves_task_draft_and_sends_only_on_enter(self):
        path = self.root / "[literal] file.txt"
        path.write_text("selected snapshot", encoding="utf-8")
        (self.root / ".env").write_text("private", encoding="utf-8")
        provider = fixture.ScriptedProvider([fixture.response()])
        self.config = replace(self.config, read_only=True)
        app = self.app(provider)
        async with app.run_test(size=(80, 30)) as pilot:
            editor = app.query_one("#task", TextArea)
            editor.load_text("draft explanation")
            await self.click(pilot, "#files")
            picker = await self.ready_picker(pilot)
            self.assertEqual(picker.values, [path.name])
            self.assertIsNone(app.messages)
            await self.click(pilot, "#picker-add")
            await self.until(pilot, lambda: not app.busy and app.messages is not None)
            self.assertEqual(editor.text, "draft explanation")
            self.assertEqual(provider.calls, [])
            path.write_text("later disk version", encoding="utf-8")
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and bool(provider.calls))
            self.assertIn("selected snapshot", json.dumps(provider.calls[0]))
            self.assertNotIn("later disk version", json.dumps(provider.calls[0]))

    async def test_stale_scan_results_and_closed_picker_cannot_select_files(self):
        entered, release = threading.Event(), threading.Event()
        def scan(config, root, query, cancelled):
            if query == "first":
                entered.set()
                release.wait(10)
            return {"files": [query + ".txt"], "entries_scanned": 1, "limited": False}
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        try:
            with patch("sprig_agent.picker.file_candidates", side_effect=scan):
                async with app.run_test(size=(110, 36)) as pilot:
                    app.open_picker(query="first")
                    await self.until(pilot, entered.is_set)
                    picker = app.screen
                    picker.query_one("#picker-query", Input).value = "second"
                    await pilot.pause()
                    self.assertEqual(picker.values, [])
                    await pilot.press("enter")
                    self.assertIs(app.screen, picker)
                    release.set()
                    await self.until(pilot, lambda: picker.values == ["second.txt"])
                    await pilot.press("escape")
                    await pilot.pause()
                    self.assertIsNone(app.messages)
                    self.assertEqual(provider.calls, [])
                    self.assertEqual(app.agent.reference_state()["files"], [])
        finally:
            release.set()

    async def test_disabled_read_and_binary_selection_do_not_send_or_lose_draft(self):
        (self.root / "binary.bin").write_bytes(b"private\0binary")
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TextArea)
            editor.load_text("keep draft")
            await self.click(pilot, "#files")
            await self.ready_picker(pilot)
            await self.click(pilot, "#picker-add")
            await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
            self.assertEqual(editor.text, "keep draft")
            self.assertIsNone(app.messages)
            self.assertEqual(provider.calls, [])
        self.config = replace(self.config, disabled_tools=("read_file",))
        app = self.app(fixture.ScriptedProvider([]))
        async with app.run_test(size=(100, 32)) as pilot:
            await self.send_text(pilot, "/files")
            await self.until(pilot, lambda: isinstance(app.screen, PickerScreen)
                             and "read_file" in str(app.screen.query_one("#picker-status", Static).render()))
            self.assertEqual(app.screen.values, [])
            await pilot.press("escape")
            self.assertIsNone(app.messages)

    async def test_zero_discovery_budget_shows_limit_without_selecting_or_submitting(self):
        (self.root / 'one.txt').write_text('body', encoding='utf-8')
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        with patch('sprig_agent.picker.file_candidates', side_effect=lambda config, directory, query, cancelled:
                   file_candidates(config, directory, query, cancelled, seconds=0)):
            async with app.run_test(size=(110, 36)) as pilot:
                editor = app.query_one('#task', TextArea)
                editor.load_text('keep draft')
                await self.click(pilot, '#files')
                await self.until(pilot, lambda: isinstance(app.screen, PickerScreen)
                    and '已达上限' in str(app.screen.query_one('#picker-status', Static).render()))
                self.assertEqual(app.screen.values, [])
                await pilot.press('enter')
                await pilot.pause()
                self.assertIsInstance(app.screen, PickerScreen)
                await pilot.press('escape')
                await self.until(pilot, lambda: not isinstance(app.screen, PickerScreen))
                self.assertEqual(editor.text, 'keep draft')
                self.assertIsNone(app.messages)
                self.assertEqual(provider.calls, [])

    async def test_privacy_changes_after_discovery_are_rechecked_before_attachment(self):
        (self.root / "value.txt").write_text("private after selection", encoding="utf-8")
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(100, 32)) as pilot:
            await self.click(pilot, "#files")
            await self.ready_picker(pilot)
            (self.root / ".sprigignore").write_text("value.txt\n", encoding="utf-8")
            await self.click(pilot, "#picker-add")
            await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
            self.assertIsNone(app.messages)
            self.assertEqual(provider.calls, [])
            self.assertNotIn("private after selection", app.transcript)

    async def test_outer_space_path_requires_direct_selection_without_changing_its_identity(self):
        (self.root / " leading.txt").write_text("right file", encoding="utf-8")
        (self.root / "leading.txt").write_text("wrong file", encoding="utf-8")
        provider = fixture.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(110, 36)) as pilot:
            editor = app.query_one("#task", TextArea)
            editor.load_text("/ref leading")
            await pilot.press("tab")
            picker = await self.ready_picker(pilot)
            picker.query_one("#picker-options").highlighted = picker.values.index(" leading.txt")
            await pilot.press("enter")
            await self.until(pilot, lambda: not isinstance(app.screen, PickerScreen))
            self.assertEqual(editor.text, "/ref leading")
            self.assertIsNone(app.messages)
            await self.click(pilot, "#files")
            picker = await self.ready_picker(pilot)
            picker.query_one("#picker-options").highlighted = picker.values.index(" leading.txt")
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
            self.assertEqual(app.agent.reference_state()["files"], [" leading.txt"])
            self.assertEqual(provider.calls, [])

    async def test_busy_picker_does_not_scan_or_change_guidance(self):
        entered, release = threading.Event(), threading.Event()
        class Provider:
            def complete(self, messages, tools, on_text=None):
                entered.set()
                release.wait(10)
                return fixture.response()
        app = self.app(Provider())
        try:
            async with app.run_test(size=(100, 32)) as pilot:
                await self.send_text(pilot, "Start")
                await self.until(pilot, entered.is_set)
                with patch("sprig_agent.picker.file_candidates") as scan:
                    app.open_picker()
                    await pilot.pause()
                    self.assertNotIsInstance(app.screen, PickerScreen)
                    scan.assert_not_called()
                    self.assertEqual(app.pending_tasks, [])
                release.set()
                await self.until(pilot, lambda: not app.busy)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
