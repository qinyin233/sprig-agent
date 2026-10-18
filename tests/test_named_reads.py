"""Declaration reads use one validated snapshot and the normal edit protocol."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.index import SymbolIndex
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.syntax import IndexUnavailable
from sprig_agent.tools import MAX_FILE, Tools
from tests.test_planning import replies


SOURCES = [
    ("source.py", "Service.run", "# before\nclass Service:\n    @staticmethod\n    def run():\n        return 'selected'\n\ndef other(): pass\n", 3, 5),
    ("source.js", "Service.run", "// before\nclass Service {\n  run() {\n    return 'selected';\n  }\n}\nfunction other() {}\n", 3, 5),
    ("source.ts", "Service.run", "// before\nclass Service {\n  run(): string {\n    return 'selected';\n  }\n}\nfunction other() {}\n", 3, 5),
    ("source.tsx", "View", "// before\nfunction View() {\n  return <div>selected</div>;\n}\nfunction Other() { return null; }\n", 2, 4),
    ("source.rs", "Service.run", 'struct Service;\nimpl Service {\n fn run(&self) -> &str {\n  "selected"\n }\n}\nfn other() {}\n', 3, 5),
    ("source.go", "Service.Run", 'package p\ntype Service struct{}\nfunc (s *Service) Run() string {\n return "selected"\n}\nfunc Other() {}\n', 3, 5),
    ("source.c", "run", '// before\nconst char *run(void) {\n return "selected";\n}\nint other(void) { return 0; }\n', 2, 4),
    ("source.cpp", "scope.Service.run", 'namespace scope {\nclass Service {\npublic:\n const char *run() {\n  return "selected";\n }\n};\n}\nvoid other() {}\n', 4, 6),
    ("source.java", "Service.run", 'class Service {\n String run() {\n  return "selected";\n }\n void other() {}\n}\n', 2, 4),
]


def call(identity, name, arguments):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": identity, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class NamedReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "fixture", max_output=6000)
        self.approve = Mock(return_value=True)
        self.cancel = threading.Event()
        self.tools = Tools(self.config, self.approve, self.cancel)
        self.addCleanup(self.tools.close)

    def read(self, path="source.py", symbol="Service.run", **kwargs):
        return self.tools.execute("read_file", {"path": path, "symbol": symbol, **kwargs})

    def test_all_nine_languages_select_expected_physical_lines_and_revision(self):
        for path, name, source, start, end in SOURCES:
            with self.subTest(language=path):
                raw = source.encode()
                (self.root / path).write_bytes(raw)
                result = self.read(path, name)
                self.assertNotIn("error", result)
                self.assertEqual((result["symbol_start_line"], result["symbol_end_line"]), (start, end))
                lines = source.splitlines(keepends=True)
                self.assertEqual(result["content"], "".join(f"{number}: " + lines[number - 1] for number in range(start, end + 1)))
                self.assertTrue(result["symbol_complete"])
                self.assertTrue(result["truncated"], "file has more lines beyond the selected declaration")
                self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertIn("read_id", result)
                raw_result = self.read(path, name, line_numbers=False)
                self.assertEqual(raw_result["content"], "".join(lines[start - 1:end]))
                self.assertNotIn("line_numbers", raw_result)
        self.approve.assert_not_called()

    def test_python_bom_unicode_names_and_all_physical_line_endings(self):
        for ending in ("\n", "\r\n", "\r"):
            source = "\ufeffclass 服务:" + ending + "    def 处理(self):" + ending + "        return '中文🙂'" + ending
            raw = source.encode()
            (self.root / "source.py").write_bytes(raw)
            result = self.read(symbol="服务.处理", line_numbers=False)
            self.assertTrue(result["symbol_complete"], result)
            self.assertEqual(result["content"], ending.join(source.split(ending)[1:]))
            self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())

    def test_ambiguous_overloads_and_missing_symbols_never_choose_a_definition(self):
        source = "\n".join("def same(): return " + str(number) for number in range(30))
        (self.root / "source.py").write_text(source, encoding="utf-8")
        with closing(Tools(replace(self.config, max_output=256), self.approve)) as tools:
            result = tools.execute("read_file", {"path": "source.py", "symbol": "same"})
        self.assertEqual(result["symbol_match_count"], 30)
        self.assertTrue(result["symbol_locations_truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()), 256)
        self.assertNotIn("read_id", result)
        self.assertNotIn("content", result)
        result = self.read(symbol="missing")
        self.assertEqual(result["symbol_match_count"], 0)
        self.assertNotIn("sha256", result)

    def test_output_cap_reports_incomplete_symbol_and_continues_with_normal_ranges(self):
        source = "class Service:\n    def run(self):\n" + "        value = '中🙂'\n" * 80 + "        return value\n"
        (self.root / "source.py").write_bytes(source.encode())
        with closing(Tools(replace(self.config, max_output=256), self.approve)) as tools:
            result = tools.execute("read_file", {"path": "source.py", "symbol": "Service.run"})
            self.assertFalse(result["symbol_complete"])
            self.assertLess(result["end_line"], result["symbol_end_line"])
            self.assertIn("read_id", result)
            self.assertEqual(result["next_offset"], result["end_line"] + 1)
            next_page = tools.execute("read_file", {"path": "source.py", "offset": result["next_offset"], "limit": 2})
            self.assertNotIn("error", next_page)
            self.assertIn("value =", next_page["content"])
        (self.root / "source.py").write_text("def long(value='" + "x" * 1000 + "'):\n    pass\n")
        with closing(Tools(replace(self.config, max_output=256), self.approve)) as tools:
            result = tools.execute("read_file", {"path": "source.py", "symbol": "long"})
        self.assertTrue(result["line_truncated"])
        self.assertFalse(result["symbol_complete"])
        self.assertNotIn("read_id", result)

    def test_parse_and_read_use_one_snapshot_and_stale_revision_cannot_be_edited(self):
        original = b"class Service:\n    def run(self):\n        return 'old'\n"
        changed = original.replace(b"old", b"new")
        path = self.root / "source.py"
        path.write_bytes(original)
        parse = self.tools.index.symbols
        def mutate(*args, **kwargs):
            result = parse(*args, **kwargs)
            path.write_bytes(changed)
            return result
        with patch.object(self.tools.index, "symbols", side_effect=mutate):
            result = self.read()
        self.assertIn("'old'", result["content"])
        self.assertEqual(result["sha256"], hashlib.sha256(original).hexdigest())
        edit = self.tools.execute("edit_lines", {"path": "source.py", "read_id": result["read_id"],
            "edits": [{"start_line": 3, "end_line": 3, "new": "        return 'unsafe'\n"}]})
        self.assertIn("error", edit)
        self.assertEqual(path.read_bytes(), changed)
        self.approve.assert_not_called()

    def test_privacy_is_rechecked_after_cache_hits_and_known_credentials_hide_revision(self):
        source = "class Service:\n    def run(self):\n        return 'ordinary'\n"
        path = self.root / "source.py"
        path.write_text(source)
        self.assertTrue(self.read()["symbol_complete"])
        for suffix in ("\0", "\udcff", "\n# sk-" + "a" * 32):
            path.write_bytes(source.encode() + suffix.encode("utf-8", errors="surrogateescape"))
            result = self.read()
            self.assertIn("error", result)
            self.assertNotIn("content", result)
            self.assertNotIn("sha256", result)
        secret = "configured-private-value"
        path.write_text(source + "# " + secret)
        with closing(Tools(replace(self.config, api_key=secret), self.approve)) as tools:
            result = tools.execute("read_file", {"path": "source.py", "symbol": "Service.run"})
        self.assertNotIn("sha256", result)
        self.assertNotIn("read_id", result)
        self.assertNotIn(secret, json.dumps(result))

    def test_invalid_modes_disabled_reads_and_privacy_paths_fail_before_parsing(self):
        source = "class Service:\n    def run(self): pass\n"
        (self.root / "source.py").write_text(source)
        with patch.object(self.tools.index, "symbols", side_effect=AssertionError("must not parse")):
            for arguments in ({"symbol": None}, {"symbol": ""}, {"symbol": "中" * 400}, {"symbol": "a\n"},
                              {"symbol": "Service.run", "offset": 2}, {"symbol": "Service.run", "limit": 1},
                              {"symbol": "Service.run", "cursor": "start"}):
                result = self.tools.execute("read_file", {"path": "source.py", **arguments})
                self.assertIn("error", result)
            for path in ("../source.py", ".env", ".git/config"):
                self.assertIn("error", self.read(path))
        with closing(Tools(replace(self.config, disabled_tools=("read_file",)), self.approve)) as tools:
            self.assertIn("error", tools.execute("read_file", {"path": "source.py", "symbol": "Service.run"}))
        with closing(Tools(replace(self.config, read_only=True), self.approve)) as tools:
            self.assertTrue(tools.execute("read_file", {"path": "source.py", "symbol": "Service.run"})["symbol_complete"])
        self.approve.assert_not_called()

    def test_parse_limits_unsupported_files_syntax_errors_and_cancellation(self):
        (self.root / "source.txt").write_text("ordinary text")
        self.assertIn("error", self.read("source.txt"))
        (self.root / "source.py").write_bytes(b"#" + b" " * MAX_FILE)
        self.assertIn("512 KiB", self.read()["error"])
        (self.root / "source.py").write_text("def broken(:\n")
        self.assertIn("parsed", self.read()["error"])
        (self.root / "source.py").write_text("class Service:\n    def run(self): pass\n")
        with patch.object(self.tools.index, "symbols", side_effect=IndexUnavailable("fixture")):
            self.assertIn("resource limit", self.read()["error"])
        self.cancel.set()
        with self.assertRaises(KeyboardInterrupt):
            self.read()

    def test_span_cache_is_detached_metadata_and_ordinary_map_shape_stays_unchanged(self):
        source = "class Service:\n    def run(self):\n        return 'body-never-in-cache'\n"
        index = SymbolIndex()
        first = index.symbols(source, "python", spans=True)
        first[0]["end_line"] = 99999
        again = index.symbols(source, "python", spans=True)
        self.assertEqual(again[0]["end_line"], 3)
        ordinary = index.symbols(source, "python")
        self.assertTrue(all(set(item) == {"name", "line", "kind"} for item in ordinary))
        self.assertNotIn("body-never-in-cache", repr(index.entries))

    def test_actual_http_named_read_to_fresh_edit_approval_and_independent_fixed_checks(self):
        (self.root / "source.py").write_bytes(("# unrelated padding\n" * 100 +
            "class Service:\n    def run(self):\n        return 2\n\ndef unrelated(): return -1\n").encode())
        checker = self.parent / "acceptance.py"
        checker.write_text("import runpy\nassert runpy.run_path('source.py')['Service']().run() == 4\n")
        captured = []
        def edit(request):
            result = json.loads(next(message["content"] for message in request["messages"]
                                     if message.get("tool_call_id") == "named"))
            self.assertTrue(result["symbol_complete"])
            return call("edit", "edit_lines", {"path": "source.py", "read_id": result["read_id"],
                "edits": [{"start_line": result["symbol_end_line"], "end_line": result["symbol_end_line"], "new": "        return 4\n"}]})
        messages = [call("named", "read_file", {"path": "source.py", "symbol": "Service.run"}), edit,
                    {"role": "assistant", "content": "Complete"}]
        with replies(messages, captured) as url:
            config = replace(self.config, base_url=url, proxy="direct", max_steps=3, timeout=10,
                             checks=((sys.executable, "-I", "-B", str(checker)),), check_after_edit=True)
            with closing(Tools(config, self.approve)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda _: None).run("Make Service.run return 4")
        self.assertTrue(complete)
        self.assertEqual(len(captured), 3)
        self.assertEqual(self.approve.call_count, 3, "one edit plus independent checkpoint and final commands")
        self.assertEqual(validate_history(history), history)
        self.assertIn(b"return 4", (self.root / "source.py").read_bytes())
        read_schema = next(tool for tool in captured[0][0]["tools"] if tool["function"]["name"] == "read_file")
        self.assertIn("symbol", read_schema["function"]["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
