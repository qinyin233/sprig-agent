"""Bounded, no-follow freshness checks for explicit clean-worktree removal."""

from contextlib import ExitStack
import hashlib
import os
from pathlib import PurePosixPath
import stat
import time

from .worktrees import COMMIT, WorktreeError, _GitDirectory, _PinnedDirectory, _safe_path

MAX_ENTRIES = 20000
MAX_BYTES = 128 * 1024 * 1024
MAX_METADATA = 16 * 1024 * 1024


def clean_index(output):
    if output and not output.endswith("\0"):
        raise WorktreeError("incomplete worktree index listing")
    paths = set()
    for row in output.split("\0")[:-1]:
        header, separator, path = row.partition("\t")
        fields = header.split(" ")
        parts = PurePosixPath(path).parts
        if (not separator or len(fields) != 4 or fields[0] != "H" or fields[1] not in {"100644", "100755"}
                or not COMMIT.fullmatch(fields[2]) or fields[3] != "0"):
            raise WorktreeError("removal refuses submodules, symbolic links, conflicts, skip-worktree or assume-unchanged entries")
        if (not parts or PurePosixPath(path).is_absolute() or str(PurePosixPath(path)) != path
                or any(part in {".", ".."} or "\\" in part or ":" in part for part in parts)
                or any(part.lower() in {".git", ".env"} or part.lower().startswith(".env.") for part in parts)
                or path in paths or len(paths) >= MAX_ENTRIES):
            raise WorktreeError("unsupported or oversized worktree index; nothing removed")
        paths.add(path)
    return paths


class RemovalGuard:
    def __init__(self, target, common, gitdir, record, paths, stack, check_cancelled):
        self.target, self.common, self.gitdir = target, common, gitdir
        self.paths, self.check_cancelled = paths, check_cancelled
        self.root = _PinnedDirectory(target, stack)
        self.metadata = _PinnedDirectory(gitdir, stack, metadata=True)
        self.files = [(target, ".git")]
        self.files += [(gitdir, name) for name in ("HEAD", "index", "config.worktree", "commondir", "gitdir", "HEAD.lock", "index.lock", "locked")]
        self.files += [(common, "packed-refs")]
        if record.get("branch"):
            branch = record["branch"]
            parts = PurePosixPath(branch).parts
            if (not branch.startswith("refs/heads/") or str(PurePosixPath(branch)) != branch
                    or any(part in {".", ".."} or "\\" in part or ":" in part for part in parts)):
                raise WorktreeError("unsupported worktree branch reference")
            self.files += [(common, branch), (common, branch + ".lock")]
        self.initial = self.snapshot()
        metadata = self.initial[1]
        marker = metadata[target, ".git"]
        head = metadata[gitdir, "HEAD"]
        try:
            marker = marker.decode("utf-8").removesuffix("\n").removesuffix("\r")
            if not marker.startswith("gitdir: ") or _safe_path(marker[8:], target) != gitdir:
                raise ValueError
            common_marker = metadata[gitdir, "commondir"].decode("utf-8").removesuffix("\n").removesuffix("\r")
            back_marker = metadata[gitdir, "gitdir"].decode("utf-8").removesuffix("\n").removesuffix("\r")
            if _safe_path(common_marker, gitdir) != common or _safe_path(back_marker, gitdir) != target / ".git":
                raise ValueError
            if record.get("branch"):
                branch = record["branch"]
                if head != ("ref: " + branch + "\n").encode("utf-8"):
                    raise ValueError
                loose = metadata[common, branch]
                if loose is None:
                    packed = metadata[common, "packed-refs"] or b""
                    matches = [line.split(b" ", 1)[0] for line in packed.splitlines() if line.endswith(b" " + branch.encode("utf-8"))]
                    if len(matches) != 1:
                        raise ValueError
                    loose = matches[0]
                if loose.strip().decode("ascii") != record["head"]:
                    raise ValueError
            elif head is None or head.strip().decode("ascii") != record["head"]:
                raise ValueError
        except (AttributeError, UnicodeError, ValueError):
            raise WorktreeError("worktree registration or HEAD changed during inspection") from None
        if any(value is not None for (directory, name), value in self.initial[1].items()
               if name.endswith(".lock") or name == "locked"):
            raise WorktreeError("worktree metadata is locked; nothing removed")

    def snapshot(self):
        deadline = time.monotonic() + 15
        size, visited, found = 0, 0, {}
        directories = {"/".join(path.split("/")[:i]) for path in self.paths for i in range(1, len(path.split("/")))}

        def check():
            self.check_cancelled()
            if time.monotonic() > deadline:
                raise WorktreeError("worktree freshness check exceeded its 15 second limit")

        def walk(directory, prefix="", depth=0):
            nonlocal size, visited
            check()
            if depth > 64:
                raise WorktreeError("worktree directory depth exceeds the safety limit")
            with directory.scandir() as entries:
                for entry in entries:
                    check()
                    visited += 1
                    if visited > MAX_ENTRIES:
                        raise WorktreeError("worktree inventory exceeds the safety limit")
                    if not prefix and entry.name == ".git":
                        continue
                    path = prefix + entry.name
                    info = directory.stat(entry.name)
                    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                        raise WorktreeError("worktree removal refuses links and reparse points")
                    if stat.S_ISDIR(info.st_mode):
                        if path not in directories:
                            raise WorktreeError("worktree contains an untracked or empty directory; nothing removed")
                        found[path + "/"] = (info.st_dev, info.st_ino)
                        with directory.child(entry.name) as child:
                            walk(child, path + "/", depth + 1)
                    else:
                        if path not in self.paths or not stat.S_ISREG(info.st_mode):
                            raise WorktreeError("worktree contains untracked or unsupported files; nothing removed")
                        digest = hashlib.sha256()
                        with directory.open_read(entry.name) as reader:
                            while chunk := reader.read(65536):
                                check()
                                size += len(chunk)
                                if size > MAX_BYTES:
                                    raise WorktreeError("worktree inventory exceeds the 128 MiB safety limit")
                                digest.update(chunk)
                            after = os.fstat(reader.fileno())
                        identity = lambda row: (row.st_dev, row.st_ino, row.st_mode, row.st_size, row.st_mtime_ns)
                        if identity(info) != identity(after):
                            raise WorktreeError("worktree file changed during inspection")
                        found[path] = (*identity(after), digest.hexdigest())

        self.root.check()
        self.metadata.check()
        walk(self.root.directory)
        if {path for path in found if not path.endswith("/")} != self.paths:
            raise WorktreeError("tracked worktree files are missing; nothing removed")
        metadata, total = {}, 0
        for base, name in self.files:
            check()
            with ExitStack() as handles:
                directory = handles.enter_context(_GitDirectory(base))
                parts = PurePosixPath(name).parts
                try:
                    for part in parts[:-1]:
                        directory = handles.enter_context(directory.child(part))
                    value = directory.read(parts[-1], MAX_METADATA)
                except FileNotFoundError:
                    value = None
            if value is not None:
                total += len(value)
                if len(value) > MAX_METADATA or total > 4 * MAX_METADATA:
                    raise WorktreeError("worktree metadata exceeds its safety limit")
            metadata[base, name] = value
        return found, metadata

    def check(self):
        if self.snapshot() != self.initial:
            raise WorktreeError("worktree files, index or references changed during approval; inspect and retry")
