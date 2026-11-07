"""Regex-first model search, legacy history and shared hard policy boundaries."""
from contextlib import closing
import copy
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.provider import Provider
from sprig_agent.search import RegexBudgetExceeded, RegexMatcher
from sprig_agent.session import validate_history
from sprig_agent.settings import load_settings
from sprig_agent.tools import TOOL_SCHEMAS, ToolError, Tools
from tests import test_context_folding as folding, test_settings as settings_fixtures
from tests.test_mcp_stdio import backend, objects, wire
from tests.test_named_reads import call


class GrepTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.approve = Mock(side_effect=AssertionError("inspection requested approval"))
        self.config = Config(self.root, "fixture", read_only=True)
        self.tools = Tools(self.config, self.approve, self.cancel)
        self.addCleanup(self.tools.close)
        (self.root / "source.py").write_bytes(b"alpha\r\nbeta\ralpha|beta\nother\n")

    def test_default_regex_explicit_literal_and_unchanged_legacy_search(self):
        with patch.object(self.tools, "_read", wraps=self.tools._read) as read:
            result = self.tools.grep("alpha|beta", "source.py")
            self.assertEqual(read.call_count, 1)
        self.assertEqual([row["line"] for row in result["matches"]], [1, 2, 3])
        self.assertNotIn("search_guidance", result)
        literal = self.tools.grep("alpha|beta", "source.py", literal=True)
        legacy = self.tools.search("alpha|beta", "source.py")
        self.assertEqual([row["line"] for row in literal["matches"]], [3])
        self.assertEqual(literal, legacy)
        self.assertEqual(result, self.tools.search("alpha|beta", "source.py", regex=True))
        self.approve.assert_not_called()

    def test_modes_and_filters_share_existing_scanner_without_retry(self):
        (self.root / "ignored.txt").write_text("ALPHA", encoding="utf-8")
        for mode in ("content", "files", "count"):
            with self.subTest(mode=mode), patch.object(self.tools, "_read", wraps=self.tools._read) as read:
                result = self.tools.grep("ALPHA|BETA", glob="*.py", ignore_case=True, output_mode=mode)
                self.assertEqual(read.call_count, 1)
            self.assertEqual(result, self.tools.search("ALPHA|BETA", glob="*.py", ignore_case=True,
                                                      output_mode=mode, regex=True))
        with patch.object(self.tools, "_read", wraps=self.tools._read) as read:
            missed = self.tools.grep("absent|missing", "source.py", literal=True)
            self.assertEqual(read.call_count, 1)
        self.assertEqual(missed["matches"], [])
        self.assertNotIn("search_guidance", missed)

    def test_read_mode_merges_complete_physical_windows_and_issues_one_version(self):
        path = self.root / "source.py"
        original = "\ufeffpre\r\nalpha🙂\rbeta\nnext\nunused\r\nunused\nalpha\nlast"
        path.write_bytes(original.encode())
        with patch.object(self.tools, "_read_validated", wraps=self.tools._read_validated) as read:
            result = self.tools.execute("grep", {
                "pattern": "alpha|beta", "path": path.name, "context": 1, "output_mode": "read",
            })
            self.assertEqual(read.call_count, 1)
        self.assertEqual(result["ranges"], [[1, 4], [6, 8]])
        self.assertEqual(result["matched_lines"], 3)
        self.assertFalse(result["truncated"])
        self.assertTrue(result["range_complete"])
        self.assertEqual(result["content"], "1: \ufeffpre\r\n2: alpha🙂\r3: beta\n4: next\n6: unused\n7: alpha\n8: last")
        explicit = self.tools.read_file(path.name, ranges=result["ranges"])
        self.assertEqual((result["read_id"], result["sha256"]), (explicit["read_id"], explicit["sha256"]))
        self.approve.assert_not_called()

    def test_read_mode_reports_match_and_window_limits_without_partial_lines(self):
        path = self.root / "source.py"
        path.write_bytes(b"alpha\nunused\n" * 9)
        result = self.tools.grep("alpha", path.name, output_mode="read")
        self.assertEqual(result["ranges"], [[line, line] for line in range(1, 16, 2)])
        self.assertEqual(result["matched_lines"], 8)
        self.assertTrue(result["truncated"] and result["range_complete"])
        result = self.tools.grep("alpha", path.name, output_mode="read", limit=1)
        self.assertEqual(result["ranges"], [[1, 1]])
        self.assertEqual(result["content"], "1: alpha\n")
        self.assertTrue(result["truncated"] and result["range_complete"])
        path.write_bytes(b"alpha\n")
        exact = self.tools.grep("alpha", path.name, output_mode="read", limit=1)
        self.assertFalse(exact["truncated"])
        missed = self.tools.grep("absent", path.name, output_mode="read")
        self.assertEqual((missed["ranges"], missed["matched_lines"]), ([], 0))
        self.assertNotIn("read_id", missed)
        path.write_bytes((b"0\n" * 10 + b"alpha\n" + b"0\n" * 10) * 477)
        with closing(Tools(replace(self.config, max_output=131072), self.approve)) as tools:
            capped = tools.grep("alpha", path.name, literal=True, context=10, limit=1000, output_mode="read")
        self.assertEqual(capped["ranges"], [[1, 9996]])
        self.assertEqual(capped["matched_lines"], 476)
        self.assertTrue(capped["truncated"] and capped["range_complete"])

    def test_read_mode_respects_scope_privacy_limits_and_the_read_tool_gate(self):
        args = {"pattern": "alpha", "path": "source.py", "output_mode": "read"}
        (self.root / "sub").mkdir()
        (self.root / ".env").write_bytes(b"alpha")
        for extra in ({"path": "."}, {"path": "sub"}, {"path": "../source.py"},
                      {"path": ".env"}, {"glob": "*.py"}):
            self.assertIn("error", self.tools.execute("grep", {**args, **extra}))
        with closing(Tools(replace(self.config, disabled_tools=("read_file",)), self.approve)) as tools:
            self.assertIn("disabled", tools.execute("grep", args)["error"])
            self.assertTrue(tools.grep("alpha", "source.py")["matches"])
        path = self.root / "source.py"
        path.write_bytes(b"alpha\n" + b"sk-" + b"z" * 30)
        self.assertIn("error", self.tools.execute("grep", args))
        secret = "fixture-private-marker"
        path.write_bytes(b"alpha\n" + secret.encode())
        with closing(Tools(replace(self.config, api_key=secret), self.approve)) as tools:
            result = tools.execute("grep", args)
            self.assertEqual(result["content"], "1: alpha\n")
            self.assertNotIn("read_id", result)
            self.assertNotIn("sha256", result)
        path.write_bytes(b"alpha" + b"x" * 800 + b"\n")
        with closing(Tools(replace(self.config, max_output=384), self.approve)) as tools:
            result = tools.execute("grep", args)
            self.assertIn("max-output", result["error"])
            self.assertNotIn("read_id", result)
            path.write_bytes(b"alpha\n")
            result = tools.execute("grep", args)
            self.assertTrue(result["read_id"].endswith("-1"))
            self.assertLessEqual(len(json.dumps(result).encode()), 384)
        self.approve.assert_not_called()

    def test_read_mode_does_not_publish_partial_source_on_regex_timeout(self):
        search = RegexMatcher._search
        def limited(matcher, line):
            if line == "beta":
                raise RegexBudgetExceeded
            return search(matcher, line)
        with patch.object(RegexMatcher, "_search", limited):
            result = self.tools.grep("alpha", "source.py", output_mode="read")
        self.assertTrue(result["timed_out"] and result["truncated"])
        self.assertNotIn("content", result)
        self.assertNotIn("read_id", result)
        self.approve.assert_not_called()

    def test_missing_scope_suggests_a_pinned_directory_without_widening(self):
        source = self.root / "src"
        source.mkdir()
        (source / "sample.py").write_text("alpha\n", encoding="utf-8")
        for name, arguments in (("grep", {"pattern": "alpha"}), ("search", {"query": "alpha"}),
                                ("project_map", {}), ("glob", {"pattern": "*.py"}), ("list_files", {})):
            for path, nearest in (("docs", "."), ("src/missing", "src"), ("src/missing/deeper", "src")):
                with self.subTest(tool=name, path=path), patch.object(self.tools, "_read") as read:
                    result = self.tools.execute(name, {**arguments, "path": path})
                    self.assertEqual(result, {"error": "Workspace path not found.", "suggested_directory": nearest,
                                              "guidance": "Use list_files at suggested_directory to choose a path."})
                    read.assert_not_called()
        recovered = self.tools.execute("grep", {"pattern": "alpha", "path": "src"})
        self.assertEqual([row["path"] for row in recovered["matches"]], ["src/sample.py"])
        self.approve.assert_not_called()

    def test_missing_scope_hint_obeys_privacy_tool_availability_and_budget(self):
        secret = "sk-" + "z" * 30
        configured = "fixture-private-value"
        long_name = "p" * 180
        for name in (secret, configured, long_name):
            (self.root / name).mkdir()
        (self.root / ".sprigignore").write_text("ignored/\n", encoding="utf-8")
        self.tools.config = replace(self.config, api_key=configured, max_output=256)
        for path in (".env/missing", "../missing", "ignored/missing", f"{secret}/missing",
                     f"{configured}/missing", f"{long_name}/missing"):
            with self.subTest(path=path):
                result = self.tools.execute("grep", {"pattern": "alpha", "path": path})
                self.assertEqual(set(result), {"error"})
                self.assertNotIn(secret, result["error"])
                self.assertNotIn(configured, result["error"])
        self.tools.config = replace(self.config, disabled_tools=("list_files",))
        self.assertEqual(self.tools.execute("grep", {"pattern": "alpha", "path": "docs"}),
                         {"error": "Workspace path not found.", "suggested_directory": "."})
        self.approve.assert_not_called()

    def test_invalid_flags_patterns_and_limits_fail_before_file_reads(self):
        invalid = [{"literal": value} for value in (None, 0, 1, "false", [], {})]
        invalid += [{"pattern": value} for value in ("", None, "x" * 1001, "(?=alpha)", r"(a)\1")]
        invalid += [{"context": 20}, {"output_mode": "count", "context": 1}]
        with patch.object(self.tools, "_read") as read:
            for arguments in invalid:
                with self.subTest(arguments=arguments):
                    self.assertIn("error", self.tools.execute("grep", {"pattern": "alpha", **arguments}))
            read.assert_not_called()
        self.approve.assert_not_called()

    def test_privacy_output_bounds_and_cancellation_still_apply(self):
        (self.root / ".env").write_text("alpha", encoding="utf-8")
        (self.root / "private.py").write_text("alpha\n" + "sk-" + "z" * 30, encoding="utf-8")
        result = self.tools.grep("alpha")
        self.assertEqual([row["path"] for row in result["matches"]], ["source.py", "source.py"])
        self.assertEqual(result["skipped_files"], 1)
        self.assertTrue(result["truncated"])
        for path in (".env", "private.py", "../outside"):
            self.assertIn("error", self.tools.execute("grep", {"pattern": "alpha", "path": path}))
        self.tools.config = replace(self.config, max_output=256)
        bounded = self.tools.grep(".", "source.py", context=2)
        self.assertLessEqual(len(json.dumps(bounded, ensure_ascii=False).encode()), 256)
        self.cancel.set()
        with self.assertRaises(KeyboardInterrupt):
            self.tools.execute("grep", {"pattern": "alpha"})
        self.cancel.clear()
        self.tools.close()
        with self.assertRaises((OSError, ToolError)):
            self.tools.grep("alpha")

    def test_either_disabled_alias_blocks_both_direct_dispatch_and_schema(self):
        for disabled in ("search", "grep"):
            config = replace(self.config, disabled_tools=(disabled,))
            with self.subTest(disabled=disabled), closing(Tools(config, self.approve)) as tools:
                names = [row["function"]["name"] for row in Agent(config, Mock(), tools, lambda _: None).schemas]
                self.assertNotIn("grep", names)
                self.assertNotIn("search", names)
                for name, argument in (("grep", "pattern"), ("search", "query")):
                    self.assertFalse(config.tool_enabled(name))
                    with self.assertRaisesRegex(ToolError, "disabled"):
                        getattr(tools, name)("alpha")
                    self.assertIn("disabled", tools.execute(name, {argument: "alpha"})["error"])
        self.assertEqual(len(TOOL_SCHEMAS), 10)  # glob adds a filename tool; search remains an alias.
        names = [row["function"]["name"] for row in TOOL_SCHEMAS]
        self.assertIn("grep", names)
        self.assertNotIn("search", names)

    def test_three_protocols_restore_legacy_search_then_grep_read_edit_and_independent_check(self):
        for protocol in ("chat_completions", "messages", "responses"):
            (self.root / "source.py").write_bytes(b"alpha\nbeta\n")
            history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "Find and repair alpha"},
                       call("legacy", "search", {"query": "alpha|beta"}),
                       {"role": "tool", "tool_call_id": "legacy", "content": '{"matches":[],"truncated":false}'}]
            original, captured, approvals = copy.deepcopy(history), [], []
            def edit(payload):
                observed = next(row for row in objects(payload) if "read_id" in row and "content" in row)
                return wire(protocol, call("edit", "edit_lines", {"path": "source.py", "read_id": observed["read_id"],
                    "edits": [{"start_line": 1, "end_line": 1, "new": "fixed\n"}]}))
            replies = [wire(protocol, call("find", "grep", {"pattern": "alpha|beta", "path": "source.py"})),
                       wire(protocol, call("read", "read_file", {"path": "source.py", "limit": 2})),
                       edit, wire(protocol, {"role": "assistant", "content": "Done"})]
            with self.subTest(protocol=protocol), backend(protocol, replies, captured, stream=protocol != "chat_completions") as url:
                check = (sys.executable, "-I", "-c", "from pathlib import Path; assert Path('source.py').read_bytes() == b'fixed\\nbeta\\n'")
                config = Config(self.root, "fixture", wire_api=protocol, base_url=url, proxy="direct", stream=True,
                                max_steps=4, checks=(check,))
                with closing(Tools(config, lambda text: approvals.append(text) or True)) as tools:
                    saved, complete = Agent(config, Provider(config), tools, lambda _: None).run(messages=copy.deepcopy(history))
            self.assertTrue(complete)
            self.assertEqual(len(captured), 4)
            self.assertEqual(len(approvals), 2)  # A new edit approval and a separate final check.
            self.assertEqual(history, original)
            self.assertEqual((self.root / "source.py").read_bytes(), b"fixed\nbeta\n")
            find = next(row for row in objects(captured[1]) if row.get("matches"))
            self.assertEqual([row["line"] for row in find["matches"]], [1, 2])
            validate_history(saved)


class GrepConfigurationTests(unittest.TestCase):
    setUp = settings_fixtures.SettingsTests.setUp
    write = settings_fixtures.SettingsTests.write
    resolve = settings_fixtures.SettingsTests.resolve

    def test_alias_cannot_restore_trusted_disabled_tool_and_sources_show_cause(self):
        for disabled, alias in (("search", "grep"), ("grep", "search")):
            with self.subTest(disabled=disabled):
                path = self.write(f"[tools]\n{disabled}=false\n{alias}=true\n", self.parent / f"{disabled}.toml")
                settings = load_settings(path)
                config, _ = self.resolve(settings, model="fixture")
                self.assertFalse(config.tool_enabled(alias))
                self.assertEqual(dict(config.setting_sources)["tools." + alias], "tools." + disabled)
                project = self.write(f"[tools]\n{alias}=true\n", self.root / f"{alias}.toml")
                with self.assertRaisesRegex(ValueError, "cannot enable a disabled tool"):
                    self.resolve(settings.with_project(project), model="fixture")


class GrepFoldingTests(unittest.TestCase):
    setUp = folding.InspectionFoldingTests.setUp
    append = folding.InspectionFoldingTests.append
    project = folding.InspectionFoldingTests.project

    def test_old_search_reads_fold_without_retaining_revision_ids(self):
        history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "Keep constraints"}]
        for n in range(16):
            name, field = ("grep", "pattern") if n % 2 else ("search", "query")
            self.append(history, str(n), name, {field: "alpha", "path": "source.py", "output_mode": "read"},
                        {"path": "source.py", "content": "alpha" * 600, "ranges": [[1, 1]],
                         "output_mode": "read", "range_complete": True, "matched_lines": 1,
                         "truncated": False, "sha256": "a" * 64, "read_id": "r0123456789ab-1"})
        history.extend({"role": "user", "content": "Keep this"} for _ in range(6))
        original = copy.deepcopy(history)
        selected, report = self.project(history, replace(self.config, max_context_bytes=6000))
        self.assertTrue(report["within_budget"])
        self.assertTrue(report["folded_inspections"])
        shown = folding.projected_inspections(selected)
        self.assertEqual(len(shown), 16)
        for _, _, result in shown.values():
            self.assertTrue(result["range_complete"])
            self.assertEqual(result["ranges"], [[1, 1]])
            self.assertFalse({"read_id", "sha256", "content"} & result.keys())
        self.assertEqual(history, original)
        validate_history(selected)

    def test_both_search_names_fold_closed_observations_without_changing_saved_history(self):
        history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "Keep constraints"}]
        for n in range(16):
            name, field = ("grep", "pattern") if n % 2 else ("search", "query")
            self.append(history, str(n), name, {field: "alpha|beta"},
                        {"matches": [{"path": "source.py", "text": "alpha" * 600}],
                         "truncated": True, "timed_out": False, "skipped_files": 1})
        history.extend({"role": "user", "content": "Keep this"} for _ in range(6))
        original = copy.deepcopy(history)
        selected, report = self.project(history)
        self.assertTrue(report["within_budget"])
        self.assertTrue(report["folded_inspections"])
        shown = folding.projected_inspections(selected)
        self.assertEqual(len(shown), 16)
        self.assertEqual({row[0] for row in shown.values()}, {"search", "grep"})
        for _, _, result in shown.values():
            self.assertTrue(result["truncated"])
            self.assertEqual(result["skipped_files"], 1)
        self.assertEqual(history, original)
        validate_history(selected)
