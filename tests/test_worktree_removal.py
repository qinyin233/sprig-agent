"""Remove real disposable linked checkouts without losing local work."""

from dataclasses import replace
from pathlib import Path
import io
import json
import os
import shlex
import sys
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from tests.test_worktrees import command, git, init_repo
from sprig_agent.config import Config
from sprig_agent.agent import Agent
from sprig_agent.tools import Tools
from sprig_agent import cli
from sprig_agent.worktrees import worktree_action


class WorktreeRemovalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.root = self.parent / "源 repo"
        self.root.mkdir()
        self.head = init_repo(self.root)
        self.target = self.parent / "完成 worktree"
        git(self.root, "worktree", "add", "--detach", str(self.target), self.head)
        self.config = Config(self.root, "offline", max_output=65536, timeout=15)

    def remove(self, approve=lambda _: True, **options):
        return worktree_action(self.config, approve, path=self.target, remove=True, **options)

    def test_clean_detached_commit_has_a_durable_recovery_ref_and_source_changes_survive(self):
        (self.target / "base.txt").write_bytes(b"finished\n")
        git(self.target, "add", "base.txt")
        git(self.target, "-c", "commit.gpgsign=false", "commit", "-qm", "finished task")
        finished = git(self.target, "rev-parse", "HEAD")
        (self.root / "base.txt").write_bytes(b"staged\n")
        git(self.root, "add", "base.txt")
        (self.root / "base.txt").write_bytes(b"unstaged\n")
        (self.root / "local.txt").write_bytes(b"keep\n")
        before = git(self.root, "status", "--porcelain")
        approvals = []
        result = self.remove(lambda text: approvals.append(text) or True)
        self.assertTrue(result.get("removed"), result)
        self.assertTrue(result["verified"])
        self.assertFalse(self.target.exists())
        self.assertEqual(git(self.root, "rev-parse", result["recovery_ref"]), finished)
        self.assertEqual(git(self.root, "status", "--porcelain"), before)
        self.assertEqual((self.root / "base.txt").read_bytes(), b"unstaged\n")
        self.assertEqual(git(self.root, "show", ":base.txt"), "staged")
        self.assertTrue(any("REMOVE CLEAN WORKTREE" in text and str(self.target) in text for text in approvals))
        self.assertFalse(any("--force" in command(text) for text in approvals))
        restored = worktree_action(self.config, lambda _: True, path=self.target, ref=result["recovery_ref"])
        self.assertTrue(restored.get("created"), restored)
        self.assertEqual((self.target / "base.txt").read_bytes(), b"finished\n")
        self.assertEqual(git(self.target, "rev-parse", "HEAD"), finished)

    def test_ignored_local_data_is_not_removed(self):
        git(self.root, "config", "core.excludesFile", str(self.parent / "ignore"))
        (self.parent / "ignore").write_text("private.bin\n", encoding="utf-8")
        (self.target / "private.bin").write_bytes(b"local data")
        result = self.remove()
        self.assertFalse(result.get("removed"), result)
        self.assertIn("error", result)
        self.assertEqual((self.target / "private.bin").read_bytes(), b"local data")

    def test_index_flag_change_during_status_approval_cannot_hide_local_edits(self):
        def approve(text):
            if "status" in command(text):
                git(self.target, "update-index", "--assume-unchanged", "base.txt")
                (self.target / "base.txt").write_bytes(b"hidden local edit\n")
            return True
        result = self.remove(approve)
        self.assertFalse(result.get("removed"), result)
        self.assertEqual((self.target / "base.txt").read_bytes(), b"hidden local edit\n")

    def test_clean_only_filter_never_executes_during_status_or_removal(self):
        (self.target / ".gitattributes").write_text("base.txt filter=fixture\n", encoding="utf-8")
        git(self.target, "add", ".gitattributes")
        git(self.target, "-c", "commit.gpgsign=false", "commit", "-qm", "attributes")
        marker = self.parent / "filter marker"
        script = self.parent / "filter.py"
        script.write_text("from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n", encoding="utf-8")
        git(self.target, "config", "filter.fixture.clean", shlex.quote(sys.executable) + " " + shlex.quote(str(script)))
        info = (self.target / "base.txt").stat()
        os.utime(self.target / "base.txt", ns=(info.st_atime_ns, info.st_mtime_ns - 2000000000))
        result = self.remove()
        self.assertFalse(marker.exists(), result)
        self.assertTrue(result.get("removed"), result)

    def new_target(self, name):
        target = self.parent / name
        git(self.root, "worktree", "add", "--detach", str(target), self.head)
        return target

    def test_dirty_staged_untracked_and_empty_directories_survive(self):
        for kind in ("modified", "staged", "untracked", "empty"):
            with self.subTest(kind=kind):
                target = self.new_target(kind)
                if kind in {"modified", "staged"}:
                    (target / "base.txt").write_bytes(b"keep this edit\n")
                    if kind == "staged":
                        git(target, "add", "base.txt")
                elif kind == "untracked":
                    (target / "local.txt").write_bytes(b"keep local data\n")
                else:
                    (target / "empty local directory").mkdir()
                result = worktree_action(self.config, lambda _: True, path=target, remove=True)
                self.assertFalse(result.get("removed"), result)
                self.assertIn("error", result)
                self.assertTrue(target.exists())
                if kind in {"modified", "staged"}:
                    self.assertEqual((target / "base.txt").read_bytes(), b"keep this edit\n")

    def test_hidden_index_flags_and_nonregular_index_modes_are_refused(self):
        for kind in ("assume", "skip", "gitlink", "symlink"):
            with self.subTest(kind=kind):
                target = self.new_target(kind)
                if kind in {"assume", "skip"}:
                    git(target, "update-index", "--assume-unchanged" if kind == "assume" else "--skip-worktree", "base.txt")
                    (target / "base.txt").write_bytes(b"hidden\n")
                else:
                    identity = self.head if kind == "gitlink" else git(target, "rev-parse", "HEAD:base.txt")
                    git(target, "update-index", "--add", "--cacheinfo", ("160000" if kind == "gitlink" else "120000") + "," + identity + ",extra")
                result = worktree_action(self.config, lambda _: True, path=target, remove=True)
                self.assertFalse(result.get("removed"), result)
                self.assertTrue(target.exists())

    def test_same_size_and_timestamp_edit_during_final_approval_stops_removal(self):
        def approve(text):
            if "remove" in command(text):
                info = (self.target / "base.txt").stat()
                (self.target / "base.txt").write_bytes(b"edit\n")
                os.utime(self.target / "base.txt", ns=(info.st_atime_ns, info.st_mtime_ns))
            return True
        result = self.remove(approve)
        self.assertFalse(result.get("removed"), result)
        self.assertEqual((self.target / "base.txt").read_bytes(), b"edit\n")
        self.assertTrue(result["retained"])
        self.assertEqual(git(self.root, "rev-parse", result["recovery_ref"]), self.head)

    def test_branch_and_packed_branch_heads_remain_reachable(self):
        for packed in (False, True):
            with self.subTest(packed=packed):
                target = self.new_target("branch-" + str(packed))
                name = "task-" + str(packed)
                git(target, "switch", "-c", name)
                if packed:
                    git(self.root, "pack-refs", "--all")
                result = worktree_action(self.config, lambda _: True, path=target, remove=True)
                self.assertTrue(result.get("removed"), result)
                self.assertEqual(git(self.root, "rev-parse", name), self.head)
                self.assertEqual(git(self.root, "rev-parse", result["recovery_ref"]), self.head)

    def test_lock_foreign_repository_and_main_checkout_are_not_removed(self):
        git(self.root, "worktree", "lock", str(self.target))
        result = self.remove()
        self.assertFalse(result.get("removed"), result)
        foreign = self.parent / "foreign"
        foreign.mkdir()
        init_repo(foreign)
        for target in (foreign, self.root):
            with self.subTest(target=target):
                result = worktree_action(replace(self.config, workdir=self.target), lambda _: True, path=target, remove=True)
                self.assertFalse(result.get("removed"), result)
                self.assertTrue(target.exists())

    def test_each_mutating_command_requires_fresh_literal_true_approval(self):
        for denied in ("update-ref", "remove"):
            with self.subTest(denied=denied):
                target = self.new_target("denied-" + denied)
                result = worktree_action(self.config, lambda text: denied not in command(text), path=target, remove=True)
                self.assertFalse(result.get("removed"), result)
                self.assertTrue(target.exists())
                self.assertEqual(git(target, "rev-parse", "HEAD"), self.head)
        result = self.remove(lambda _: 1)
        self.assertFalse(result.get("approved"), result)
        self.assertTrue(self.target.exists())

    def test_cancel_before_removal_preserves_the_retained_commit(self):
        stop = threading.Event()
        def approve(text):
            if "remove" in command(text):
                stop.set()
            return True
        result = self.remove(approve, cancel_event=stop)
        self.assertTrue(result.get("cancelled"), result)
        self.assertFalse(result["removed"])
        self.assertTrue(self.target.exists())
        self.assertEqual(git(self.root, "rev-parse", result["recovery_ref"]), self.head)

    def test_refusing_post_removal_verification_reports_uncertainty_without_retry(self):
        lists, removals = 0, 0
        def approve(text):
            nonlocal lists, removals
            argv = command(text)
            lists += "list" in argv
            removals += "remove" in argv
            return lists < 2
        result = self.remove(approve)
        self.assertEqual(result.get("outcome"), "uncertain", result)
        self.assertIsNone(result["removed"])
        self.assertFalse(result["verified"])
        self.assertFalse(self.target.exists())
        self.assertEqual(removals, 1)
        self.assertEqual(git(self.root, "rev-parse", result["recovery_ref"]), self.head)

    def test_read_only_limits_private_storage_and_changed_config_stop_before_removal(self):
        for config in (replace(self.config, read_only=True), replace(self.config, disabled_tools=("run",)),
                       replace(self.config, command_output_limit=256), replace(self.config, config_path=self.target / "config.toml")):
            with self.subTest(read_only=config.read_only, limit=config.max_output):
                long_key = "fixture." + "k" * 400
                if config.command_output_limit == 256:
                    git(self.root, "config", long_key, "bounded")
                result = worktree_action(config, lambda _: True, path=self.target, remove=True)
                self.assertFalse(result.get("removed"), result)
                self.assertTrue(self.target.exists())
                if config.command_output_limit == 256:
                    self.assertTrue(result["output_limited"], result)
                    git(self.root, "config", "--unset", long_key)
        def approve(text):
            if "remove" in command(text):
                git(self.root, "config", "fixture.changed", "true")
            return True
        result = self.remove(approve)
        self.assertFalse(result.get("removed"), result)
        self.assertTrue(self.target.exists())

    def test_inventory_budget_is_enforced_before_any_recovery_branch_or_deletion(self):
        with patch("sprig_agent.worktree_removal.MAX_BYTES", 4):
            result = self.remove()
        self.assertFalse(result.get("removed"), result)
        self.assertNotIn("recovery_ref", result)
        self.assertTrue(self.target.exists())

    def test_deleting_recovery_reference_during_final_approval_stops_removal(self):
        def approve(text):
            if "remove" in command(text):
                reference = text.split("Keep commit in recovery branch: ", 1)[1].splitlines()[0]
                git(self.root, "update-ref", "-d", reference)
            return True
        result = self.remove(approve)
        self.assertFalse(result.get("removed"), result)
        self.assertTrue(self.target.exists())
        self.assertFalse(result["retained"])

    def test_cli_and_chat_removal_are_local_and_preserve_the_original_conversation(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(cli, "approve", return_value=True), patch.object(cli, "Provider") as provider, redirect_stdout(output), redirect_stderr(errors):
            status = cli.main(["--no-config", "--workdir", str(self.root), "--remove-worktree", str(self.target)])
        self.assertEqual(status, 0, errors.getvalue())
        self.assertTrue(json.loads(output.getvalue())["removed"])
        provider.assert_not_called()
        target = self.new_target("chat remove")
        provider, emitted = Mock(), []
        with closing(Tools(self.config, lambda _: True)) as tools:
            agent = Agent(self.config, provider, tools, emitted.append)
            history = [{"role": "system", "content": "fixture"}, {"role": "user", "content": "existing task"}, {"role": "assistant", "content": "Done"}]
            with patch("builtins.input", side_effect=["/remove-worktree " + str(target), "/exit"]):
                cli.chat(agent, None, history, emitted.append, autostart=False)
            self.assertEqual(agent.config.workdir, self.root)
            self.assertEqual(history[1]["content"], "existing task")
            self.assertEqual(len(history), 4)
            self.assertIn("prior conclusions and acceptance results are stale", history[-1]["content"])
            self.assertNotIn(str(target), json.dumps(history))
        provider.complete.assert_not_called()
        self.assertFalse(target.exists())
