"""Native Windows behavior: real handles, ACLs, junctions and descendant cleanup."""

import json
import os
from pathlib import Path
import subprocess
import stat
import sys
import tempfile
import time
import unittest
import venv
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent.config import Config
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.tools import Tools


@unittest.skipUnless(os.name == "nt", "native Windows APIs")
class WindowsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name)
        self.root = self.parent / "workspace"
        self.root.mkdir()
        self.config = Config(self.root, "test", timeout=2, max_output=1024)
        self.tools = Tools(self.config, lambda _: True)
        self.addCleanup(self.tools.close)

    def test_ambiguous_windows_paths_and_case_insensitive_secrets(self):
        (self.root / ".ENV").write_text("hidden")
        (self.root / ".GiT").mkdir()
        (self.root / ".GiT/config").write_text("hidden")
        for path in ("C:/escape", "//server/share", "a:stream", "NUL", "CON.txt", "COM1", "LPT¹.txt",
                     "CONIN$", "a.", "a ", "a/.ENV", ".ENV", ".GiT/config", "a/../../escape"):
            with self.subTest(path=path):
                self.assertIn("error", self.tools.execute("read_file", {"path": path}))
                self.assertIn("error", self.tools.execute("edit_file", {"path": path, "old": "", "new": "bad"}))
        self.assertEqual(self.tools.execute("list_files", {})["entries"], [])
        self.assertEqual(self.tools.execute("search", {"query": "hidden"})["matches"], [])

    def test_unicode_paths_and_crlf_are_preserved(self):
        path = "测试 🌱.txt"
        result = self.tools.execute("edit_file", {"path": path, "old": "", "new": "你好\r\n世界\r\n"})
        self.assertTrue(result["changed"])
        self.assertEqual(self.tools.execute("read_file", {"path": path})["content"], "你好\r\n世界\r\n")
        self.assertEqual(self.tools.execute("search", {"query": "世界"})["matches"][0]["line"], 2)

    def test_junction_cannot_read_write_search_or_run_outside(self):
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "a").write_text("secret")
        link = self.root / "link"
        # mklink is a cmd builtin; paths come only from this controlled fixture.
        subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        try:
            for name, args in (("read_file", {"path": "link/a"}),
                               ("edit_file", {"path": "link/a", "old": "secret", "new": "bad"}),
                               ("run", {"argv": [sys.executable, "-V"], "cwd": "link"})):
                self.assertIn("error", self.tools.execute(name, args))
            self.assertEqual(self.tools.execute("list_files", {})["entries"], [])
            self.assertEqual(self.tools.execute("search", {"query": "secret"})["matches"], [])
            self.assertEqual((outside / "a").read_text(), "secret")
        finally:
            # Remove only the junction itself, never its target contents.
            link.rmdir()

    def test_parent_is_pinned_while_approval_is_pending(self):
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "a").write_text("old")
        def swap(_):
            sub.rename(self.root / "moved")
            return True
        self.tools.approve = swap
        self.assertIn("error", self.tools.execute("edit_file", {"path": "sub/a", "old": "old", "new": "new"}))
        self.assertEqual((sub / "a").read_text(), "old")

    def test_concurrent_content_change_requires_fresh_approval(self):
        file = self.root / "a"
        file.write_text("old")
        def change(_):
            file.write_text("other editor")
            return True
        self.tools.approve = change
        self.assertIn("error", self.tools.execute("edit_file", {"path": "a", "old": "old", "new": "new"}))
        self.assertEqual(file.read_text(), "other editor")

    def test_edit_preserves_existing_private_acl(self):
        from sprig_agent.windows import file_descriptor, check_private, open_file, close_handle
        with os.fdopen(file_descriptor(self.root / "private.txt", create=True, private=True), "wb") as file:
            file.write(b"old")
        self.assertTrue(self.tools.execute("edit_file", {"path": "private.txt", "old": "old", "new": "new"})["changed"])
        handle = open_file(self.root / "private.txt")
        try:
            check_private(handle)
        finally:
            close_handle(handle)

    def test_replacement_security_descriptor_includes_owner(self):
        import ctypes
        from ctypes import wintypes
        from sprig_agent.windows import api, security, existing_attributes, file_descriptor, checked
        path = self.root / "owned.txt"
        with os.fdopen(file_descriptor(path, create=True, private=True), "wb") as file:
            file.write(b"old")
        get_owner = api(security, "GetSecurityDescriptorOwner", wintypes.BOOL,
                        ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL))
        with existing_attributes(path) as attributes:
            owner, defaulted = ctypes.c_void_p(), wintypes.BOOL()
            checked(get_owner(attributes.descriptor, ctypes.byref(owner), ctypes.byref(defaulted)))
            self.assertTrue(owner.value, "replacement must preserve the original owner")

    def test_batch_file_cannot_invoke_an_implicit_shell(self):
        batch = self.root / "write.cmd"
        batch.write_text("@echo off\necho bad>pwned\n")
        result = self.tools.execute("run", {"argv": [str(batch)]})
        self.assertIn("explicit cmd.exe", result["error"])
        self.assertFalse((self.root / "pwned").exists())

    def test_timeout_kills_descendants_before_delayed_side_effect(self):
        child = "import time; from pathlib import Path; time.sleep(0.6); Path('pwned').write_text('bad')"
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); print('spawned',flush=True); time.sleep(30)"
        result = self.tools.execute("run", {"argv": [sys.executable, "-c", parent], "timeout": 0.2})
        self.assertTrue(result["timed_out"])
        self.assertIn("spawned", result["output"])
        time.sleep(0.7)
        self.assertFalse((self.root / "pwned").exists())

    def test_app_alias_and_alias_backed_venv_are_rejected_before_launch(self):
        from sprig_agent import windows
        environment = self.root / "fake-env"
        scripts = environment / "Scripts"
        scripts.mkdir(parents=True)
        executable = scripts / "python.exe"
        executable.write_bytes(b"placeholder")
        home = self.root / "alias-home"
        home.mkdir()
        alias = home / "python.exe"
        alias.write_bytes(b"placeholder")
        (environment / "pyvenv.cfg").write_text(f"home = {home}\n", encoding="utf-8")
        real_lstat = os.lstat

        for blocked, command in ((alias, alias), (alias, executable)):
            with self.subTest(command=command):
                def fake_lstat(path, *args, **kwargs):
                    if Path(path) == blocked:
                        return SimpleNamespace(st_reparse_tag=stat.IO_REPARSE_TAG_APPEXECLINK)
                    return real_lstat(path, *args, **kwargs)
                with patch.object(windows.os, "lstat", side_effect=fake_lstat), \
                        patch.object(windows, "api", side_effect=AssertionError("must not launch")):
                    result = self.tools.execute("run", {"argv": [str(command), "-c", "print(42)"]})
                self.assertIn("uv sync --managed-python", result["error"])

    def test_relative_executable_uses_approved_working_directory(self):
        venv.EnvBuilder(with_pip=False).create(self.root / "local-env")
        result = self.tools.execute("run", {"argv": ["./local-env/Scripts/python.exe", "-c", "print('SPRIG_OK')"]})
        self.assertEqual(result["returncode"], 0, result)
        self.assertIn("SPRIG_OK", result["output"])

    def test_access_time_update_during_review_does_not_reject_valid_edit(self):
        file = self.root / "review.txt"
        file.write_text("old", encoding="utf-8")

        def review(_):
            info = file.stat()
            os.utime(file, ns=(info.st_atime_ns + 10_000_000_000, info.st_mtime_ns))
            return True

        self.tools.approve = review
        result = self.tools.execute("edit_file", {"path": "review.txt", "old": "old", "new": "new"})
        self.assertTrue(result.get("changed"), result)
        self.assertEqual(file.read_text(), "new")

    def test_interrupt_also_kills_job_and_releases_pipes(self):
        from sprig_agent import windows
        child = "import time; from pathlib import Path; time.sleep(0.5); Path('pwned').write_text('bad')"
        with patch.object(windows.time, "sleep", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.tools.execute("run", {"argv": [sys.executable, "-c", child]})
        time.sleep(0.6)
        self.assertFalse((self.root / "pwned").exists())
        self.assertEqual(self.tools.execute("run", {"argv": [sys.executable, "-c", "print(42)"]})["returncode"], 0)

    def test_private_sessions_roundtrip_and_binding(self):
        store = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(store.close)
        history = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
        store.save("demo", history)
        self.assertEqual(store.load("demo"), history)
        other = SessionStore(self.parent / "sessions", Config(self.root, "other"))
        try:
            with self.assertRaisesRegex(SessionError, "history will not be sent"):
                other.load("demo")
        finally:
            other.close()
        with patch("sprig_agent.session.os.replace", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                store.save("demo", history + [{"role": "assistant", "content": "new"}])
        self.assertEqual(store.load("demo"), history)

    def test_session_acl_granting_everyone_is_rejected(self):
        store = SessionStore(self.parent / "sessions", self.config)
        self.addCleanup(store.close)
        store.save("demo", [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}])
        file = self.parent / "sessions/demo.json"
        before = file.read_bytes()
        subprocess.run(["icacls.exe", str(file), "/grant", "*S-1-1-0:(R)"], check=True, capture_output=True)
        with self.assertRaises(SessionError):
            store.load("demo")
        with self.assertRaises(SessionError):
            store.save("demo", [])
        self.assertEqual(file.read_bytes(), before)

    def test_inherited_directory_acl_is_not_treated_as_private(self):
        directory = self.parent / "inherited"
        directory.mkdir()
        with self.assertRaisesRegex(SessionError, "owner-only"):
            SessionStore(directory, self.config)


if __name__ == "__main__":
    unittest.main()
