"""Preflight independent edits; individual approvals and backups remain authoritative."""

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import PurePosixPath


def path_key(parts):
    return tuple(part.casefold() if os.name == "nt" else part for part in parts)


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


@dataclass(frozen=True)
class EditPreview:
    path: str
    parts: tuple
    revision: tuple | None
    before: str | None
    after: str
    rules: dict | None


class EditBatch:
    def __init__(self, previews):
        self.pending = {path_key(item.parts): item for item in previews}
        self.failed = False
        self.completed = 0

    def stopped_result(self):
        return {"error": "Edit batch stopped after an error, refusal or uncertain outcome; "
                         "inspect earlier tool results before proposing further edits.",
                "skipped": True, "changed": False, "batch_writes_completed": self.completed}

    def check_source(self, tools, parts, raw, info):
        expected = self.pending.get(path_key(parts))
        if (expected is None or expected.revision != tools._revision(info)
                or expected.before != digest(raw)):
            raise ValueError("file changed after edit batch preflight; request a fresh batch")

    def check_proposal(self, parts, after):
        if self.pending[path_key(parts)].after != digest(after):
            raise ValueError("proposal changed after edit batch preflight; request a fresh batch")

    def check_pending(self, tools):
        # Reopen and revalidate every remaining target after EACH approval. This
        # catches changes in a later file before committing the current file.
        # External writers can still race; this is not a cross-file transaction.
        for item in self.pending.values():
            tools._check_cancelled()
            tools._parts(item.path)
            tools.instructions.recheck_action(item.parts[:-1], item.rules)
            with tools._parent(item.path) as (fd, name):
                raw, info = tools._snapshot(fd, name)
                tools._decode(raw or b"")
                if item.revision != tools._revision(info) or item.before != digest(raw):
                    raise ValueError("file changed after edit batch preflight; request a fresh batch")

    def finish(self, arguments, result):
        if ("error" in result or result.get("approved") is False
                or any(result.get(flag) for flag in ("outcome", "skipped", "cancelled", "interrupted"))):
            self.failed = True
        else:
            self.pending.pop(path_key(PurePosixPath(arguments["path"]).parts), None)
            if result.get("approved") is True and result.get("changed") is True:
                self.completed += 1


@contextmanager
def preflight(tools, requests):
    """Yield per-call failures or None; requests contain (name, arguments, error)."""
    if len(requests) > 16:
        raise ValueError("edit batch exceeds 16 tool calls")
    if len(requests) < 2 or any(name not in {"edit_file", "edit_lines"} for name, _, _ in requests):
        yield None
        return
    # Same-file chains may depend on preceding edits. Leave them sequential,
    # as well as mixed read/command/edit responses; never simulate a virtual FS.
    keys = []
    for _, arguments, _ in requests:
        if isinstance(arguments, dict) and isinstance(arguments.get("path"), str):
            keys.append(path_key(PurePosixPath(arguments["path"]).parts))
    if len(keys) != len(set(keys)):
        yield None
        return
    if tools._edit_preview or tools._edit_batch is not None:
        raise RuntimeError("nested edit batch")
    previews, failure = [], None
    tools._edit_preview = True
    try:
        with tools.instructions.model_action():
            for index, (name, arguments, error) in enumerate(requests):
                tools._check_cancelled()
                result = error if error is not None else tools.execute(name, arguments)
                if not isinstance(result, EditPreview):
                    failure = index, result
                    break
                previews.append(result)
    finally:
        tools._edit_preview = False
    if failure is not None:
        index, result = failure
        skipped = {"error": f"Edit batch preflight failed at call {index + 1}; no batch writes executed. "
                            "Read the failure and propose a corrected batch.",
                   "skipped": True, "changed": False, "batch_writes_completed": 0}
        yield [dict(result, batch_preflight="failed", batch_writes_completed=0)
               if offset == index else dict(skipped) for offset in range(len(requests))]
        return
    tools._edit_batch = EditBatch(previews)
    try:
        yield None
    finally:
        tools._edit_batch = None
