"""Command sandbox policy and opt-in real Linux kernel regressions."""

from contextlib import closing
from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.filesystem import Directory
from sprig_agent.settings import load_settings, public_config
from sprig_agent.tools import Tools


class SandboxSettingsTests(unittest.TestCase):
    def test_only_trusted_execution_layers_can_select_the_sandbox(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = parent / "work"
            root.mkdir()
            with Directory(parent) as directory:
                directory.write_new("config.toml", b'''
[execution]
command_sandbox = "bubblewrap"
sandbox_read_dirs = ["runtime"]

[profiles.default.execution]
command_sandbox = "bubblewrap"
''', private=True)
            settings = load_settings(parent / "config.toml")
            config, _ = settings.resolve({"workdir": str(root), "profile": "default", "model": "fixture"}, {})
            self.assertEqual(config.sandbox_read_dirs, (parent / "runtime",))
            self.assertEqual(config.binding()["command_sandbox"], "bubblewrap")
            self.assertEqual(public_config(config, {})["execution"]["command_sandbox"], "bubblewrap")
            with Directory(root) as directory:
                directory.write_new("project.toml", b'[execution]\ncommand_sandbox = "off"\n', private=True)
            with self.assertRaisesRegex(ValueError, "project configuration cannot change execution"):
                settings.with_project(root / "project.toml")
            self.assertNotIn("command_sandbox", Config(root, "fixture").binding())

    def test_unsupported_platform_refuses_before_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Config(Path(temporary), "fixture", command_sandbox="bubblewrap")
            with (closing(Tools(config, lambda _: self.fail("unexpected approval"))) as tools,
                  patch("sprig_agent.command_sandbox.sys.platform", "unsupported")):
                self.assertIn("run requires the configured Linux sandbox", Agent(config, None, tools, print)._prompt())
                result = tools.execute("run", {"argv": ["unavailable"]})
            self.assertIn("only on Linux", result["error"])


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("SPRIG_TEST_BUBBLEWRAP") == "1",
                     "requires opt-in on Linux with bubblewrap, unshare and enabled user namespaces")
class KernelSandboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "work"
        self.root.mkdir()
        self.runtime = self.parent / "runtime"
        self.runtime.mkdir()
        self.runtime_alias = self.parent / "runtime-alias"
        self.runtime_alias.symlink_to(self.runtime, target_is_directory=True)
        self.config = Config(self.root, "fixture", command_sandbox="bubblewrap",
                             sandbox_read_dirs=(self.runtime_alias,), background_enabled=True,
                             background_timeout=20)
        self.approvals = []
        self.tools = Tools(self.config, lambda text: self.approvals.append(text) or True)
        self.addCleanup(self.tools.close)

    def run_code(self, source, *args, **options):
        return self.tools.execute("run", {"argv": ["/usr/bin/python3", "-I", "-B", "-c", source, *map(str, args)],
                                          **options})

    def test_foreground_masks_private_files_and_isolates_host_network_and_binfmt(self):
        outside = self.parent / "host-only"
        outside.write_text("private fixture")
        (self.runtime / "library").write_text("runtime fixture")
        (self.root / ".git").mkdir()
        (self.root / ".git" / "config").write_text("private repository metadata")
        (self.root / ".ENV").write_text("private fixture")
        (self.root / ".sprigignore").write_text("protected.txt\n")
        (self.root / "protected.txt").write_text("private fixture")
        (self.root / "alias").symlink_to(outside)
        case = """
import errno, os, pathlib, socket, subprocess, sys

outside, runtime, alias, root = map(pathlib.Path, sys.argv[1:])
assert pathlib.Path.cwd() == root
assert not outside.exists() and not pathlib.Path('alias').exists()
assert not pathlib.Path('.git/config').exists()
for name in ['.ENV', 'protected.txt', '.sprigignore']:
    assert pathlib.Path(name).read_text() == ''
    try:
        pathlib.Path(name).write_text('bad')
    except OSError as error:
        assert error.errno in {errno.EROFS, errno.EBUSY}, error
    else:
        raise AssertionError(name)
assert (runtime / 'library').read_text() == 'runtime fixture'
assert (alias / 'library').read_text() == 'runtime fixture'
try:
    (alias / 'library').write_text('bad')
except OSError as error:
    assert error.errno == errno.EROFS, error
else:
    raise AssertionError('runtime is writable')
assert 'SPRIG_SANDBOX_TEST_ENV' not in os.environ
assert not pathlib.Path('/usr/lib/wsl/lib').exists()
assert not pathlib.Path('/mnt/c/Windows').exists()
for name in os.listdir('/proc/self/fd'):
    if int(name) > 2:
        assert not pathlib.Path('/proc/self/fd', name).is_symlink()
for family in (socket.AF_INET, socket.AF_UNIX):
    try:
        socket.socket(family)
    except PermissionError:
        pass
    else:
        raise AssertionError('unexpected socket')
left, right = socket.socketpair()
left.close()
right.close()
marker = pathlib.Path('format-probe.exe')
marker.write_bytes(b'MZ' + bytes(64))
marker.chmod(0o700)
try:
    subprocess.run(['./format-probe.exe'], check=True)
except OSError as error:
    assert error.errno == errno.ENOEXEC, error
else:
    raise AssertionError('host binfmt handler reached')
pathlib.Path('result').write_text('ok')
"""
        with patch.dict(os.environ, {"SPRIG_SANDBOX_TEST_ENV": "disposable"}):
            result = self.run_code(case, outside, self.runtime, self.runtime_alias, self.root)
        self.assertEqual(result.get("returncode"), 0, result)
        self.assertEqual((self.root / "result").read_text(), "ok")
        self.assertEqual((self.root / ".ENV").read_text(), "private fixture")
        self.assertIn("network blocked", self.approvals[0])
        self.assertIn(str(self.runtime), self.approvals[0])

    def test_owned_pipe_and_pty_use_the_same_sandbox_and_approved_input(self):
        program = """
import os, pathlib, sys
assert not pathlib.Path(sys.argv[1]).exists()
assert sys.stdin.isatty() == (sys.argv[2] == 'pty')
if sys.stdin.isatty():
    import tty
    tty.setraw(0)
print('READY', flush=True)
pathlib.Path(sys.argv[2]).write_text(sys.stdin.read(1))
"""
        outside = self.parent / "host-only"
        outside.touch()
        for mode in ("pipe", "pty"):
            started = self.run_code(program, outside, mode, background=True, stdin=mode)
            self.assertIs(started.get("started"), True, started)
            job = started["job_id"]
            deadline = time.monotonic() + 8
            while True:
                output = self.tools.execute("job", {"action": "output", "job_id": job})
                if "READY" in output.get("content", ""):
                    break
                self.assertLess(time.monotonic(), deadline, output)
                time.sleep(.02)
            written = self.tools.execute("job", {"action": "input", "job_id": job, "text": "x"})
            self.assertIs(written.get("input_complete"), True, written)
            done = self.tools.execute("job", {"action": "wait", "job_id": job, "seconds": 5})
            self.assertEqual((done["state"], done["returncode"]), ("exited", 0), done)
            self.assertEqual((self.root / mode).read_text(), "x")
        self.assertEqual(len(self.approvals), 4)

    def test_changes_during_approval_refuse_launch_and_missing_inner_exe_has_no_fallback(self):
        cwd = self.root / "subdirectory"
        cwd.mkdir()

        def replace_cwd(_):
            cwd.rename(self.root / "old")
            cwd.mkdir()
            return True

        self.tools.approve = replace_cwd
        result = self.run_code("raise AssertionError('must not execute')", cwd=cwd.name)
        self.assertIn("working directory changed", result["error"])
        self.tools.approve = lambda _: True
        outside = self.parent / "outside"
        outside.write_text("#!/bin/sh\ntouch must-not-run\n")
        outside.chmod(0o700)
        result = self.tools.execute("run", {"argv": [str(outside)]})
        self.assertNotEqual(result.get("returncode"), 0, result)
        self.assertFalse((self.root / "must-not-run").exists())

    def test_hardlink_and_nested_mount_are_refused_before_approval(self):
        outside = self.parent / "host-only"
        outside.touch()
        alias = self.root / "alias"
        os.link(outside, alias)
        result = self.run_code("raise AssertionError('must not execute')")
        self.assertIn("multiply linked", result["error"])
        alias.unlink()
        with patch("sprig_agent.command_sandbox._mount_points", return_value={self.root / "submount"}):
            result = self.run_code("raise AssertionError('must not execute')")
        self.assertIn("another mount", result["error"])
        self.assertEqual(self.approvals, [])

    def test_writable_workspace_cannot_replace_the_host_pty_bootstrap(self):
        interpreter = self.root / ".venv/bin/python"
        with patch("sprig_agent.command_sandbox.sys.executable", str(interpreter)):
            result = self.run_code("raise AssertionError('must not execute')", background=True, stdin="pty")
        self.assertIn("installation and Python import path outside", result["error"])
        self.assertEqual(self.approvals, [])

    def test_environment_override_is_never_passed_to_launcher_or_command(self):
        self.tools.close()
        config = replace(self.config, command_env=(("SPRIG_SANDBOX_TEST_ENV", "fixture-environment-value"),))
        self.tools = Tools(config, lambda _: True)
        self.addCleanup(self.tools.close)
        result = self.run_code("import os; assert 'SPRIG_SANDBOX_TEST_ENV' not in os.environ")
        self.assertEqual(result.get("returncode"), 0, result)
