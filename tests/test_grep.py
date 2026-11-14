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

    def test_read_mode_pages_complete_matches_when_the_output_is_too_large(self):
        path = self.root / "source.py"
        body = b"alpha " + b"x" * 130 + b"\n"
        path.write_bytes((body + b"skip\n") * 6)
        args = {"pattern": "alpha", "path": path.name, "output_mode": "read"}
        with closing(Tools(replace(self.config, max_output=600), self.approve)) as tools:
            page = tools.execute("grep", args)
            self.assertTrue(page.get("next_cursor"), page)
            identity = page["read_id"]
            shown = []
            for _ in range(6):
                self.assertTrue(page["range_complete"])
                self.assertEqual(page["read_id"], identity)
                self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 600)
                shown.extend(page["ranges"])
                cursor = page.get("next_cursor")
                if cursor is None:
                    self.assertFalse(page["truncated"])
                    break
                self.assertTrue(page["truncated"])
                page = tools.execute("grep", {**args, "cursor": cursor})
            else:
                self.fail("search pages did not reach the end")
        self.assertEqual(shown, [[line, line] for line in range(1, 12, 2)])

    def test_read_cursor_binds_query_options_path_position_and_file_version(self):
        path = self.root / "source.py"
        args = {"pattern": "alpha", "path": path.name, "output_mode": "read", "limit": 1}
        first = self.tools.execute("grep", args)
        cursor = first["next_cursor"]
        (self.root / "same.py").write_bytes(path.read_bytes())
        for extra in ({"path": "same.py"}, {"pattern": "beta"}, {"ignore_case": True},
                      {"literal": True}, {"context": 1}, {"cursor": cursor.rsplit(":", 1)[0] + ":2"},
                      {"output_mode": "content"}):
            with self.subTest(extra=extra):
                result = self.tools.execute("grep", {**args, "cursor": cursor, **extra})
                self.assertIn("error", result)
                self.assertNotIn("content", result)
                self.assertNotIn("read_id", result)
        resumed = self.tools.execute("grep", {**args, "cursor": cursor, "limit": 2})
        self.assertEqual(resumed["ranges"], [[3, 3]])
        self.assertEqual(resumed["read_id"], first["read_id"])
        self.assertNotIn("next_cursor", resumed)
        path.write_bytes(path.read_bytes() + b"alpha\n")
        stale = self.tools.execute("grep", {**args, "cursor": cursor})
        self.assertIn("restart", stale["error"])
        self.assertNotIn("content", stale)

    def test_read_cursor_preserves_context_without_matching_the_previous_prefix_again(self):
        path = self.root / "source.py"
        path.write_bytes(b"alpha first\r\nalpha second")
        args = {"pattern": "alpha", "path": path.name, "output_mode": "read", "limit": 1, "context": 1}
        first = self.tools.execute("grep", args)
        checked = []
        original = RegexMatcher._search
        def observe(matcher, line):
            checked.append(line)
            return original(matcher, line)
        with patch.object(RegexMatcher, "_search", observe):
            second = self.tools.execute("grep", {**args, "cursor": first["next_cursor"]})
        self.assertEqual(checked, ["alpha second"])
        self.assertEqual(second["content"], first["content"])
        self.assertEqual(second["matched_lines"], 1)
        self.assertFalse(second["truncated"])

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
        path.write_bytes(b"alpha\nalpha\n" + secret.encode())
        before_protection = self.tools.execute("grep", {**args, "limit": 1})
        with closing(Tools(replace(self.config, api_key=secret), self.approve)) as tools:
            result = tools.execute("grep", {**args, "limit": 1})
            self.assertEqual(result["content"], "1: alpha\n")
            self.assertTrue(result["truncated"])
            self.assertNotIn("read_id", result)
            self.assertNotIn("sha256", result)
            self.assertNotIn("next_cursor", result)
            protected = tools.execute("grep", {**args, "cursor": before_protection["next_cursor"]})
            self.assertIn("error", protected)
            self.assertNotIn("content", protected)
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

    def test_source_modes_do_not_publish_partial_source_on_regex_timeout(self):
        search = RegexMatcher._search
        def limited(matcher, line):
            if line == "beta":
                raise RegexBudgetExceeded
            return search(matcher, line)
        for mode in ("read", "windows"):
            with self.subTest(mode=mode), patch.object(RegexMatcher, "_search", limited):
                result = self.tools.grep("alpha", "source.py", output_mode=mode)
            self.assertTrue(result["timed_out"] and result["truncated"])
            self.assertFalse(result.get("files"))
            self.assertNotIn("content", result)
            self.assertNotIn("read_id", result)
        self.approve.assert_not_called()

    def test_windows_return_versioned_source_for_each_file_and_resume_single_file(self):
        nested = self.root / "src"
        nested.mkdir()
        (nested / "a.py").write_bytes("before\r\nalpha界\rafter\n\nalpha again".encode())
        (nested / "b.py").write_bytes(b"alpha\nlast")
        (nested / "ignored.txt").write_bytes(b"alpha")
        with patch.object(self.tools, "_read", wraps=self.tools._read) as read:
            result = self.tools.grep("alpha", "src", glob="*.py", output_mode="windows", context=1)
            self.assertEqual(read.call_count, 2)
        self.assertFalse(result["truncated"])
        a, b = sorted(result["files"], key=lambda row: row["path"])
        self.assertEqual(a["content"], "1: before\r\n2: alpha界\r3: after\n")
        self.assertEqual(b["content"], "1: alpha\n2: last")
        self.assertTrue(a["truncated"] and a["range_complete"])
        self.assertFalse(b["truncated"])
        self.assertNotEqual(a["read_id"], b["read_id"])
        self.assertEqual(self.tools.read_file(a["path"], ranges=a["ranges"])["read_id"], a["read_id"])
        resumed = self.tools.grep("alpha", a["path"], output_mode="read", context=1, cursor=a["next_cursor"])
        self.assertEqual(resumed["read_id"], a["read_id"])
        self.assertEqual(resumed["ranges"], [[4, 5]])
        self.assertFalse(resumed["truncated"])
        self.assertIn("error", self.tools.execute("grep", {"pattern": "alpha", "path": "src",
                                                           "output_mode": "windows", "cursor": a["next_cursor"]}))
        limited = self.tools.grep("alpha", "src", glob="*.py", output_mode="windows", limit=1)
        self.assertEqual(len(limited["files"]), 1)
        self.assertTrue(limited["truncated"])
        self.approve.assert_not_called()

    def test_windows_returns_multiple_merged_and_disjoint_matches_per_file(self):
        (self.root / "a.txt").write_bytes(b"hit a\r\nhit b\r\nafter\r\ngap\r\nnext\r\nhit c\r\nlast\r\n")
        (self.root / "b.txt").write_bytes(b"hit d\r\nafter\r\n")
        result = self.tools.grep("hit", output_mode="windows", matches_per_file=3, context=1)
        a, b = sorted(result["files"], key=lambda row: row["path"])
        self.assertEqual(a["ranges"], [[1, 3], [5, 7]])
        self.assertEqual(a["matched_lines"], 3)
        self.assertEqual(b["matched_lines"], 1)
        self.assertEqual(a["read_id"], self.tools.read_file(a["path"], ranges=a["ranges"])["read_id"])
        for invalid in (0, 9, True, None, "3"):
            with self.subTest(invalid=invalid):
                self.assertIn("matches_per_file", self.tools.execute("grep", {
                    "pattern": "hit", "output_mode": "windows", "matches_per_file": invalid})["error"])
        for mode in ("content", "files", "count", "read"):
            self.assertIn("matches_per_file", self.tools.execute("grep", {
                "pattern": "hit", "path": "a.txt", "output_mode": mode, "matches_per_file": 2})["error"])

    def test_windows_fits_largest_complete_prefix_and_cursor_starts_at_omitted_match(self):
        (self.root / "source.txt").write_bytes(b"hit " + b"x" * 1700 + b"\r\nmiss\r\n"
                                              + b"hit " + b"y" * 1700 + b"\r\nmiss\r\n"
                                              + b"hit " + b"z" * 1700 + b"\r\n")
        arguments = {"pattern": "hit", "path": "source.txt"}
        result = self.tools.grep(**arguments, output_mode="windows", matches_per_file=3)
        row, = result["files"]
        self.assertFalse(result["truncated"])
        self.assertTrue(row["truncated"] and row["range_complete"])
        self.assertEqual(row["ranges"], [[1, 1], [3, 3]])
        self.assertEqual(row["matched_lines"], 2)
        self.assertEqual(len(self.tools.read_versions.snapshot()), 1)
        page = self.tools.grep(**arguments, output_mode="read", cursor=row["next_cursor"])
        self.assertEqual(page["ranges"], [[5, 5]])
        self.assertEqual(page["read_id"], row["read_id"])

    def test_windows_enforce_aggregate_budget_and_privacy_before_issuing_versions(self):
        (self.root / "src").mkdir()
        (self.root / "src" / "large.py").write_bytes(("alpha " + "界" * 4000).encode())
        (self.root / "src" / "small.py").write_bytes(b"alpha\r\n")
        secret = "fixture-private-marker"
        (self.root / "src" / "redacted.py").write_bytes(f"alpha\nalpha\n{secret}".encode())
        (self.root / "src" / ".env").write_bytes(b"alpha\n")
        with closing(Tools(replace(self.config, max_output=1024, api_key=secret), self.approve)) as tools:
            result = tools.execute("grep", {"pattern": "alpha", "path": "src", "output_mode": "windows"})
            rows = {row["path"]: row for row in result["files"]}
            self.assertEqual(set(rows), {"src/large.py", "src/small.py", "src/redacted.py"})
            self.assertEqual(rows["src/large.py"], {"path": "src/large.py", "line": 1, "window_omitted": True})
            self.assertEqual(rows["src/small.py"]["content"], "1: alpha\r\n")
            self.assertTrue(rows["src/small.py"]["read_id"].endswith("-1"))
            self.assertFalse({"read_id", "sha256", "next_cursor"} & rows["src/redacted.py"].keys())
            self.assertTrue(result["truncated"])
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 1024)
        with closing(Tools(replace(self.config, disabled_tools=("read_file",)), self.approve)) as tools:
            self.assertIn("disabled", tools.execute("grep", {"pattern": "alpha", "output_mode": "windows"})["error"])
        self.approve.assert_not_called()

    def test_windows_drop_a_result_excluded_after_it_was_read(self):
        render = self.tools._render_ranges
        ids = []

        def exclude(*args, **kwargs):
            window = render(*args, **kwargs)
            ids.append(window["read_id"])
            (self.root / ".sprigignore").write_bytes(b"source.py\n")
            return window

        with patch.object(self.tools, "_render_ranges", side_effect=exclude):
            result = self.tools.grep("alpha", "source.py", output_mode="windows")
        self.assertEqual(result["files"], [])
        self.assertEqual(result["skipped_files"], 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(ids), 1)
        self.assertIsNone(self.tools.read_versions.resolve(ids[0], ("source.py",)))

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

    def test_windows_support_two_revision_bound_edits_on_all_three_wire_protocols(self):
        from tests.test_glob_protocols import answer, values
        from tests.test_native_protocols import backend as native_backend

        source = self.root / "src"
        source.mkdir()
        for protocol in ("chat_completions", "messages", "responses"):
            for stream in (False, True):
                for name in ("a.py", "b.py"):
                    (source / name).write_bytes(b"old_value = 0\n")
                approvals, captured = [], []

                def edit(payload):
                    windows = next(row["files"] for row in values(payload) if row.get("output_mode") == "windows"
                                   and "files" in row)
                    proposals = [("edit-" + str(index), "edit_lines", {"path": window["path"],
                                  "read_id": window["read_id"], "edits": [
                                      {"start_line": 1, "end_line": 1, "new": "new_value = 1\n"}]})
                                 for index, window in enumerate(windows)]
                    return answer(protocol, stream, calls=proposals)

                replies = [answer(protocol, stream, calls=[("inspect", "grep", {
                    "path": "src", "pattern": "^old_value", "output_mode": "windows"})]),
                    edit, answer(protocol, stream, text="Done")]
                with (self.subTest(protocol=protocol, stream=stream),
                      native_backend(protocol, replies, captured, stream=stream) as url):
                    check = (sys.executable, "-I", "-B", "-c", "from pathlib import Path; "
                             "assert all(Path('src', name).read_bytes() == b'new_value = 1\\n' for name in ('a.py','b.py'))")
                    config = Config(self.root, "fixture", wire_api=protocol, base_url=url, proxy="direct", stream=stream,
                                    max_steps=3, max_context_bytes=16384, checks=(check,))
                    with closing(Tools(config, lambda text: approvals.append(text) or True)) as tools:
                        history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Update both values")
                    self.assertTrue(complete)
                    self.assertEqual(len(captured), 3)
                    self.assertEqual(len(approvals), 3)
                    self.assertEqual((source / "a.py").read_bytes(), b"new_value = 1\n")
                    self.assertEqual((source / "b.py").read_bytes(), b"new_value = 1\n")
                    self.assertTrue(any(row.get("sprig_check") == "acceptance" for row in history))
                    validate_history(history)


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

    def test_old_cross_file_windows_drop_nested_provenance_with_the_body(self):
        history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "Keep constraints"}]
        for n in range(16):
            self.append(history, str(n), "grep", {"pattern": "alpha", "path": "src", "output_mode": "windows"},
                        {"output_mode": "windows", "truncated": False, "files": [{"path": "src/a.py",
                         "content": "alpha" * 600, "read_id": "r0123456789ab-1", "next_cursor": "s" + "b" * 64 + ":2"}]})
        history.extend({"role": "user", "content": "Keep this"} for _ in range(6))
        original = copy.deepcopy(history)
        selected, report = self.project(history, replace(self.config, max_context_bytes=6000))
        self.assertTrue(report["within_budget"])
        self.assertEqual(len(folding.projected_inspections(selected)), 16)
        self.assertNotIn("r0123456789ab-1", json.dumps(selected))
        self.assertNotIn("next_cursor", json.dumps(selected))
        self.assertEqual(history, original)
        validate_history(selected)

    def test_old_search_reads_fold_without_retaining_revision_ids(self):
        history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "Keep constraints"}]
        for n in range(16):
            name, field = ("grep", "pattern") if n % 2 else ("search", "query")
            self.append(history, str(n), name, {field: "alpha", "path": "source.py", "output_mode": "read"},
                        {"path": "source.py", "content": "alpha" * 600, "ranges": [[1, 1]],
                         "output_mode": "read", "range_complete": True, "matched_lines": 1,
                         "truncated": True, "sha256": "a" * 64, "read_id": "r0123456789ab-1",
                         "next_cursor": "s" + "b" * 64 + ":2"})
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
            self.assertFalse({"read_id", "sha256", "content", "next_cursor"} & result.keys())
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
