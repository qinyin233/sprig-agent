"""Exercise approved status against real, independently owned Git checkouts."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from sprig_agent.config import Config
from sprig_agent.tools import Tools


@unittest.skipUnless(shutil.which("git"), "requires Git")
class GitStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.parent = Path(directory.name).resolve()
        self.repo = self.parent / "local 测试"
        self.repo.mkdir()
        self.env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        self.env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                        GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        self.git("init", "-q")
        (self.repo / "src").mkdir()
        self.put("src/example.txt", "base\n")
        self.put("other.txt", "outside scope\n")
        self.git("add", ".")
        self.git("commit", "-qm", "base")
        self.approvals = []

    def put(self, path, text):
        (self.repo / path).write_text(text, encoding="utf-8")

    def git(self, *argv, cwd=None):
        return subprocess.run([shutil.which("git"), *argv], cwd=cwd or self.repo, env=self.env,
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20).stdout

    def tools(self, root=None, **changes):
        return Tools(Config(root or self.repo, "fixture", **changes),
                     lambda text: self.approvals.append(text) or True)

    def test_shadow_index_status_does_not_load_original_config_or_modify_metadata(self):
        self.put(".gitattributes", "*.txt filter=external\n")
        self.put("src/example.txt", "staged\n")
        self.git("add", ".gitattributes", "src/example.txt")
        self.git("commit", "-qm", "attributes")
        self.put("src/example.txt", "staged again\n")
        self.git("add", "src/example.txt")
        self.put("src/example.txt", "different working copy\n")
        self.put(".env", "disposable private marker\n")
        self.put("src/new 测试.txt", "new\n")
        self.put("ignored.local", "ignored\n")
        home = self.parent / "home"
        attributes = home / ".config/git"
        attributes.mkdir(parents=True)
        (attributes / "ignore").write_text("src/new*\n")
        (self.repo / ".git/info/exclude").write_text("ignored.local\n")
        self.git("pack-refs", "--all")
        self.git("config", "filter.external.clean", "echo unexpected > unsafe-marker")
        config = self.repo / ".git/config"
        with config.open("ab") as stream:
            stream.write(b'\n[include]\n\tpath = invalid-config\n')
        (self.repo / ".git/invalid-config").write_bytes(b"invalid git configuration syntax\n")
        before = {name: (self.repo / ".git" / name).read_bytes() for name in ("config", "index", "HEAD")}
        with (patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}),
              closing(self.tools(command_sandbox="bubblewrap")) as tools):
            result = tools.execute("git_status", {})
        self.assertEqual(result.get("entries"), [
            {"path": "src/example.txt", "index": "M", "worktree": "M"},
            {"path": "src/new 测试.txt", "index": "?", "worktree": "?"},
        ], result)
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(self.approvals), 1)
        self.assertIn("read-only host command", self.approvals[0])
        self.assertFalse((self.repo / "unsafe-marker").exists())
        self.assertEqual({name: (self.repo / ".git" / name).read_bytes() for name in before}, before)

    def test_linked_worktree_with_nested_workspace_and_literal_scope(self):
        linked = self.parent / "linked"
        self.git("worktree", "add", "-q", "--detach", str(linked), "HEAD")
        (linked / "src" / "example.txt").write_text("nested dirty\n")
        (linked / "other.txt").write_text("sibling dirty\n")
        (linked / "src" / "[literal].txt").write_text("literal bracket name\n")
        with closing(self.tools(linked / "src", command_sandbox="bubblewrap")) as tools:
            result = tools.execute("git_status", {"path": "[literal].txt"})
            full = tools.execute("git_status", {})
        self.assertEqual(result.get("entries"), [{"path": "[literal].txt", "index": "?", "worktree": "?"}], result)
        self.assertEqual([row["path"] for row in full["entries"]], ["[literal].txt", "example.txt"])
        self.assertEqual(full["head"], self.git("rev-parse", "HEAD", cwd=linked).decode().strip())

    def test_disabled_rejected_and_alternate_object_paths_never_start_git(self):
        with closing(self.tools(read_only=True)) as tools:
            self.assertIs(tools.execute("git_status", {})["approved"], False)
        with closing(self.tools(disabled_tools=("run",))) as tools:
            self.assertIs(tools.execute("git_status", {})["approved"], False)
        self.assertEqual(self.approvals, [])
        with closing(self.tools()) as tools:
            tools.approve = lambda text: "yes"
            self.assertEqual(tools.execute("git_status", {}), {"approved": False})
        (self.repo / ".git/objects/info/alternates").write_text(str(self.parent / "outside"))
        with closing(self.tools()) as tools:
            result = tools.execute("git_status", {})
        self.assertIn("alternate object", result["error"])
        self.assertEqual(self.approvals, [])

    def test_small_budget_returns_bounded_complete_records_and_total(self):
        for number in range(10):
            self.put(f"src/{number}.txt", "new\n")
        with closing(self.tools(max_output=256)) as tools:
            result = tools.execute("git_status", {})
        self.assertEqual(result["total"], 10)
        self.assertTrue(result["entries"])
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=True)), 256)

    def test_model_status_refuses_stale_workspace_instructions_before_approval(self):
        with closing(self.tools()) as tools:
            tools.instructions.mark_served(tools.instructions.context())
            self.put("AGENTS.md", "Review these updated instructions first.\n")
            with tools.instructions.model_action():
                result = tools.execute("git_status", {})
        self.assertIn("error", result)
        self.assertEqual(self.approvals, [])
