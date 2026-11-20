"""Keyboard-first requirement views use the existing local approval and audit paths."""

from dataclasses import replace
import sys
import unittest

from textual.events import Paste
from textual.widgets import Button, Input, OptionList, TextArea

from sprig_agent.change_review_ui import ChangeReviewScreen
from sprig_agent.requirement_board import RequirementBoard
from sprig_agent.tui import ApprovalScreen, TaskInput
from tests import test_tui as ui


class RequirementBoardTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = ui.TuiTests.setUp
    asyncSetUp = ui.TuiTests.asyncSetUp
    app = ui.TuiTests.app
    until = ui.TuiTests.until
    click = ui.TuiTests.click

    async def test_command_opens_an_empty_board_and_pages_tracked_names(self):
        provider = ui.ScriptedProvider([])
        app = self.app(provider)
        async with app.run_test(size=(80, 24)) as pilot:
            editor = app.query_one("#task", TaskInput)
            editor.load_text("/requirements board")
            editor.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, RequirementBoard))
            board = app.screen
            self.assertTrue(board.query_one("#board-revert", Button).disabled)
            self.assertEqual(board.rows, [])
            await pilot.press("escape")
            for index in range(17):
                app.messages, _ = app.agent.change_requirement(app.messages, f"set req-{index} content {index}")
            self.assertTrue(app.slash_command("/requirements board"))
            await self.until(pilot, lambda: isinstance(app.screen, RequirementBoard))
            board = app.screen
            await self.click(pilot, "#board-next")
            self.assertEqual(board.page_offset, 16)
            names = board.query_one("#board-requirements", OptionList)
            self.assertIs(board.focused, names)
            self.assertEqual(names.highlighted, 0)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and board.details is not None)
            self.assertIn("req-16 · v1", board.query_one("#board-details", TextArea).text)
            self.assertIn("未检查", board.query_one("#board-details", TextArea).text)
            self.assertEqual(provider.calls, [])

    async def test_board_review_verify_and_revert_keep_the_main_draft(self):
        path = self.root / "scope.txt"
        path.write_text("verification scope", encoding="utf-8")
        (self.root / "unsent.txt").write_text("unsent", encoding="utf-8")
        fixed = (sys.executable, "-I", "-B", "-c", "print('CHECK_ONLY')")
        self.config = replace(self.config, checks=(fixed,), check_files=("scope.txt",))
        provider = ui.ScriptedProvider([
            ui.call("cache", "edit_file", {"path": "cache.txt", "old": "", "new": "cache"}), ui.response(),
            ui.call("logs", "edit_file", {"path": "logs.txt", "old": "", "new": "logs"}), ui.response(),
        ])
        app = self.app(provider, save_session="requirements", change_dir=self.parent / "changes")
        async with app.run_test(size=(80, 24)) as pilot:
            agent = app.agent
            for name in ("cache", "logs"):
                app.messages, _ = agent.change_requirement(app.messages, f"set {name} retain {name}")
                app.start(f"write {name}", requirement=name)
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
                detail = app.screen.query_one("#approval-detail", TextArea)
                self.assertGreaterEqual(detail.content_region.height, 5)
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen) and "CHECK_ONLY" in app.screen.description)
                await self.click(pilot, "#approve")
                await self.until(pilot, lambda: not app.busy)
            self.assertEqual(len(provider.calls), 4)

            editor = app.query_one("#task", TaskInput)
            editor.load_text("Unsent ")
            editor.move_cursor(editor.document.end)
            editor.post_message(Paste("  pasted\nsecond\nthird  "))
            await self.until(pilot, lambda: bool(editor.pastes.spans))
            selection = editor.files.select(len(editor.text), len(editor.text), "unsent.txt")
            editor.replace(selection.text[len(editor.text):], editor.document.end, editor.document.end)
            editor.files = selection
            draft = editor.text, editor.paste_state(), editor.files.paths()

            await self.click(pilot, "#requirement-board")
            await self.until(pilot, lambda: isinstance(app.screen, RequirementBoard))
            board = app.screen
            options = board.query_one("#board-requirements", OptionList)
            self.assertIs(board.focused, options)
            self.assertEqual(options.highlighted, 0)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and board.details is not None)
            self.assertEqual([row["path"] for row in board.files], ["cache.txt"])
            files = board.query_one("#board-files", OptionList)
            await pilot.press("tab")
            self.assertIs(board.focused, files)
            await pilot.press("enter")
            await self.until(pilot, lambda: not app.busy and isinstance(app.screen, ChangeReviewScreen))
            review = app.screen
            self.assertEqual([row["path"] for row in review.rows], ["cache.txt"])
            self.assertIn("+cache", review.query_one("#review-text", TextArea).text)
            await pilot.press("escape")
            await self.until(pilot, lambda: app.screen is board)

            check = board.query_one("#board-checks", Input)
            check.value = "1"
            check.focus()
            await pilot.press("enter")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("CHECK_ONLY", app.screen.description)
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and app.screen is board)
            self.assertEqual(app.task_status, 2)
            await self.click(pilot, "#board-refresh")
            await self.until(pilot, lambda: not app.busy and board.details is not None)
            self.assertIn("所选检查通过 · 当前有效", board.query_one("#board-details", TextArea).text)

            files.highlighted = 0
            await pilot.pause()
            await self.click(pilot, "#board-revert")
            await self.until(pilot, lambda: isinstance(app.screen, ApprovalScreen))
            self.assertIn("REVERT REQUIREMENT cache revision 1", app.screen.description)
            self.assertGreaterEqual(app.screen.query_one("#approval-detail", TextArea).content_region.height, 5)
            self.assertTrue((self.root / "cache.txt").exists())
            await self.click(pilot, "#approve")
            await self.until(pilot, lambda: not app.busy and app.screen is board)
            self.assertFalse((self.root / "cache.txt").exists())
            self.assertEqual((self.root / "logs.txt").read_text(encoding="utf-8"), "logs")
            await pilot.press("escape")
            await self.until(pilot, lambda: not isinstance(app.screen, RequirementBoard))
            self.assertEqual((editor.text, editor.paste_state(), editor.files.paths()), draft)
            self.assertEqual(len(provider.calls), 4)
