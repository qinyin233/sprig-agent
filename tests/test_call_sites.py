"""Fresh, private and bounded syntactic call locations in all indexed languages."""

from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.index import MAX_CACHE_BYTES, MAX_CACHE_ENTRIES, SymbolIndex
from sprig_agent.provider import Provider
from sprig_agent.session import validate_history
from sprig_agent.syntax import IndexUnavailable
from sprig_agent.tools import Tools
from tests.test_edit_checks import DONE, response
from tests.test_planning import replies


SOURCES = {
    "python": ("sample.py", "class Service:\n def caller(self):\n  client.target()\n", "Service.caller", 3),
    "javascript": ("sample.js", "class Service { caller(){ client.target() } }", "Service.caller", 1),
    "typescript": ("sample.ts", "class Service { caller(x: T){ x.target<string>() } }", "Service.caller", 1),
    "tsx": ("sample.tsx", "function App(){ return <div>{client.target()}</div> }", "App", 1),
    "rust": ("sample.rs", "impl Service { fn caller(&self) { self.target(); } }", "Service.caller", 1),
    "go": ("sample.go", "package sample\nfunc caller(){ client.target() }", "caller", 2),
    "c": ("sample.c", "void caller(){ client->target(); }", "caller", 1),
    "cpp": ("sample.cpp", "void caller(){ library::target<T>(); }", "caller", 1),
    "java": ("Sample.java", "class Service { void caller(){ client.target(); } }", "Service.caller", 1),
}


class CallSiteParserTests(unittest.TestCase):
    def setUp(self):
        self.index = SymbolIndex()
        self.addCleanup(self.index.clear)

    def test_all_nine_languages_report_real_call_and_enclosing_declaration(self):
        for language, (_, text, enclosing, line) in SOURCES.items():
            with self.subTest(language=language):
                result = self.index.call_sites(text, language)
                self.assertFalse(result["unresolved"])
                self.assertEqual(len(result["calls"]), 1)
                call = result["calls"][0]
                self.assertEqual((call["name"], call["enclosing"], call["line"]), ("target", enclosing, line))
                self.assertGreater(call["column"], 0)
                self.assertEqual(call["kind"], "call")
                self.assertNotIn("_byte", call)

    def test_same_line_different_methods_and_module_calls_have_correct_enclosures(self):
        text = "class X { a(){target()} b(){target()} } target()"
        calls = self.index.call_sites(text, "javascript")["calls"]
        self.assertEqual([item["enclosing"] for item in calls], ["X.a", "X.b", ""])
        self.assertEqual(len({item["column"] for item in calls}), 3)

    def test_python_nested_async_and_module_enclosures_are_syntactic(self):
        text = "target()\nclass X:\n async def a(self):\n  def inner():\n   target()\n  target()\ntarget()\n"
        calls = self.index.call_sites(text, "python")["calls"]
        self.assertEqual([item["enclosing"] for item in calls], ["", "X.a.inner", "X.a", ""])

    def test_comments_and_plain_strings_are_excluded_but_interpolations_are_not(self):
        for language, text in (
            ("python", "# target()\ns='target()'\nf'{target()}'\n"),
            ("javascript", "// target()\nconst s='target()'; `value: ${target()}`;"),
            ("java", 'class X { String s="target()"; void run(){ /*target()*/ target(); } }'),
        ):
            with self.subTest(language=language):
                calls = self.index.call_sites(text, language)["calls"]
                self.assertEqual([item["name"] for item in calls], ["target"])

    def test_generics_constructors_and_rust_raw_names_keep_name_candidates(self):
        examples = [
            ("javascript", "new Worker(); target()", ["Worker", "target"]),
            ("typescript", "target<T>(); new Worker<T>()", ["target", "Worker"]),
            ("rust", "fn main(){ ns::target::<T>(); r#type(); }", ["target", "type"]),
            ("cpp", "void f(){ ns::target<T>(); new ns::Worker<T>(); }", ["target", "Worker"]),
            ("java", "class X { void f(){ target(); new Worker<String>(); } }", ["target", "Worker"]),
        ]
        for language, text, names in examples:
            with self.subTest(language=language):
                result = self.index.call_sites(text, language)
                self.assertEqual([item["name"] for item in result["calls"]], names)
                self.assertFalse(result["unresolved"])

    def test_dynamic_receivers_and_subscript_calls_are_never_claimed_resolved(self):
        cases = [
            ("python", "factory().target()\nfunctions[0]()\n", ["factory", "target"]),
            ("javascript", "factory().target(); functions[0]();", ["factory", "target"]),
            ("go", "package a\nfunc f(){ target[T](); }", ["target"]),
        ]
        for language, text, names in cases:
            with self.subTest(language=language):
                result = self.index.call_sites(text, language)
                self.assertTrue(result["unresolved"])
                # Nested call expressions can start at the same source byte;
                # their ordering does not describe runtime evaluation order.
                self.assertCountEqual([item["name"] for item in result["calls"]], names)
                self.assertTrue(any(item.get("receiver_unresolved") for item in result["calls"]))

    def test_bom_crlf_cr_unicode_and_utf8_byte_columns(self):
        for language, text in (("python", "\ufeff# 中\r\ndef caller():\r \"中\"; target()\r"),
                               ("javascript", '\ufeff// 中\r\nfunction caller(){\r "中"; target();\r}\r')):
            result = self.index.call_sites(text, language)
            call = result["calls"][0]
            self.assertEqual(call["line"], 3)
            self.assertEqual(call["enclosing"], "caller")
            self.assertEqual(call["column"], len(' "中"; '.encode()) + 1)

    def test_cache_is_content_keyed_detached_and_separate_from_declarations(self):
        text = "def caller():\n target()\n"
        symbols = self.index.symbols(text, "python")
        first = self.index.call_sites(text, "python")
        first["calls"][0]["name"] = "mutated"
        self.assertEqual(self.index.call_sites(text, "python")["calls"][0]["name"], "target")
        self.assertEqual(self.index.symbols(text, "python"), symbols)
        self.assertEqual(self.index.call_sites(text.replace("target", "changed"), "python")["calls"][0]["name"], "changed")
        self.assertEqual(len(self.index.entries), 3)

    def test_cancelled_traversal_and_resource_limits_do_not_poison_cache(self):
        for language, text in (("python", "target(); target()"), ("javascript", "target(); target()")):
            with self.subTest(language=language):
                self.index.clear()
                with patch("sprig_agent.call_sites.MAX_SYMBOLS", 1), self.assertRaises(IndexUnavailable):
                    self.index.call_sites(text, language)
                self.assertFalse(self.index.entries)
                self.assertEqual(len(self.index.call_sites(text, language)["calls"]), 2)
                self.index.clear()
                checks = []
                def cancel():
                    checks.append(True)
                    if len(checks) == 5:
                        raise KeyboardInterrupt
                with self.assertRaises(KeyboardInterrupt):
                    self.index.call_sites(text, language, cancel)
                self.assertFalse(self.index.entries)

    def test_traversal_name_and_target_depth_bounds_are_explicit(self):
        for language in ("python", "javascript"):
            with self.subTest(language=language):
                self.index.clear()
                with patch("sprig_agent.call_sites.MAX_NODES", 1), self.assertRaises(IndexUnavailable):
                    self.index.call_sites("target()", language)
                self.assertFalse(self.index.entries)
                with patch("sprig_agent.call_sites.MAX_NAME_BYTES", 3), self.assertRaises(IndexUnavailable):
                    self.index.call_sites("target()", language)
                self.assertFalse(self.index.entries)
                with self.assertRaises(IndexUnavailable):
                    self.index.call_sites(".".join(["part"] * 70) + "()", language)
                self.assertFalse(self.index.entries)

    def test_malformed_sources_are_flagged_and_can_be_replaced(self):
        for language in ("python", "javascript"):
            self.assertIsNone(self.index.call_sites("def broken( {", language))
            self.assertEqual(self.index.call_sites("target()", language)["calls"][0]["name"], "target")

    def test_cache_entry_and_byte_caps_include_call_metadata(self):
        for number in range(MAX_CACHE_ENTRIES + 20):
            self.index.call_sites(f"def caller_{number}():\n target()\n", "python")
        self.assertLessEqual(len(self.index.entries), MAX_CACHE_ENTRIES)
        self.assertLessEqual(self.index.bytes, MAX_CACHE_BYTES)

    def test_complete_sweep_prunes_only_the_requested_parse_format(self):
        old = "def caller():\n target()\n"
        symbols = self.index.symbols(old, "python")
        spans = self.index.symbols(old, "python", spans=True)
        self.index.call_sites(old, "python")
        with self.index.scan(modes=(b"\0calls\0",)):
            self.index.call_sites("different()", "python")
        self.assertEqual(len(self.index.entries), 3)
        from sprig_agent import index
        with patch.object(index.ast, "parse", wraps=index.ast.parse) as parsed:
            self.assertEqual(self.index.symbols(old, "python"), symbols)
            self.assertEqual(self.index.symbols(old, "python", spans=True), spans)
        self.assertEqual(parsed.call_count, 0)


class CallSiteToolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "offline", read_only=True, max_output=4096)
        self.tools = Tools(self.config, lambda prompt: self.fail("call lookup requested approval"))
        self.addCleanup(self.tools.close)

    def write(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))

    def lookup(self, **arguments):
        return self.tools.execute("project_map", {"query": "target", "include_calls": True, **arguments})

    def test_cross_file_definition_and_call_sites_are_one_bounded_readonly_result(self):
        self.write("library.py", "def target():\n return 1\n")
        self.write("client.py", "from library import target\ndef caller():\n return target()\n")
        result = self.lookup()
        self.assertTrue(result["calls_are_syntactic"])
        self.assertFalse(result["truncated"])
        symbols = [item for file in result["files"] for item in file["symbols"]]
        self.assertEqual([item["kind"] for item in symbols], ["function", "call"])
        self.assertEqual(symbols[1]["enclosing"], "caller")
        self.assertNotIn("return 1", json.dumps(result))
        self.assertNotIn("sha256", json.dumps(result))
        self.assertNotIn("read_id", json.dumps(result))
        self.assertEqual(self.tools.history.state["records"], [])

    def test_default_declaration_map_and_explicit_false_keep_original_shape(self):
        self.write("module.py", "def target():\n target()\n")
        original = self.tools.execute("project_map", {"query": "target"})
        self.assertEqual(original, self.lookup(include_calls=False))
        self.assertEqual(original["files"][0]["symbols"], [{"name": "target", "line": 1, "kind": "function"}])

    def test_interleaved_default_and_call_maps_reuse_both_parse_formats(self):
        self.write("module.py", "def target():\n target()\n")
        self.lookup()
        from sprig_agent import index
        with patch.object(index.ast, "parse", wraps=index.ast.parse) as parsed:
            self.tools.execute("project_map", {"query": "target"})
            self.lookup()
            self.tools.execute("project_map", {"query": "target"})
            self.lookup()
        self.assertEqual(parsed.call_count, 0)
        self.assertLessEqual(self.tools.index.bytes, MAX_CACHE_BYTES)

    def test_exact_definition_survives_many_calls_at_minimum_output_budget(self):
        self.tools.config = replace(self.config, max_output=256)
        self.write("a.py", "def caller():\n" + " target()\n" * 100)
        self.write("z.py", "def target(): pass\n")
        result = self.lookup()
        self.assertTrue(result["truncated"])
        self.assertEqual(result["files"][0]["path"], "z.py")
        self.assertEqual(result["files"][0]["symbols"][0]["kind"], "function")
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False).encode()), 256)

    def test_candidates_match_exact_call_name_without_import_alias_inference(self):
        self.write("client.py", "from lib import target as renamed\nrenamed()\ntargeted()\ntarget()\nother.target()\n")
        result = self.lookup()
        calls = [item for file in result["files"] for item in file["symbols"] if item["kind"] == "call"]
        self.assertEqual([item["line"] for item in calls], [4, 5])
        self.assertTrue(result["calls_are_syntactic"])

    def test_dollar_identifiers_supported_by_javascript_and_java_are_queryable(self):
        self.write("client.js", "function $target(){}; function caller(){ $target() }")
        result = self.lookup(query="$target")
        symbols = [item for file in result["files"] for item in file["symbols"]]
        self.assertEqual([item["kind"] for item in symbols], ["function", "call"])
        self.assertEqual(symbols[1]["enclosing"], "caller")

    def test_unknown_call_targets_make_empty_result_incomplete(self):
        self.write("client.py", "functions[0]()\n")
        result = self.lookup()
        self.assertEqual(result["files"], [])
        self.assertTrue(result["truncated"])

    def test_invalid_binary_and_oversized_files_do_not_create_false_absence(self):
        for raw in (b"target()\x00", b"target()\xff", b"#" * (512 * 1024 + 1)):
            (self.root / "client.py").write_bytes(raw)
            result = self.lookup()
            self.assertTrue(result["truncated"])
            self.assertEqual(result["files"], [])

    def test_nested_paths_fresh_sources_privacy_and_configured_credentials(self):
        self.write("public/client.py", "def caller():\n target()\n")
        self.write("hidden/client.py", "target()\n")
        self.write(".sprigignore", "hidden/\n")
        self.assertEqual([file["path"] for file in self.lookup()["files"]], ["public/client.py"])
        self.write("public/client.py", "def caller():\n changed()\n")
        self.assertEqual(self.lookup()["files"], [])
        self.write("public/client.py", "target()\n")
        self.write(".sprigignore", "public/\nhidden/\n")
        self.assertEqual(self.lookup()["files"], [])
        (self.root / ".sprigignore").unlink()
        key = "configured-private-credential"
        self.tools.config = replace(self.config, api_key=key)
        self.tools.index.clear()
        self.write("public/client.py", "target()\n# " + key)
        self.write("hidden/client.py", "# no source here\n")
        result = self.lookup()
        self.assertTrue(result["truncated"])
        self.assertNotIn(key, json.dumps(result) + repr(self.tools.index.entries))
        self.assertFalse(any(key in entry[0] for entry in self.tools.index.entries.values()))

    def test_invalid_flags_or_non_simple_queries_fail_before_directory_access(self):
        for arguments in ({"include_calls": "true"}, {"include_calls": 1}, {"query": ""}, {"query": "Service.target"},
                          {"query": "src/client.py"}, {"query": "a|b"}):
            with self.subTest(arguments=arguments), patch.object(self.tools, "_directory") as directory:
                self.assertIn("error", self.lookup(**arguments))
                directory.assert_not_called()

    def test_disabled_and_closed_root_guards_apply_to_call_lookup(self):
        self.tools.config = replace(self.config, disabled_tools=("project_map",))
        self.assertIn("disabled", self.lookup()["error"])
        self.tools.config = self.config
        self.tools.close()
        self.assertIn("error", self.lookup())

    def test_loopback_call_lookup_to_fresh_read_edit_and_independent_check(self):
        self.write("library.py", "def target():\n return 'broken'\n")
        self.write("client.py", "from library import target\ndef caller():\n return target()\n")
        captured, prompts = [], []
        def call(identity, name, arguments):
            return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        def read_after_map(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertTrue(result["calls_are_syntactic"])
            self.assertTrue(any(item.get("enclosing") == "caller" for file in result["files"] for item in file["symbols"]))
            return response(call("read", "read_file", {"path": "library.py", "symbol": "target"}))
        def edit_after_read(payload):
            result = json.loads(payload["messages"][-1]["content"])
            self.assertTrue(result["symbol_complete"])
            return response(call("edit", "edit_lines", {"path": "library.py", "read_id": result["read_id"],
                "edits": [{"start_line": 2, "end_line": 2, "new": " return 'fixed'\n"}]}))
        first = response(call("map", "project_map", {"query": "target", "include_calls": True}))
        with replies([first, read_after_map, edit_after_read, DONE], captured) as url:
            config = replace(self.config, read_only=False, base_url=url, proxy="direct", max_steps=4, max_context_bytes=16384,
                             checks=((sys.executable, "-I", "-B", "-c", "import sys; sys.path.insert(0,'.'); from client import caller; assert caller()=='fixed'"),))
            with closing(Tools(config, lambda prompt: prompts.append(prompt) or True)) as tools:
                history, complete = Agent(config, Provider(config), tools, lambda message: None).run("Repair the target")
        self.assertTrue(complete)
        self.assertEqual(validate_history(history), history)
        self.assertEqual(len(captured), 4)
        self.assertTrue(all(size <= 16384 for _, size in captured))
        self.assertEqual(len(prompts), 2)  # The source write and final check each need approval.
        self.assertTrue(history[-1]["tool_call_id"].startswith("sprig-check-"))


if __name__ == "__main__":
    unittest.main()
