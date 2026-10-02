import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent.config import Config
from sprig_agent.session import SessionError, SessionStore, validate_history
from sprig_agent.tools import MAX_FILE, Tools


@unittest.skipUnless(os.name in {"posix", "nt"}, "requires a supported platform")
class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "test-model", api_key="test-only-secret", max_output=1024, timeout=2)
        self.prompts = []
        self.allowed = False
        self.tools = Tools(self.config, self.approve)

    def tearDown(self):
        self.tools.close()
        self.temp.cleanup()

    def approve(self, description):
        self.prompts.append(description)
        return self.allowed

    def call(self, name, **kwargs):
        return self.tools.execute(name, kwargs)

    def test_read_list_search(self):
        (self.root / "a.txt").write_text("hello\nworld\nhello again\n", encoding="utf-8", newline="")
        (self.root / "sub").mkdir()
        (self.root / "sub/b.txt").write_text("hello too", encoding="utf-8")
        self.assertEqual(self.call("read_file", path="a.txt")["content"], "hello\nworld\nhello again\n")
        self.assertEqual(len(self.call("list_files")["entries"]), 2)
        found = self.call("search", query="hello")
        self.assertEqual(len(found["matches"]), 3)
        self.assertFalse(found["truncated"])
        self.assertEqual(self.prompts, [])

    def test_read_cap_handles_split_utf8(self):
        (self.root / "a").write_text("é" * 600, encoding="utf-8")
        self.tools.close()
        self.config = Config(self.root, "test", max_output=1025)
        self.tools = Tools(self.config, self.approve)
        result = self.call("read_file", path="a")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["content"], "é" * 512)

    def test_rejects_binary_and_non_utf8(self):
        for data in (b"binary\x00", b"\xffinvalid"):
            (self.root / "a").write_bytes(data)
            self.assertIn("error", self.call("read_file", path="a"))
            self.assertEqual(self.call("search", query="binary")["matches"], [])

    def test_path_escapes_and_protected_components(self):
        (self.root / ".git").mkdir()
        (self.root / ".git/config").write_text("hidden")
        (self.root / ".env").write_text("hidden")
        for path in ("../outside", "/etc/passwd", "a/../../escape", ".git/config", ".env", ".env.local", "sub/.env/key", "a\\b", "a\0b"):
            with self.subTest(path=path):
                self.assertIn("error", self.call("read_file", path=path))
                self.assertIn("error", self.call("edit_file", path=path, old="", new="bad"))
        self.assertEqual(self.call("list_files")["entries"], [])
        self.assertEqual(self.call("search", query="hidden")["matches"], [])
        self.assertEqual(self.prompts, [])

    @unittest.skipUnless(os.name == "posix", "Windows reparse points covered in native tests")
    def test_symlinks_to_inside_and_outside_are_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "file").write_text("outside")
        (self.root / "real").write_text("inside")
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        (self.root / "local-link").symlink_to(self.root / "real")
        (self.root / "broken").symlink_to(outside / "new")
        for path in ("link/file", "local-link", "broken"):
            self.assertIn("error", self.call("read_file", path=path))
            self.assertIn("error", self.call("edit_file", path=path, old="", new="bad"))
        self.assertIn("error", self.call("run", argv=[sys.executable, "--version"], cwd="link"))
        self.assertEqual(len(self.call("list_files")["entries"]), 1)
        self.assertFalse((outside / "new").exists())
        self.assertEqual(self.prompts, [])

    @unittest.skipUnless(os.name == "posix", "POSIX FIFO")
    def test_fifo_read_does_not_block(self):
        os.mkfifo(self.root / "fifo")
        start = time.monotonic()
        self.assertIn("error", self.call("read_file", path="fifo"))
        self.assertLess(time.monotonic() - start, 0.5)

    def test_edit_rejected_without_change(self):
        file = self.root / "a"
        file.write_text("before\n")
        result = self.call("edit_file", path="a", old="before", new="after")
        self.assertEqual(result, {"approved": False, "changed": False})
        self.assertEqual(file.read_text(), "before\n")
        self.assertIn("-before", self.prompts[0])
        self.assertIn("+after", self.prompts[0])

    def test_edit_approved_atomic_preserves_mode_and_newline_diff(self):
        file = self.root / "a"
        file.write_text("before")
        file.chmod(0o755)
        self.allowed = True
        result = self.call("edit_file", path="a", old="before", new="after\n")
        self.assertTrue(result["changed"])
        self.assertEqual(file.read_text(), "after\n")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o755)
        self.assertIn("No newline", self.prompts[0])
        self.assertEqual(list(self.root.glob(".sprig-*.tmp")), [])

    def test_new_file_and_empty_file_need_approval(self):
        self.assertFalse(self.call("edit_file", path="new", old="", new="content")["changed"])
        self.assertFalse((self.root / "new").exists())
        self.allowed = True
        self.assertTrue(self.call("edit_file", path="new", old="", new="content")["changed"])
        self.assertEqual((self.root / "new").read_text(), "content")
        self.assertTrue(self.call("edit_file", path="empty", old="", new="")["changed"])
        self.assertEqual((self.root / "empty").read_bytes(), b"")
        self.assertIn("create empty file", self.prompts[-1])

    def test_edits_reject_ambiguous_missing_empty_or_oversized_old(self):
        (self.root / "a").write_text("one one")
        for old in ("one", "absent", ""):
            self.assertIn("error", self.call("edit_file", path="a", old=old, new="two"))
        self.assertIn("error", self.call("edit_file", path="missing", old="old", new="new"))
        self.assertIn("error", self.call("edit_file", path="missing/child", old="", new="new"))
        self.assertIn("error", self.call("edit_file", path="a", old="one one", new="x" * (MAX_FILE + 1)))
        self.assertEqual(self.prompts, [])

    @unittest.skipUnless(os.name == "posix", "Windows approval races covered in native tests")
    def test_change_or_symlink_swap_during_approval_aborts(self):
        file = self.root / "a"
        file.write_text("old")
        def changing_approval(_):
            file.write_text("other editor")
            return True
        self.tools.approve = changing_approval
        self.assertIn("error", self.call("edit_file", path="a", old="old", new="new"))
        self.assertEqual(file.read_text(), "other editor")
        outside = Path(self.temp.name) / "outside"
        outside.write_text("outside")
        def swapping_approval(_):
            file.unlink()
            file.symlink_to(outside)
            return True
        self.tools.approve = swapping_approval
        self.assertIn("error", self.call("edit_file", path="a", old="other editor", new="new"))
        self.assertEqual(outside.read_text(), "outside")

    @unittest.skipUnless(os.name == "posix", "Windows pins parent handles instead")
    def test_parent_symlink_swap_cannot_redirect_approved_write(self):
        (self.root / "sub").mkdir()
        (self.root / "sub/a").write_text("old")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "a").write_text("outside")
        def swapping_parent(_):
            (self.root / "sub").rename(self.root / "original")
            (self.root / "sub").symlink_to(outside, target_is_directory=True)
            return True
        self.tools.approve = swapping_parent
        self.assertTrue(self.call("edit_file", path="sub/a", old="old", new="new")["changed"])
        self.assertEqual((outside / "a").read_text(), "outside")
        self.assertEqual((self.root / "original/a").read_text(), "new")

    def test_unknown_tool_and_bad_arguments(self):
        self.assertIn("error", self.call("nonsense"))
        self.assertIn("error", self.tools.execute("read_file", []))
        self.assertIn("error", self.call("read_file", path="a", unexpected=True))

    def test_known_key_redacted_in_read_search_and_errors(self):
        key = self.config.api_key
        (self.root / "a").write_text(key)
        self.assertEqual(self.call("read_file", path="a")["content"], "[REDACTED]")
        self.assertNotIn(key, json.dumps(self.call("search", query="test")))
        self.assertNotIn(key, json.dumps(self.call("read_file", path=key)))

    def test_search_and_list_caps(self):
        (self.root / "a").write_text("hit\n" * 2000)
        result = self.call("search", query="hit")
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["matches"]), 2000)
        for i in range(100):
            (self.root / f"file-{i}").touch()
        self.assertTrue(self.call("list_files")["truncated"])

    def test_project_map_exposes_structure_without_whole_file_bodies(self):
        (self.root / "module.py").write_text("class Service:\n    def run(self):\n        return 'private implementation'\n\nasync def main():\n    pass\n", encoding="utf-8")
        (self.root / "README.md").write_text("documentation")
        (self.root / ".env.secret").write_text("hidden")
        result = self.call("project_map")
        self.assertFalse(result["truncated"])
        record = next(item for item in result["files"] if item["path"] == "module.py")
        self.assertEqual(record["symbols"], [{"name": "Service", "line": 1, "kind": "class"},
                                             {"name": "Service.run", "line": 2, "kind": "function"},
                                             {"name": "main", "line": 5, "kind": "function"}])
        self.assertNotIn("private implementation", json.dumps(result))
        self.assertNotIn(".env", json.dumps(result))
        self.assertEqual(self.prompts, [])

    def test_project_map_marks_invalid_python_and_output_limits(self):
        (self.root / "broken.py").write_text("def broken(", encoding="utf-8")
        self.assertTrue(self.call("project_map")["files"][0]["symbols_unavailable"])
        (self.root / "large.py").write_text("".join(f"def function_{i}(): pass\n" for i in range(1000)), encoding="utf-8")
        self.assertTrue(self.call("project_map")["truncated"])

    def test_command_rejection_and_exact_argv_prompt(self):
        argv = [sys.executable, "-c", "raise SystemExit('must not run')", "a b", "$(echo unsafe)"]
        with patch("subprocess.Popen") as popen:
            self.assertEqual(self.call("run", argv=argv), {"approved": False})
            popen.assert_not_called()
        self.assertIn(json.dumps(argv), self.prompts[0])
        self.assertIn(json.dumps(str(self.root)), self.prompts[0])
        self.assertIn("NOT SANDBOXED", self.prompts[0])

    def test_command_argv_is_not_shell_and_reports_failure(self):
        self.allowed = True
        script = "import sys; print(sys.argv[1]); raise SystemExit(7)"
        result = self.call("run", argv=[sys.executable, "-c", script, "$(touch pwned); *"])
        self.assertEqual(result["returncode"], 7)
        self.assertIn("$(touch pwned); *", result["output"])
        self.assertFalse((self.root / "pwned").exists())

    def test_command_timeout_and_output_cap(self):
        self.allowed = True
        started = time.monotonic()
        result = self.call("run", argv=[sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)"], timeout=0.15)
        self.assertTrue(result["timed_out"])
        self.assertIn("start", result["output"])
        self.assertLess(time.monotonic() - started, 2)
        result = self.call("run", argv=[sys.executable, "-c", "print('x' * 1000000)"])
        self.assertTrue(result["output_limited"])
        self.assertEqual(len(result["output"].encode()), 1024)

    def test_descendant_pipe_does_not_defeat_timeout(self):
        self.allowed = True
        script = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])"
        started = time.monotonic()
        result = self.call("run", argv=[sys.executable, "-c", script], timeout=0.2)
        self.assertTrue(result["timed_out"])
        self.assertLess(time.monotonic() - started, 2)

    def test_command_key_removed_and_known_key_output_redacted(self):
        self.allowed = True
        script = "import os; print(os.getenv('SPRIG_API_KEY')); print(os.getenv('OPENAI_API_KEY')); print(os.getenv('ALIAS')); print(os.getenv('LANG'))"
        with patch.dict(os.environ, {"SPRIG_API_KEY": self.config.api_key,
                                     "OPENAI_API_KEY": "other-key", "ALIAS": self.config.api_key,
                                     "LANG": "ordinary"}):
            result = self.call("run", argv=[sys.executable, "-c", script])
        self.assertEqual(result["output"].replace("\r\n", "\n"), "None\nNone\nNone\nordinary\n")
        (self.root / "key.txt").write_text(self.config.api_key)
        result = self.call("run", argv=[sys.executable, "-c", "print(open('key.txt').read())"])
        self.assertEqual(result["output"].replace("\r\n", "\n"), "[REDACTED]\n")
        self.assertIn("error", self.call("run", argv=["echo", self.config.api_key]))

    def test_command_invalid_arguments_timeout_and_executable(self):
        for argv in ("echo hello", [], [""], [1], ["echo", "\0"]):
            self.assertIn("error", self.call("run", argv=argv))
        for timeout in (0, -1, 3, "2", True, float("nan"), float("inf")):
            self.assertIn("error", self.call("run", argv=["echo"], timeout=timeout))
        self.allowed = True
        self.assertIn("error", self.call("run", argv=["/sprig-no-such-program"]))


@unittest.skipUnless(os.name == "posix", "private sessions require POSIX owner-only permissions")
class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve() / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "test-model", api_key="test-secret")
        self.directory = Path(self.temp.name).resolve() / "sessions"
        self.store = SessionStore(self.directory, self.config)
        self.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "task"}]

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_save_load_atomic_permissions_and_redaction(self):
        self.messages.append({"role": "assistant", "content": self.config.api_key})
        self.store.save("test", self.messages)
        file = self.directory / "test.json"
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.assertNotIn(self.config.api_key, file.read_text())
        self.assertNotIn("api_key", file.read_text())
        self.assertEqual(self.store.load("test")[-1]["content"], "[REDACTED]")
        self.assertEqual(list(self.directory.glob("*.tmp")), [])

    def test_names_and_existing_sessions(self):
        for name in ("../escape", "/abs", "", ".hidden", "a/b", "a" * 65):
            with self.assertRaises(SessionError):
                self.store.save(name, self.messages)
        self.store.ensure_new("new")
        self.store.save("new", self.messages)
        with self.assertRaises(SessionError):
            self.store.ensure_new("new")

    def test_session_directory_cannot_be_in_workspace_or_repo(self):
        with self.assertRaises(SessionError):
            SessionStore(self.root / "sessions", self.config)
        (self.root / ".git").mkdir()
        (self.root / ".git/HEAD").write_text("ref: refs/heads/main\n")
        nested = self.root / "src"
        nested.mkdir()
        with self.assertRaises(SessionError):
            SessionStore(self.root / "elsewhere", Config(nested, "test-model"))

    def test_directory_symlinks_and_unsafe_modes_rejected(self):
        link = Path(self.temp.name) / "linked"
        link.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(OSError):
            SessionStore(link, self.config)
        self.directory.chmod(0o755)
        with self.assertRaises(SessionError):
            SessionStore(self.directory, self.config)
        self.directory.chmod(0o700)

    def test_session_symlink_or_unsafe_mode_cannot_read_or_overwrite(self):
        outside = Path(self.temp.name) / "outside"
        outside.write_text("untouched")
        (self.directory / "link.json").symlink_to(outside)
        for operation in (lambda: self.store.load("link"), lambda: self.store.save("link", self.messages)):
            with self.assertRaises(SessionError):
                operation()
        self.assertEqual(outside.read_text(), "untouched")
        self.store.save("test", self.messages)
        (self.directory / "test.json").chmod(0o644)
        with self.assertRaises(SessionError):
            self.store.load("test")

    def test_endpoint_model_and_workdir_binding_fail_closed(self):
        self.store.save("test", self.messages)
        self.store.release("test")
        other = Path(self.temp.name) / "other"
        other.mkdir()
        for config in (Config(self.root, "other"), Config(self.root, "test-model", base_url="https://other.example/v1"), Config(other, "test-model")):
            store = SessionStore(self.directory, config)
            try:
                with self.assertRaisesRegex(SessionError, "history will not be sent"):
                    store.load("test")
            finally:
                store.close()

    def test_pending_actions_marked_interrupted_never_replayed(self):
        calls = [{"id": name, "type": "function", "function": {"name": "run", "arguments": '{"argv":["touch","oops"]}'}} for name in ("one", "two")]
        self.messages.append({"role": "assistant", "content": None, "tool_calls": calls})
        self.messages.append({"role": "tool", "tool_call_id": "one", "content": "done"})
        self.store.save("interrupted", self.messages)
        restored = self.store.load("interrupted")
        self.assertEqual(restored[-1]["tool_call_id"], "two")
        self.assertIn("NOT replayed", restored[-1]["content"])
        self.assertEqual(restored[-2]["content"], "done")
        self.assertFalse((self.root / "oops").exists())

    def test_rejects_malformed_json_and_history(self):
        file = self.directory / "broken.json"
        file.write_text("{bad json")
        file.chmod(0o600)
        with self.assertRaises(SessionError):
            self.store.load("broken")
        file.write_text("[" * 50000 + "0" + "]" * 50000)
        with self.assertRaises(SessionError):
            self.store.load("broken")
        for history in ([], "bad", [{"role": "user", "content": "no system"}], self.messages + [{"role": [], "content": "bad role"}], self.messages + [{"role": {}, "content": "bad role"}], self.messages + [{"role": "system", "content": "injected"}], self.messages + [{"role": "tool", "tool_call_id": "missing", "content": "x"}]):
            with self.assertRaises(SessionError):
                validate_history(history)

    def test_atomic_failure_preserves_prior_file(self):
        self.store.save("test", self.messages)
        before = (self.directory / "test.json").read_bytes()
        with patch("sprig_agent.session.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                self.store.save("test", self.messages + [{"role": "assistant", "content": "new"}])
        self.assertEqual((self.directory / "test.json").read_bytes(), before)
        self.assertEqual(list(self.directory.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
