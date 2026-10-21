"""Overlapping reference selections remain bounded, fresh and resumable."""
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.references import reference_paths
from sprig_agent.picker import PickerScreen
from textual.widgets import Input
from tests import test_references as refs
from tests import test_tui as tui
from tests.test_generation_options import envelope
from tests.test_native_protocols import backend, native


def populate(root, count=16):
    paths = tuple(f"item-{i:02}.txt" for i in range(count))
    for path in paths:
        (root / path).write_text("REFERENCE_BODY_" + path, encoding="utf-8")
    return paths


class ReferenceMergeTests(unittest.TestCase):
    setUp = refs.FileReferenceTests.setUp

    def test_full_capacity_overlap_refreshes_and_keeps_first_path_spelling(self):
        paths = populate(self.root)
        history, _ = self.agent.update_references(paths)
        (self.root / paths[0]).write_text("UPDATED_REFERENCE_BODY", encoding="utf-8")
        old_hash = self.agent.reference_state()["observed"][0]["sha256"]
        incoming = tuple(path.upper() for path in paths) if os.name == "nt" else paths
        updated, result = self.agent.update_references(incoming, messages=history)
        self.assertEqual(result["file_references"], list(paths))
        self.assertNotEqual(result["observed"][0]["sha256"], old_hash)
        self.assertEqual(updated, history)
        self.assertNotIn("UPDATED_REFERENCE_BODY", json.dumps(updated))
        self.assertEqual(len(self.saved), 2)
        self.assertEqual(self.prompts, [])
        self.provider.complete.assert_not_called()

    def test_overlapping_union_accepts_16_and_rejects_new_17th_without_state_change(self):
        paths = populate(self.root, 17)
        history, _ = self.agent.update_references(paths[:9])
        history, result = self.agent.update_references([*paths[7:9], *paths[9:16]], messages=history)
        self.assertEqual(result["file_references"], list(paths[:16]))
        state, saved = self.agent.reference_state(), copy.deepcopy(self.saved)
        with self.assertRaisesRegex(ValueError, "16"):
            self.agent.update_references([paths[0], paths[16]], messages=history)
        self.assertEqual(self.agent.reference_state(), state)
        self.assertEqual(self.saved, saved)
        with self.assertRaisesRegex(ValueError, "16"):
            self.agent.update_references([paths[0]] * 17, messages=history)
        self.assertEqual(self.agent.reference_state(), state)
        self.provider.complete.assert_not_called()

    def test_duplicate_union_obeys_utf8_path_budget_and_raw_input_limits(self):
        from sprig_agent.references import merge_reference_paths
        paths = tuple(letter * 1200 + ".txt" for letter in "中另第")
        self.assertEqual(merge_reference_paths(paths[:2], paths[:2]), paths[:2])
        with self.assertRaisesRegex(ValueError, "8192"):
            merge_reference_paths(paths[:2], paths[2:])
        with self.assertRaisesRegex(ValueError, "16"):
            merge_reference_paths(["a"] * 17, [])
        with self.assertRaisesRegex(ValueError, "16"):
            reference_paths(["a"] * 17)
        with self.assertRaises(ValueError):
            merge_reference_paths(["a"], ["../outside"])

    def test_unchanged_union_still_rechecks_privacy_and_cancellation(self):
        paths = populate(self.root)
        history, _ = self.agent.update_references(paths)
        state, saved = self.agent.reference_state(), copy.deepcopy(self.saved)
        (self.root / ".sprigignore").write_text(paths[-1] + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.agent.update_references([paths[0]], messages=history)
        self.assertEqual(self.agent.reference_state(), state)
        self.assertEqual(self.saved, saved)
        (self.root / ".sprigignore").write_text("", encoding="utf-8")
        original = self.tools.execute
        def cancel(name, arguments):
            result = original(name, arguments)
            if name == "read_file":
                raise KeyboardInterrupt()
            return result
        with patch.object(self.tools, "execute", side_effect=cancel), self.assertRaises(KeyboardInterrupt):
            self.agent.update_references([paths[0]], messages=history)
        self.assertEqual(self.agent.reference_state(), state)
        self.assertEqual(self.saved, saved)
        self.provider.complete.assert_not_called()


class ReferenceMergeCliTests(unittest.TestCase):
    setUp = refs.ReferenceConfigurationTests.setUp
    write = refs.ReferenceConfigurationTests.write
    invoke = refs.ReferenceConfigurationTests.invoke

    def test_full_selection_resume_with_overlapping_flags_reaches_three_http_wires(self):
        paths = populate(self.root)
        for wire in ("chat_completions", "messages", "responses"):
            self.path = self.parent / (wire + ".toml")
            captured = []
            reply = envelope(tui.response()) if wire == "chat_completions" else native(wire, "Done")
            with self.subTest(wire=wire), backend(wire, [reply, reply], captured) as url:
                self.write('provider="x"\n[providers.x]\nmodel="fixture"\nproxy="direct"\nwire_api=' + json.dumps(wire)
                           + '\nbase_url=' + json.dumps(url) + '\n')
                common = ["--config", str(self.path), "--cli", "--read-only", "--session-dir", str(self.parent / ("sessions-" + wire))]
                flags = [item for path in paths for item in ("--ref-file", path)]
                status, _, error = self.invoke([*common, "--save-session", "full", *flags, "Inspect"])
                self.assertEqual(status, 0, error)
                (self.root / paths[0]).write_text("FRESH_RESUME_MARKER_" + wire, encoding="utf-8")
                status, _, error = self.invoke([*common, "--resume", "full", *flags, "Continue"])
                self.assertEqual(status, 0, error)
                self.assertEqual(len(captured), 2)
                def strings(value):
                    if isinstance(value, str):
                        yield value
                    elif isinstance(value, list):
                        for child in value:
                            yield from strings(child)
                    elif isinstance(value, dict):
                        for child in value.values():
                            yield from strings(child)
                context = next(text for text in strings(captured[1]["body"]) if text.startswith("Application-prepared context"))
                frames = json.loads(context.split("\n", 1)[1])
                self.assertEqual([frame["path"] for frame in frames], list(paths))
                self.assertIn("FRESH_RESUME_MARKER_" + wire, frames[0]["content"])


class ReferenceMergeTuiTests(unittest.IsolatedAsyncioTestCase):
    setUp = tui.TuiTests.setUp
    asyncSetUp = tui.TuiTests.asyncSetUp
    app = tui.TuiTests.app
    until = tui.TuiTests.until

    async def test_picker_reselect_and_cli_marked_resume_accept_existing_full_set(self):
        from sprig_agent.completion import file_candidates
        paths = populate(self.root)
        self.config = replace(self.config, reference_files=paths, read_only=True, setting_sources=(("reference_files", "CLI"),))
        provider = tui.ScriptedProvider([])
        app = self.app(provider, save_session="full")
        with patch("sprig_agent.picker.file_candidates", side_effect=lambda *args: file_candidates(*args, seconds=5)):
            async with app.run_test(size=(100, 32)) as pilot:
                app.open_picker(query=paths[0])
                await self.until(pilot, lambda: isinstance(app.screen, PickerScreen) and bool(app.screen.values))
                app.screen.query_one("#picker-query", Input).focus()
                await pilot.press("enter")
                await self.until(pilot, lambda: not app.busy and not isinstance(app.screen, PickerScreen))
                self.assertEqual(app.last_operation_status, 0)
                self.assertEqual(app.agent.reference_state()["files"], list(paths))
                self.assertIsNotNone(app.messages)
                self.assertEqual(provider.calls, [])
        app.close()
        restored_provider = tui.ScriptedProvider([])
        restored = self.app(restored_provider, resume="full")
        async with restored.run_test(size=(80, 24)) as pilot:
            self.assertEqual(restored.agent.reference_state()["files"], list(paths))
            self.assertFalse(restored.busy)
            self.assertEqual(restored_provider.calls, [])


if __name__ == "__main__":
    unittest.main()
