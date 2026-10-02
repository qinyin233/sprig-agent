"""Real grammar coverage and bounds, including native Windows/POSIX parsing."""

import json
from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent.config import Config
from sprig_agent.index import SymbolIndex
from sprig_agent.syntax import IndexUnavailable, language_for
from sprig_agent.tools import Tools


FIXTURES = {
    "module.js": ("export class Service { run() {} handler = () => 1; }\nconst worker = () => 1;\nfunction top() {}",
                  ["Service", "Service.run", "Service.handler", "worker", "top"]),
    "module.ts": ("interface Service { run(): void; }\ntype Name = string;\nclass Worker { constructor() {} run() {} }",
                  ["Service", "Service.run", "Name", "Worker", "Worker.constructor", "Worker.run"]),
    "module.tsx": ("export function View() { return <div>hello</div>; }", ["View"]),
    "module.rs": ("struct Service;\ntrait Runner { fn run(&self); }\nimpl Service { fn start(&self) {} }\nmod inner { fn go() {} }",
                  ["Service", "Runner", "Runner.run", "Service.start", "inner", "inner.go"]),
    "module.go": ("package sample\ntype Service struct {}\ntype Runner interface { Run() }\nfunc (s *Service) Run() {}\nfunc Top() {}",
                  ["Service", "Runner", "Runner.Run", "Service.Run", "Top"]),
    "module.c": ("struct Item { int id; };\nint go(int x) { return x; }\nint stop(void);\nint (*callback)(void);",
                 ["Item", "go", "stop"]),
    "module.cpp": ("namespace util { class Service { public: void run(); }; }\nvoid util::Service::run() {}\nint *create() { return nullptr; }",
                   ["util", "util.Service", "util.Service.run", "util::Service::run", "create"]),
    "module.java": ("class Service { Service() {} void run() {} }\ninterface Runner { void start(); }",
                    ["Service", "Service.Service", "Service.run", "Runner", "Runner.start"]),
}


class SyntaxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cancel = threading.Event()
        self.tools = Tools(Config(self.root, "offline", read_only=True, max_output=1000000),
                           lambda _: self.fail("mapping requested approval"), self.cancel)
        self.addCleanup(self.tools.close)

    def test_real_declarations_in_all_installed_grammars(self):
        for name, (source, _) in FIXTURES.items():
            (self.root / name).write_text(source, encoding="utf-8", newline="")
        (self.root / "plain.txt").write_text("function fake() {}", encoding="utf-8")
        result = self.tools.project_map()
        self.assertFalse(result["truncated"])
        by_path = {item["path"]: item for item in result["files"]}
        for name, (source, expected) in FIXTURES.items():
            with self.subTest(name=name):
                symbols = by_path[name]["symbols"]
                self.assertEqual([item["name"] for item in symbols], expected)
                self.assertTrue(all(1 <= item["line"] <= len(source.splitlines()) for item in symbols))
        self.assertEqual(by_path["plain.txt"], {"path": "plain.txt"})

    def test_comments_strings_and_function_bodies_do_not_invent_declarations(self):
        samples = {
            "javascript": '// function fake() {}\nconst message = "class Fake {}";\nfunction real() { function local() {} }',
            "typescript": '/* interface Fake {} */\nconst message = "class Fake {}";\nfunction real() { const local = () => 1; }',
            "tsx": '// function fake() {}\nfunction real() { return <p>function fake() {}</p>; }',
            "rust": '// fn fake() {}\nconst X: &str = "struct Fake;";\nfn real() { fn local() {} }',
            "go": 'package p\n// func fake() {}\nvar message = "type Fake struct {}"\nfunc real() { local := func() {}; _ = local }',
            "c": '// int fake(void);\nconst char *message = "struct Fake {};";\nint real(void) { return 0; }',
            "cpp": '// void fake();\nconst char *message = "class Fake {};";\nvoid real() { auto local = []() {}; }',
            "java": '// class Fake {}\nclass Real { String message = "void fake() {}"; void real() {} }',
        }
        for language, source in samples.items():
            with self.subTest(language=language):
                result = self.tools.index.symbols(source, language)
                self.assertIsNotNone(result)
                names = [item["name"] for item in result]
                self.assertEqual(names, ["Real", "Real.real"] if language == "java" else ["real"])

    def test_unicode_bom_and_physical_newlines_match_ranged_reads(self):
        source = '\ufeff// padding\r\n// second\rfunction 中文() { return "a\u2028b"; }\n'
        (self.root / "one.js").write_text(source, encoding="utf-8", newline="")
        symbols = self.tools.project_map()["files"][0]["symbols"]
        self.assertEqual(symbols, [{"name": "中文", "line": 3, "kind": "function"}])
        self.assertTrue(self.tools.read_file("one.js", offset=symbols[0]["line"], limit=1)["content"].startswith("function 中文"))
        self.assertEqual(self.tools.index.python("\ufeffdef name(): pass\r\n")[0]["line"], 1)

    def test_extensions_and_ambiguous_headers_have_explicit_mapping(self):
        for name, expected in {"a.C": "cpp", "a.c": "c", "a.h": "cpp", "a.HPP": "cpp", "a.mjs": "javascript",
                               "a.cjs": "javascript", "a.jsx": "javascript", "a.mts": "typescript", "a.cts": "typescript",
                               "a.tsx": "tsx", "a.PY": "python", "a.txt": None}.items():
            with self.subTest(name=name):
                self.assertEqual(language_for(name), expected)

    def test_generics_named_expressions_typedefs_and_nested_names(self):
        samples = {
            "javascript": ('const Worker = class { run() {} };\nconst start = function internal() {};',
                           ["Worker", "Worker.run", "start"]),
            "rust": ('struct Service<T> { value: T }\nimpl<T> Service<T> { fn run(&self) {} }',
                     ["Service", "Service.run"]),
            "go": ('package p\ntype Service[T any] struct {}\nfunc (s *Service[T]) Run() {}',
                   ["Service", "Service.Run"]),
            "c": ('typedef struct Item { int id; } Item;\ntypedef int Count;\nint (*callback)(void);',
                  ["Item", "Count"]),
            "cpp": ('namespace outer { namespace inner { template<class T> class Box { public: void run() {} }; } }',
                    ["outer", "outer.inner", "outer.inner.Box", "outer.inner.Box.run"]),
        }
        for language, (source, names) in samples.items():
            with self.subTest(language=language):
                result = self.tools.index.symbols(source, language)
                self.assertIsNotNone(result)
                self.assertEqual([item["name"] for item in result], names)

    def test_anonymous_classes_and_objects_do_not_look_like_named_members(self):
        for language, source, expected in (
            ("javascript", "const object = { callback() {} }; consume(class { hidden() {} });", []),
            ("java", "class Service { Object task = new Object() { void hidden() {} }; void run() {} }",
             ["Service", "Service.run"]),
        ):
            with self.subTest(language=language):
                self.assertEqual([item["name"] for item in self.tools.index.symbols(source, language)], expected)

    def test_same_content_is_cached_per_language_and_results_are_independent(self):
        cache = self.tools.index
        source = "export function one() {}"
        with patch.object(cache.syntax, "parse", wraps=cache.syntax.parse) as parse:
            first = cache.symbols(source, "javascript")
            first[0]["name"] = "poison"
            self.assertEqual(cache.symbols(source, "typescript")[0]["name"], "one")
            self.assertEqual(cache.symbols(source, "javascript")[0]["name"], "one")
            self.assertEqual(parse.call_count, 2)
        self.assertNotIn(source, repr(cache.entries))
        self.tools.close()
        self.assertFalse(cache.syntax.parsers)

    def test_external_edit_ignore_and_secret_are_checked_before_native_cache(self):
        file = self.root / "one.rs"
        file.write_text("fn before() {}", encoding="utf-8")
        original = file.stat()
        self.tools.project_map()
        file.write_text("fn after_() {}", encoding="utf-8")
        os.utime(file, ns=(original.st_atime_ns, original.st_mtime_ns))
        self.assertEqual(self.tools.project_map()["files"][0]["symbols"][0]["name"], "after_")
        (self.root / ".sprigignore").write_text("one.rs\n.sprigignore\n", encoding="utf-8")
        self.assertEqual(self.tools.project_map()["files"], [])
        (self.root / ".sprigignore").unlink()
        secret = "sk-" + "a" * 32
        file.write_text("fn after_() {}\n// " + secret, encoding="utf-8")
        with patch.object(self.tools.index.syntax, "parse", wraps=self.tools.index.syntax.parse) as parse:
            result = self.tools.project_map()
            parse.assert_not_called()
        self.assertTrue(result["files"][0]["symbols_unavailable"])
        self.assertNotIn(secret, json.dumps(result) + repr(self.tools.index.entries))

    def test_invalid_syntax_is_explicit_cached_and_repaired(self):
        file = self.root / "one.ts"
        file.write_text("function broken(", encoding="utf-8")
        with patch.object(self.tools.index.syntax, "parse", wraps=self.tools.index.syntax.parse) as parse:
            self.assertTrue(self.tools.project_map()["files"][0]["symbols_unavailable"])
            self.assertTrue(self.tools.project_map()["files"][0]["symbols_unavailable"])
            file.write_text("function repaired() {}", encoding="utf-8")
            self.assertEqual(self.tools.project_map()["files"][0]["symbols"][0]["name"], "repaired")
            self.assertEqual(parse.call_count, 2)

    def test_traversal_and_symbol_limits_do_not_cache_transient_failures(self):
        for bound in ("MAX_NODES", "MAX_SYMBOLS", "MAX_NAME_BYTES"):
            with self.subTest(bound=bound):
                cache = SymbolIndex()
                with patch("sprig_agent.syntax." + bound, 1):
                    with self.assertRaises(IndexUnavailable):
                        cache.symbols("function first() {}\nfunction second() {}", "javascript")
                self.assertFalse(cache.entries)
                self.assertEqual(len(cache.symbols("function first() {}\nfunction second() {}", "javascript")), 2)
        with patch("sprig_agent.syntax.MAX_NAME_BYTES", 8):
            with self.assertRaises(IndexUnavailable):
                self.tools.index.symbols("class Outer { method() {} }", "javascript")
        self.assertFalse(self.tools.index.entries)

    def test_cancellation_after_native_parse_discards_result_and_cache(self):
        cache = self.tools.index
        from sprig_agent.syntax import declarations

        def cancelled_walk(*args):
            self.cancel.set()
            return declarations(*args)

        with patch("sprig_agent.syntax.declarations", side_effect=cancelled_walk):
            with self.assertRaises(KeyboardInterrupt):
                cache.symbols("function one() {}", "javascript", self.tools._check_cancelled)
        self.assertFalse(cache.entries)
        self.cancel.clear()
        self.assertEqual(cache.symbols("function one() {}", "javascript")[0]["name"], "one")

    def test_oversized_and_over_budget_native_sources_are_marked(self):
        (self.root / "one.go").write_bytes(b"//" + b"x" * (512 * 1024))
        result = self.tools.project_map()
        self.assertTrue(result["truncated"])
        self.assertTrue(result["files"][0]["symbols_unavailable"])
        (self.root / "one.go").unlink()
        (self.root / "one.ts").write_text("\n".join(f"function named_{n}() {{}}" for n in range(100)), encoding="utf-8")
        self.tools.config = replace(self.tools.config, max_output=256)
        result = self.tools.project_map()
        self.assertTrue(result["truncated"])
        self.assertEqual(result["files"], [{"path": "one.ts", "symbols_unavailable": True}])

    def test_native_timeout_and_parser_recovery_in_isolated_process(self):
        # A native regression must fail this test, not kill the entire suite.
        probe = '''
from unittest.mock import patch
from sprig_agent.index import SymbolIndex
from sprig_agent.syntax import IndexUnavailable
source = "function work() { return 2; }\\n" * 18000
with patch("sprig_agent.syntax.MAX_PARSE_MICROS", 1):
    cache = SymbolIndex()
    try:
        cache.symbols(source, "javascript")
    except IndexUnavailable:
        pass
    else:
        raise AssertionError("native parse did not time out")
    assert not cache.entries
cache.clear()
assert cache.symbols("function recovered() {}", "javascript")[0]["name"] == "recovered"
print("timeout and recovery passed")
'''
        environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
        for key in ("SPRIG_API_KEY", "OPENAI_API_KEY"):
            environment.pop(key, None)
        result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=15, env=environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("timeout and recovery passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
