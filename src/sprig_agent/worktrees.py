"""Explicit local Git worktrees; never switch, clean, merge or replay a task."""

from contextlib import ExitStack
from dataclasses import replace
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import uuid

from .filesystem import Directory
from .config import redact
from .tools import Tools

COMMIT = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
FILTER = re.compile(r"filter\.(.+)\.(smudge|process|required)\Z", re.IGNORECASE | re.DOTALL)
MAX_CONFIG = 1024 * 1024


class WorktreeError(ValueError):
    pass


class _Stopped(Exception):
    def __init__(self, result):
        self.result = result


class _GitDirectory:
    """Read-only metadata capability; never passed to model file tools."""
    def __init__(self, path, *, parent=None, name=None):
        if parent is not None:
            parent.ensure_open()
        self.path, self.fd, self.inner = Path(path), None, None
        if os.name == "nt":
            from .windows import open_file
            self.fd = open_file(self.path, directory=True, allow_git_metadata=True)
        else:
            self.inner = Directory(self.path, parent=parent.inner if parent else None, name=name)

    def ensure_open(self):
        if os.name == "nt":
            if self.fd is None:
                raise OSError("Git metadata handle is closed")
        else:
            self.inner.ensure_open()

    def __enter__(self):
        self.ensure_open()
        return self

    def __exit__(self, *_):
        if os.name == "nt":
            from .windows import close_handle
            if self.fd is not None:
                close_handle(self.fd)
                self.fd = None
        else:
            self.inner.close()

    def child(self, name):
        return _GitDirectory(self.path / name, parent=self, name=name)

    def read(self, name, limit):
        self.ensure_open()
        if os.name != "nt":
            return self.inner.read(name, limit)
        from .windows import file_descriptor
        with os.fdopen(file_descriptor(self.path / name, allow_git_metadata=True), "rb") as reader:
            return reader.read(limit + 1)


def _identity(path):
    info = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise WorktreeError("worktree management does not follow symlinks or reparse points")
    return info.st_dev, info.st_ino


class _PinnedDirectory:
    def __init__(self, path, stack, *, metadata=False):
        self.path, self.identities = path, []
        directory = stack.enter_context((_GitDirectory if metadata else Directory)(path.anchor))
        self.identities.append((Path(path.anchor), _identity(Path(path.anchor))))
        for part in path.parts[1:]:
            directory = stack.enter_context(directory.child(part))
            self.identities.append((directory.path, _identity(directory.path)))
        self.directory = directory

    def check(self):
        self.directory.ensure_open()
        if any(_identity(path) != original for path, original in self.identities):
            raise WorktreeError("worktree directory changed during approval; inspect and retry")


def _safe_path(value, base):
    value = os.fspath(value)
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise WorktreeError("worktree paths must be nonempty and contain no control characters")
    path = Path(value).expanduser()
    return Path(os.path.abspath(path if path.is_absolute() else base / path))


def parse_worktrees(output):
    """Parse Git's NUL-delimited porcelain, including paths containing newlines."""
    if not output.endswith("\0\0"):
        raise WorktreeError("incomplete Git worktree listing")
    records, current = [], {}
    for field in output.split("\0"):
        if not field:
            if current:
                if "path" not in current or ("head" not in current and not current.get("bare")) or len(records) >= 256:
                    raise WorktreeError("invalid or oversized Git worktree listing")
                records.append(current)
                current = {}
            continue
        name, _, value = field.partition(" ")
        key = {"worktree": "path", "HEAD": "head"}.get(name, name)
        if key not in {"path", "head", "branch", "bare", "detached", "locked", "prunable"} or key in current:
            raise WorktreeError("unsupported Git worktree listing")
        if key == "head" and not COMMIT.fullmatch(value):
            raise WorktreeError("invalid Git worktree commit")
        current[key] = value if key in {"path", "head", "branch"} or value else True
    if current or not records:
        raise WorktreeError("incomplete Git worktree listing")
    return records


def worktree_action(config, approve, *, path=None, ref="HEAD", cancel_event=None):
    """List, or create a detached checkout in a new explicit external directory.

    Every Git process uses the normal approved, bounded process runner. Git's
    user/system config and inherited routing variables are deliberately absent.
    Existing local includes are rejected; local checkout drivers are disabled.
    """
    if config.read_only or not config.tool_enabled("run"):
        return {"error": "worktree management requires enabled command execution and writable mode", "approved": False}
    if not isinstance(ref, str) or not 1 <= len(ref) <= 256 or ref.startswith("-") or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in ref):
        return {"error": "worktree ref must be a local Git revision of at most 256 characters", "approved": False}
    created, commands, add_attempted = False, 0, False
    target, root = None, _safe_path(config.workdir, config.workdir)
    executable = shutil.which("git")
    if executable is None:
        return {"error": "Git executable not found on the process PATH", "approved": False}
    executable = Path(executable).resolve()
    if executable.is_relative_to(root):
        return {"error": "worktree management refuses a Git executable inside the workspace", "approved": False}
    config_guards = []
    with ExitStack() as stack:
        source = _PinnedDirectory(root, stack)
        target_parent = target_pin = hooks_pin = None
        if path is not None:
            target = _safe_path(path, root)
            if any(part.lower() in {".git", ".env"} or part.lower().startswith(".env.") for part in target.parts):
                return {"error": "worktree target cannot use protected Git or environment paths", "approved": False}
            if target.is_relative_to(root) or root.is_relative_to(target):
                return {"error": "new worktree must be outside the source workspace", "approved": False}
            target_parent = _PinnedDirectory(target.parent, stack)

        def check():
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt
            source.check()
            if target_parent:
                target_parent.check()
                if not created:
                    try:
                        target_parent.directory.stat(target.name)
                    except FileNotFoundError:
                        pass
                    else:
                        raise WorktreeError("worktree target already exists; nothing was replaced")
            if target_pin is not None:
                target_pin.check()
            for directory, name, original in config_guards:
                directory.check()
                try:
                    current = directory.directory.read(name, MAX_CONFIG)
                except FileNotFoundError:
                    current = None
                if current != original:
                    raise WorktreeError("Git configuration changed during approval; inspect and retry")
            if hooks_pin is not None:
                hooks_pin.check()
                if any(hooks_pin.path.iterdir()):
                    raise WorktreeError("private hooks directory changed; command not executed")

        def confirm(description):
            nonlocal add_attempted
            check()
            answer = approve(redact("LOCAL WORKTREE MANAGEMENT; does not switch the current conversation.\n" + description, config.secrets)) is True
            if answer:
                check()
                if adding and not created:
                    add_attempted = True
            return answer

        # This empty private directory disables hooks on every Git command.
        hooks = Path(tempfile.gettempdir()).resolve() / ("sprig-worktree-hooks-" + uuid.uuid4().hex)
        if os.name == "nt":
            from .windows import private_directory
            private_directory(hooks)
        else:
            hooks.mkdir(mode=0o700)
        def remove_hooks():
            try:
                hooks.rmdir()  # Never recursively delete unexpected contents.
            except OSError:
                pass
        stack.callback(remove_hooks)
        hooks_pin = _PinnedDirectory(hooks, stack)

        command_config = replace(config, inherit_env=False, env_allowlist=(),
            command_env=(("GIT_CONFIG_NOSYSTEM", "1"),
                ("GIT_CONFIG_SYSTEM", os.devnull), ("GIT_CONFIG_GLOBAL", os.devnull),
                ("GIT_TERMINAL_PROMPT", "0"), ("GIT_OPTIONAL_LOCKS", "0"),
                ("GIT_ALLOW_PROTOCOL", ""), ("GIT_NO_LAZY_FETCH", "1")))
        tools = Tools(command_config, confirm, cancel_event)
        stack.callback(tools.close)
        base = [str(executable), "--no-pager", "-c", "core.fsmonitor=false", "-c", "submodule.recurse=false",
                "-c", "core.quotePath=false", "-c", "core.hooksPath=" + str(hooks),
                "-c", "gc.auto=0", "-c", "maintenance.auto=false",
                "-c", "core.sparseCheckout=false", "-c", "core.sparseCheckoutCone=false"]

        def run(arguments):
            nonlocal commands
            result = redact(tools.execute("run", {"argv": arguments}), config.secrets)
            if result.get("approved") is True:
                commands += 1
            if result.get("approved") is False:
                raise _Stopped({"approved": False, "error": "worktree command rejected; no remaining commands executed"})
            if ("error" in result or result.get("returncode") != 0 or result.get("timed_out")
                    or result.get("output_limited") or result.get("outcome")):
                raise _Stopped({"approved": result.get("approved", False),
                    "error": "worktree command did not complete reliably; inspect Git state before retrying",
                    "timed_out": bool(result.get("timed_out")), "output_limited": bool(result.get("output_limited"))})
            if "[REDACTED]" in result["output"] or "\ufffd" in result["output"]:
                raise _Stopped({"approved": True,
                    "error": "Git metadata was masked or could not be decoded exactly; command sequence stopped"})
            return result["output"]

        adding = False
        try:
            check()
            if target is None:
                records = parse_worktrees(run([*base, "worktree", "list", "--porcelain", "-z"]))
                return {"approved": True, "worktrees": records, "commands_approved": commands, "git_network_disabled": True}
            probe = run([*base, "rev-parse", "--path-format=absolute", "--show-toplevel",
                         "--git-common-dir", "--absolute-git-dir", "--verify", "--end-of-options", ref + "^{commit}"]).splitlines()
            if len(probe) != 4 or Path(probe[0]) != root or not COMMIT.fullmatch(probe[3]):
                raise WorktreeError("start worktree creation from the repository root with a valid local commit")
            commit = probe[3]
            for directory in dict.fromkeys((probe[1], probe[2])):
                pin = _PinnedDirectory(_safe_path(directory, root), stack, metadata=True)
                for name in ("config", "config.worktree"):
                    try:
                        original = pin.directory.read(name, MAX_CONFIG)
                    except FileNotFoundError:
                        original = None
                    if original is not None and len(original) > MAX_CONFIG:
                        raise WorktreeError("Git configuration exceeds the 1 MiB safety limit")
                    config_guards.append((pin, name, original))
            keys = run([*base, "config", "--no-includes", "--null", "--name-only", "--list"]).split("\0")
            check()
            drivers = set()
            for key in keys:
                if key.lower().startswith(("include.", "includeif.")):
                    raise WorktreeError("local Git config includes require manual review; no worktree created")
                match = FILTER.fullmatch(key)
                if match:
                    driver = match[1]
                    if len(driver) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in driver):
                        raise WorktreeError("unsupported Git content filter name")
                    drivers.add(driver)
            if len(drivers) > 32:
                raise WorktreeError("too many Git content filters; no worktree created")
            overrides = [item for driver in sorted(drivers) for item in (
                "-c", "filter." + driver + ".smudge=", "-c", "filter." + driver + ".process=",
                "-c", "filter." + driver + ".required=false")]
            adding = True
            run([*base, *overrides, "worktree", "add", "--detach", "--no-guess-remote", "--", str(target), commit])
            created = True
            target_pin = _PinnedDirectory(target, stack)
            check()
            head = run([*base, "-C", str(target), "rev-parse", "--verify", "HEAD"]).strip()
            check()
            if head != commit:
                raise WorktreeError("created worktree commit could not be verified")
            return {"approved": True, "created": True, "verified": True, "path": str(target),
                    "head": commit, "detached": True, "commands_approved": commands,
                    "hooks_disabled": True, "content_filters_disabled": True,
                    "git_network_disabled": True, "working_changes_copied": False,
                    "start_argv": ["sprig", "--workdir", str(target)]}
        except (_Stopped, ValueError, OSError) as error:
            result = error.result if isinstance(error, _Stopped) else {"error": str(error)}
            return {**result, "created": None if add_attempted else False, "verified": False,
                    "commands_approved": commands, **({"path": str(target)} if target else {}),
                    **({"outcome": "uncertain", "recovery": "inspect the target and git worktree list; no cleanup or retry was performed"} if add_attempted else {})}
