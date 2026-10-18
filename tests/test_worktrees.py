"""Real disposable Git repositories and approved local worktree management."""

from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sprig_agent import cli
from sprig_agent.agent import Agent
from sprig_agent.config import Config
from sprig_agent.policy import command_environment
from sprig_agent.session import SessionError, SessionStore
from sprig_agent.tools import Tools
from sprig_agent.worktrees import WorktreeError, _GitDirectory, parse_worktrees, worktree_action


def git(root, *args):
    env = command_environment(Config(root, "offline-fixture"))
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(root), *args], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, encoding="utf-8", timeout=15)
    if result.returncode:
        raise AssertionError("fixture Git failed: " + result.stderr)
    return result.stdout.strip()


def init_repo(root):
    git(root, "init", "-q")
    git(root, "config", "user.name", "Sprig Fixture")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "base.txt").write_bytes(b"base\n")
    git(root, "add", "--", "base.txt")
    git(root, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


def command(description):
    return json.loads(description.split("argv: ", 1)[1].splitlines()[0])


class WorktreeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "源 repo"
        self.root.mkdir()
        self.head = init_repo(self.root)
        self.config = Config(self.root, "offline", max_output=65536, timeout=15)
        self.target = self.parent / "新 worktree"
        self.approve = Mock(return_value=True)

    def create(self, **kwargs):
        return worktree_action(self.config, kwargs.pop("approve", self.approve), path=self.target, **kwargs)

    def test_checkout_preserves_staged_unstaged_untracked_files_and_original_index(self):
        branch = git(self.root, "symbolic-ref", "HEAD")
        (self.root / "base.txt").write_bytes(b"staged\n")
        git(self.root, "add", "base.txt")
        (self.root / "base.txt").write_bytes(b"unstaged\n")
        (self.root / "untracked.txt").write_bytes(b"private work\n")
        index = (self.root / ".git/index").read_bytes()
        result = self.create()
        self.assertTrue(result.get("verified"), result)
        self.assertEqual(result["head"], self.head)
        self.assertEqual(self.approve.call_count, 4)
        self.assertEqual((self.target / "base.txt").read_bytes(), b"base\n")
        self.assertFalse((self.target / "untracked.txt").exists())
        self.assertEqual((self.root / "base.txt").read_bytes(), b"unstaged\n")
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(git(self.root, "symbolic-ref", "HEAD"), branch)
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), self.head)
        self.assertEqual(git(self.target, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        listing = worktree_action(self.config, self.approve)
        self.assertEqual({Path(item["path"]) for item in listing["worktrees"]}, {self.root, self.target})

    def test_agent_file_tools_use_new_root_and_old_session_binding_cannot_resume(self):
        self.assertTrue(self.create().get("verified"))
        with closing(Tools(replace(self.config, workdir=self.target), self.approve)) as tools:
            read = tools.execute("read_file", {"path": "base.txt", "limit": 5})
            result = tools.execute("edit_lines", {"path": "base.txt", "read_id": read["read_id"],
                "edits": [{"start_line": 1, "end_line": 1, "new": "isolated\n"}]})
            self.assertTrue(result.get("changed"), result)
            self.assertIn("error", tools.execute("read_file", {"path": ".git"}))
        self.assertEqual((self.root / "base.txt").read_bytes(), b"base\n")
        with closing(SessionStore(self.parent / "sessions", self.config)) as store:
            store.save("bound", [{"role": "system", "content": "fixture"}, {"role": "user", "content": "task"}])
        with closing(SessionStore(self.parent / "sessions", replace(self.config, workdir=self.target))) as store:
            with self.assertRaises(SessionError):
                store.load("bound")

    def test_existing_linked_worktree_can_start_another_committed_checkout(self):
        self.assertTrue(self.create().get("verified"))
        (self.target / "base.txt").write_bytes(b"uncommitted linked edit\n")
        another = self.parent / "another task"
        result = worktree_action(replace(self.config, workdir=self.target), self.approve, path=another)
        self.assertTrue(result.get("verified"), result)
        self.assertEqual((another / "base.txt").read_bytes(), b"base\n")
        self.assertEqual((self.target / "base.txt").read_bytes(), b"uncommitted linked edit\n")

    def test_linked_workspace_cannot_store_transcripts_in_original_or_other_repositories(self):
        self.assertTrue(self.create().get("verified"))
        unrelated = self.parent / "unrelated"
        unrelated.mkdir()
        init_repo(unrelated)
        bare = self.parent / "bare-repo"
        git(self.root, "init", "--bare", "-q", str(bare))
        for repository in (self.root, unrelated, bare):
            directory = repository / "private-sessions"
            with self.subTest(repository=repository.name):
                with self.assertRaisesRegex(SessionError, "outside"):
                    with closing(SessionStore(directory, replace(self.config, workdir=self.target))):
                        pass
                self.assertFalse(directory.exists())

    def test_ref_is_pinned_even_when_source_head_changes_during_creation_approval(self):
        def approve(description):
            argv = command(description)
            if "worktree" in argv and "add" in argv:
                (self.root / "base.txt").write_bytes(b"new source commit\n")
                git(self.root, "add", "base.txt")
                git(self.root, "-c", "commit.gpgsign=false", "commit", "-qm", "new source")
            return True
        result = self.create(approve=approve)
        self.assertTrue(result.get("verified"), result)
        self.assertEqual(result["head"], self.head)
        self.assertNotEqual(git(self.root, "rev-parse", "HEAD"), self.head)
        self.assertEqual((self.target / "base.txt").read_bytes(), b"base\n")

    def test_rejection_at_each_stage_never_replays_or_cleans_a_created_directory(self):
        for stage in range(1, 5):
            self.target = self.parent / ("decline-" + str(stage))
            answers = iter([True] * (stage - 1) + [False])
            result = self.create(approve=lambda _: next(answers))
            self.assertFalse(result["approved"])
            self.assertFalse(result["verified"])
            self.assertEqual(self.target.exists(), stage == 4)
            if stage == 4:
                self.assertEqual(result["outcome"], "uncertain")
                self.assertEqual((self.target / "base.txt").read_bytes(), b"base\n")
            else:
                self.assertFalse(result["created"])

    def test_target_and_git_configuration_are_rechecked_after_approval(self):
        for mutation in ("target", "config"):
            self.target = self.parent / mutation
            def approve(description):
                argv = command(description)
                if "worktree" in argv and "add" in argv:
                    if mutation == "target":
                        self.target.mkdir()
                        (self.target / "keep.txt").write_text("user change")
                    else:
                        git(self.root, "config", "filter.new.smudge", "untrusted-filter")
                return True
            result = self.create(approve=approve)
            self.assertIn("error", result)
            self.assertFalse(result["created"])
            self.assertFalse(result["verified"])
            if mutation == "target":
                self.assertEqual((self.target / "keep.txt").read_text(), "user change")
            else:
                self.assertFalse(self.target.exists())

    def test_hooks_and_required_content_filters_do_not_run_during_checkout(self):
        marker = self.parent / "executed.txt"
        helper = self.parent / "filter.py"
        helper.write_text("from pathlib import Path\nimport sys\nPath(" + repr(str(marker))
                          + ").write_text('executed')\nsys.stdout.write(sys.stdin.read())\n", encoding="utf-8")
        (self.root / ".gitattributes").write_text("base.txt filter=tripwire\n")
        git(self.root, "add", ".gitattributes")
        git(self.root, "-c", "commit.gpgsign=false", "commit", "-qm", "attributes")
        git(self.root, "config", "filter.tripwire.smudge", shlex.join([Path(sys.executable).as_posix(), helper.as_posix()]))
        git(self.root, "config", "filter.tripwire.process", shlex.join([Path(sys.executable).as_posix(), helper.as_posix()]))
        git(self.root, "config", "filter.tripwire.required", "true")
        hook = self.root / ".git/hooks/post-checkout"
        hook.write_text("#!/bin/sh\n" + shlex.join([Path(sys.executable).as_posix(), helper.as_posix()]) + "\n", encoding="utf-8")
        hook.chmod(0o755)
        result = self.create()
        self.assertTrue(result.get("verified"), result)
        self.assertFalse(marker.exists())
        self.assertEqual((self.target / "base.txt").read_bytes(), b"base\n")

    def test_local_config_includes_are_refused_before_checkout(self):
        include = self.parent / "included.conf"
        include.write_text("[filter \"untrusted\"]\nsmudge = never-run-this\n")
        git(self.root, "config", "include.path", include.as_posix())
        result = self.create()
        self.assertIn("includes", result["error"])
        self.assertFalse(result["created"])
        self.assertFalse(self.target.exists())

    def test_masked_filter_name_is_not_used_as_an_authoritative_override(self):
        (self.root / ".gitattributes").write_text("base.txt filter=development\n")
        git(self.root, "add", ".gitattributes")
        git(self.root, "-c", "commit.gpgsign=false", "commit", "-qm", "attributes")
        git(self.root, "config", "filter.development.smudge", "never-run-this")
        self.config = replace(self.config, command_env=(("BUILD_MODE", "development"),))
        result = self.create()
        self.assertIn("masked", result["error"])
        self.assertFalse(result["created"])
        self.assertFalse(self.target.exists())

    def test_invalid_utf8_metadata_never_reaches_checkout(self):
        with (self.root / ".git/config").open("ab") as writer:
            writer.write(b'\n[filter "bad\xffname"]\nsmudge = never-run-this\n')
        result = self.create()
        self.assertIn("error", result)
        self.assertFalse(result["created"])
        self.assertFalse(self.target.exists())

    def test_missing_promisor_blob_does_not_fetch_or_report_a_complete_checkout(self):
        marker = self.parent / "remote-executed.txt"
        helper = self.parent / "remote.py"
        helper.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('remote')\n", encoding="utf-8")
        blob = git(self.root, "rev-parse", "HEAD:base.txt")
        object_path = self.root / ".git/objects" / blob[:2] / blob[2:]
        object_path.chmod(0o600)
        object_path.unlink()
        git(self.root, "config", "remote.origin.url", "ext::" + shlex.join([Path(sys.executable).as_posix(), helper.as_posix()]))
        git(self.root, "config", "remote.origin.promisor", "true")
        git(self.root, "config", "extensions.partialClone", "origin")
        git(self.root, "config", "protocol.ext.allow", "always")
        result = self.create()
        self.assertIn("error", result)
        self.assertFalse(result["verified"])
        self.assertEqual(result["outcome"], "uncertain")
        self.assertFalse(marker.exists())

    @unittest.skipUnless(os.name == "nt", "native Windows metadata handle capability")
    def test_windows_metadata_capability_cannot_write_or_open_protected_env(self):
        from sprig_agent import windows
        config = self.root / ".git/config"
        with self.assertRaises(ValueError):
            windows.open_file(config)
        handle = windows.open_file(config, allow_git_metadata=True)
        windows.close_handle(handle)
        original = config.read_bytes()
        descriptor = windows.file_descriptor(config, allow_git_metadata=True)
        try:
            with self.assertRaises(OSError):
                os.write(descriptor, b"forbidden metadata write")
        finally:
            os.close(descriptor)
        for kwargs in ({"create": True}, {"exclusive": True}):
            with self.assertRaisesRegex(ValueError, "read-only"):
                windows.open_file(config, allow_git_metadata=True, **kwargs)
            with self.assertRaisesRegex(ValueError, "read-only"):
                windows.file_descriptor(config, allow_git_metadata=True, **kwargs)
        protected = self.root / ".git/.env"
        protected.write_text("private-fixture")
        with self.assertRaises(ValueError):
            windows.open_file(protected, allow_git_metadata=True)
        self.assertEqual(config.read_bytes(), original)

    def test_creation_ignores_inherited_git_routing_and_trusted_execution_overrides(self):
        self.config = replace(self.config, inherit_env=True, env_allowlist=("GIT_DIR",),
                              command_env=(("GIT_WORK_TREE", str(self.parent / "wrong")),))
        with patch.dict(os.environ, {"GIT_DIR": str(self.parent / "missing-git"), "GIT_CONFIG_COUNT": "1",
                                    "GIT_CONFIG_KEY_0": "core.bare", "GIT_CONFIG_VALUE_0": "true"}):
            result = self.create()
        self.assertTrue(result.get("verified"), result)
        self.assertEqual((self.target / "base.txt").read_bytes(), b"base\n")

    def test_read_only_disabled_commands_and_cancellation_do_not_create_a_worktree(self):
        for config in (replace(self.config, read_only=True), replace(self.config, disabled_tools=("run",))):
            result = worktree_action(config, self.approve, path=self.target)
            self.assertIn("error", result)
        self.approve.assert_not_called()
        event = threading.Event()
        event.set()
        with self.assertRaises(KeyboardInterrupt):
            self.create(cancel_event=event)
        self.approve.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_nested_existing_and_protected_targets_are_never_overwritten(self):
        for path in (self.root / "nested", self.root, self.parent, self.parent / ".env", self.parent / ".git"):
            result = worktree_action(self.config, self.approve, path=path)
            self.assertIn("error", result)
        self.target.mkdir()
        (self.target / "keep").write_bytes(b"keep")
        self.assertIn("error", self.create())
        self.assertEqual((self.target / "keep").read_bytes(), b"keep")
        self.approve.assert_not_called()

    def test_subdirectory_and_invalid_revisions_stop_before_creation(self):
        child = self.root / "subdir"
        child.mkdir()
        result = worktree_action(replace(self.config, workdir=child), self.approve, path=self.target)
        self.assertIn("error", result)
        self.assertFalse(self.target.exists())
        for ref in ("missing-ref", "--help", "HEAD\nignored"):
            self.assertIn("error", self.create(ref=ref))
        self.assertFalse(self.target.exists())

    def test_small_command_output_budget_stops_before_checkout(self):
        longer = self.root.with_name("source-" + "x" * 70)
        self.root.rename(longer)
        self.root = longer
        self.config = replace(self.config, workdir=longer, max_output=256)
        result = self.create()
        self.assertIn("error", result)
        self.assertTrue(result["output_limited"])
        self.assertFalse(result["created"])
        self.assertFalse(self.target.exists())
        self.assertEqual(self.approve.call_count, 1)

    def test_git_binary_inside_workspace_is_rejected_before_any_process(self):
        executable = self.root / "git.exe"
        executable.write_bytes(b"not-a-real-git")
        with patch("sprig_agent.worktrees.shutil.which", return_value=str(executable)):
            result = self.create()
        self.assertIn("executable inside", result["error"])
        self.approve.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_closed_metadata_reader_cannot_read_or_open_children(self):
        with _GitDirectory(self.root / ".git") as directory:
            self.assertTrue(directory.read("config", 1024))
        for action in (lambda: directory.read("config", 1024), lambda: directory.child("objects")):
            with self.assertRaises(OSError):
                action()

    def test_chat_worktree_listing_stays_local_and_retains_original_context(self):
        provider, emitted = Mock(), []
        with closing(Tools(self.config, self.approve)) as tools:
            agent = Agent(self.config, provider, tools, emitted.append)
            history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "existing task"},
                       {"role": "assistant", "content": "Done"}]
            with patch("builtins.input", side_effect=["/worktrees", "/exit"]):
                self.assertEqual(cli.chat(agent, None, history, emitted.append, autostart=False), 2)
            self.assertEqual(agent.config.workdir, self.root)
            self.assertIn("existing task", history[1]["content"])
            self.assertFalse(any("worktrees" in item.get("content", "") for item in history))
        provider.complete.assert_not_called()
        self.assertEqual(self.approve.call_count, 1)

    def test_cli_local_management_never_initializes_a_provider_and_cannot_mix_tasks(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, "approve", return_value=True), patch.object(cli, "Provider") as provider, \
                redirect_stdout(output), redirect_stderr(errors):
            status = cli.main(["--no-config", "--workdir", str(self.root), "--create-worktree", str(self.target)])
        self.assertEqual(status, 0, errors.getvalue())
        self.assertTrue(json.loads(output.getvalue())["verified"])
        provider.assert_not_called()
        for args in (["--list-worktrees", "Task"], ["--worktree-ref", "HEAD"],
                     ["--create-worktree", str(self.parent / "unused"), "--file", "base.txt"]):
            with patch.object(cli, "Provider") as provider, redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["--no-config", "--workdir", str(self.root), *args]), 1)
            provider.assert_not_called()

    def test_nul_listing_preserves_spaces_newlines_and_marks_missing_or_locked_worktrees(self):
        value = "worktree /repo\0HEAD " + self.head + "\0branch refs/heads/main\0\0" + (
            "worktree /path with\nnewline\0HEAD " + self.head + "\0detached\0locked keep it\0prunable missing\0\0")
        records = parse_worktrees(value)
        self.assertEqual(records[1]["path"], "/path with\nnewline")
        self.assertEqual(records[1]["locked"], "keep it")
        self.assertTrue(records[1]["detached"])
        for invalid in ("", "worktree /repo\0", "worktree /a\0worktree /b\0\0", "worktree /x\0HEAD invalid\0\0"):
            with self.assertRaises(WorktreeError):
                parse_worktrees(invalid)


if __name__ == "__main__":
    unittest.main()
