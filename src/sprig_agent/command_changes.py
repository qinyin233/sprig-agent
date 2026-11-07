"""Bounded, explicitly approved observations of files around a foreground command."""

import copy
from dataclasses import dataclass
import json
import os
import stat

from .changes import ChangeError
from .config import redact

MAX_WATCHED_FILES = 4  # At 512 KiB before and after, the full batch fits the 4 MiB journal.


@dataclass(frozen=True)
class Snapshot:
    body: bytes | None
    revision: tuple | None
    mode: int | None
    directory_chain: tuple


class CommandChanges:
    def __init__(self, tools, paths):
        self.tools = tools
        tools._require_tool("read_file")
        tools._require_tool("edit_file")
        if not isinstance(paths, list) or not 1 <= len(paths) <= MAX_WATCHED_FILES:
            raise ChangeError("backup requires one to four explicit workspace file paths")
        self.config, self.owner = tools.config, tools._conversation_version
        self.history = tools.history.state
        self.history_snapshot = copy.deepcopy(self.history)
        self.job_revision = tools.jobs.revision
        self._guard()
        self.targets = []
        seen = set()
        for path in paths:
            parts = tools._parts(path)
            if not parts or redact(path, self.config.secrets) != path:
                raise ChangeError("backup requires explicit non-sensitive file paths")
            tools.policy.check_text(path)
            key = tuple(part.casefold() for part in parts) if os.name == "nt" else parts
            if key in seen:
                raise ChangeError("backup paths must be distinct")
            seen.add(key)
            self.targets.append(("/".join(parts), tools.instructions.prepare_action(parts[:-1])))
        self.before = self._capture()

    def _guard(self):
        tools = self.tools
        tools._require_tool("run")
        tools._require_tool("read_file")
        tools._require_tool("edit_file")
        tools._check_cancelled()
        if tools.config is not self.config or tools._conversation_version != self.owner or tools.config.read_only:
            raise ChangeError("backup owner or policy changed")
        if tools._edit_batch is not None or getattr(tools, "_restoring_batch", False):
            raise ChangeError("backup is unavailable during another edit or restore")
        if tools.jobs.revision != self.job_revision or tools.jobs.unsettled():
            raise ChangeError("backup requires settled owned background jobs")
        if tools.history.state is not self.history or tools.history.state != self.history_snapshot:
            raise ChangeError("change history changed during the command backup")
        if tools.history.store is not None:
            tools.history.store.handle.ensure_open()
        tools.history._ready()

    def _capture(self, expected=None):
        tools = self.tools
        snapshots, identities = [], set()
        for index, (path, rules) in enumerate(self.targets):
            tools._check_cancelled()
            parts = tools._parts(path)
            chain = None
            if expected is not None:
                chain = expected[index].directory_chain
                tools.instructions.recheck_action(parts[:-1], rules)
            with tools._command_directory(parts[:-1], chain) as (directory, directory_chain):
                body, info = tools._snapshot(directory, parts[-1])
            text = tools._decode(body or b"")
            if any(secret in text for secret in tools.config.secrets):
                raise ChangeError("configured credentials block command backups")
            revision = tools._revision(info)
            if revision is not None:
                identity = revision[:2]
                if identity in identities:
                    raise ChangeError("backup paths refer to the same file identity")
                identities.add(identity)
            snapshots.append(Snapshot(body, revision, stat.S_IMODE(info.st_mode) & 0o777 if info else None,
                                      directory_chain))
        return snapshots

    def description(self):
        selected = [{"path": path, "before_bytes": None if snapshot.body is None else len(snapshot.body)}
                    for (path, _), snapshot in zip(self.targets, self.before)]
        storage = "private journal" if self.tools.history.store is not None else "in memory"
        return ("\nBACK UP only these workspace files before and after this foreground command: "
                + json.dumps(selected, ensure_ascii=False)
                + f"\nStore complete UTF-8 changes {storage}; older backups or redo entries may be dropped. "
                  "This is an observation, not a command sandbox. Restoring files requires separate approvals.")

    def check_before(self):
        self._guard()
        if self._capture(self.before) != self.before:
            raise ChangeError("backup files changed during approval; command was not started")
        self._guard()

    def finish(self):
        from .tools import ToolError
        try:
            self._guard()
            after = self._capture(self.before)
            observed = [(path, before.body, current.body,
                         before.mode if before.mode is not None else current.mode, current.revision)
                        for (path, _), before, current in zip(self.targets, self.before, after)
                        if before.body != current.body]
            if not observed:
                return {"recorded": 0, "status": "unchanged"}
            if self._capture(after) != after:
                raise ChangeError("backup files changed before journal update")
            self._guard()
        except (OSError, ToolError, ChangeError, ValueError, RuntimeError):
            return {"recorded": 0, "status": "unavailable"}
        try:
            identity = self.tools.history.record_command(observed)
        except (OSError, ChangeError, ValueError, RuntimeError):
            status = "uncertain" if self.tools.history.state["pending"] else "unavailable"
            return {"recorded": 0, "status": status}
        return {"recorded": len(observed), "status": "recorded", "batch_id": identity}
