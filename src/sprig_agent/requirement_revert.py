"""Guarded inverses of applied, explicitly scoped file backups."""

import copy
from dataclasses import dataclass
import json
import os
from pathlib import PurePosixPath
import uuid

from .batch_restore import BatchRestoreResult
from .change_review import comparison
from .changes import ChangeError, RECORD_ID, reverted_ids, unpack
from .config import redact, terminal_text

ACTION = "requirement-revert"


def _path_key(path):
    parts = PurePosixPath(path).parts
    return tuple(part.casefold() for part in parts) if os.name == "nt" else parts


@dataclass(frozen=True)
class RevertFile:
    path: str
    parts: tuple
    chain: tuple
    rules: object
    current: bytes | None
    target: bytes | None
    revision: tuple | None
    mode: int
    records: tuple

    def check(self, tools, parent, name):
        if tools._parts(self.path) != self.parts:
            raise ChangeError("path policy changed during requirement revert")
        tools.instructions.recheck_action(self.parts[:-1], self.rules)
        raw, info = tools._snapshot(parent, name)
        if raw != self.current or tools._revision(info) != self.revision:
            raise ChangeError("file changed outside the recorded edits: " + json.dumps(self.path))
        return info


def _plan(tools, state, scopes, revision, identity):
    if type(revision) is not int or not 1 <= revision <= 5000:
        raise ValueError("choose a recorded requirement revision")
    if identity is not None and (not isinstance(identity, str) or not RECORD_ID.fullmatch(identity)):
        raise ValueError("choose a complete change ID from the requirement preview")
    records = state["records"]
    if state["cursor"] != len(records):
        raise ChangeError("redo entries are pending; requirement revert would discard them")
    reverted = reverted_ids(state)
    selected = set()
    for record in records:
        scope = record.get("scope", {})
        owner = scopes.get(scope.get("id"))
        if owner and scope == owner[0] and scope["revision"] == revision and record["id"] not in reverted:
            selected.add(record["id"])
    if not selected or identity is not None and identity not in selected:
        raise ChangeError("no matching applied backup for that requirement revision and change ID")

    by_path = {}
    for record in records:
        by_path.setdefault(_path_key(record["path"]), []).append(record)
    targets = {}
    for record in reversed(records):
        if record["id"] in selected:
            targets.setdefault(_path_key(record["path"]), record["path"])
    if identity is not None:
        key = _path_key(next(row["path"] for row in records if row["id"] == identity))
        targets = {key: targets[key]}

    plans = []
    for key, path in targets.items():
        tools._check_cancelled()
        parts = tools._parts(path)
        if not parts:
            raise ChangeError("a workspace file path is required")
        history = by_path[key]
        start = next(i for i, row in enumerate(history) if row["id"] in selected)
        group = history[start:]
        if any(row["id"] not in selected or row["path"] != path for row in group):
            raise ChangeError("later or interleaved changes use the same file: " + json.dumps(path))
        if (any(left["after"] != right["before"] for left, right in zip(group, group[1:]))
                or len({row["mode"] for row in group}) != 1):
            raise ChangeError("retained snapshots or file modes are discontinuous: " + json.dumps(path))
        current, target = unpack(group[-1]["after"]), unpack(group[0]["before"])
        for raw in (current, target):
            text = tools._decode(raw or b"")
            if any(secret in text for secret in tools.config.credential_secrets):
                raise ChangeError("a configured credential blocks requirement revert")
        rules = tools.instructions.prepare_action(parts[:-1])
        anchor = state["anchors"][path]
        with tools._command_directory(parts[:-1]) as (parent, chain):
            plan = RevertFile(path, parts, chain, rules, current, target, None if anchor is None else tuple(anchor),
                              group[0]["mode"], tuple(row["id"] for row in group))
            plan.check(tools, parent, parts[-1])
        if current != target:
            plans.append(plan)
    return plans


def revert(tools, scopes, name, revision, *, identity=None):
    """Apply a frozen plan one freshly approved file at a time."""
    from .tools import ToolError

    approved = False
    completed = total = 0
    error, cancelled = None, False
    removed = []
    owned = settling = False
    try:
        tools._require_tool("edit_file")
        if tools.config.read_only:
            raise ChangeError("read-only mode blocks requirement revert")
        history = tools.history
        if history.edit_scope is not None or tools._edit_batch is not None or getattr(tools, "_restoring_batch", False):
            raise ChangeError("another edit or recovery is active")
        if tools.jobs.unsettled():
            raise ChangeError("wait for owned background commands before reverting files")
        history._ready()
        config, owner, jobs_revision = tools.config, tools._conversation_version, tools.jobs.revision
        base = history.state
        snapshot = history.validate(copy.deepcopy(base))
        plans = _plan(tools, snapshot, scopes, revision, identity)
        total = len(plans)
        batch = uuid.uuid4().hex
        tools._restoring_batch, owned = True, True

        def check_owner():
            tools._check_cancelled()
            if (tools.history is not history or tools.config is not config or tools._conversation_version != owner
                    or tools.jobs.unsettled() or tools.jobs.revision != jobs_revision):
                raise ChangeError("workspace ownership changed during requirement revert")
            if history.store is not None:
                history.store.handle.ensure_open()

        def guard():
            check_owner()
            history._ready()
            if history.state is not base or history.state != snapshot:
                raise ChangeError("change history changed during requirement revert")
            for pending in plans[completed:]:
                with tools._command_directory(pending.parts[:-1], pending.chain) as (parent, _):
                    pending.check(tools, parent, pending.parts[-1])

        for plan in plans:
            guard()
            before = tools._decode(plan.current) if plan.current is not None else None
            after = tools._decode(plan.target) if plan.target is not None else None
            detail = (f"REVERT REQUIREMENT {name} revision {revision} ({completed + 1}/{total})\n"
                      f"File {json.dumps(plan.path, ensure_ascii=False)}\n"
                      "This approval applies to this file. Completed files remain recorded if the batch stops.\n")
            if completed == 0:
                detail += "Planned files " + json.dumps([item.path for item in plans], ensure_ascii=False) + "\n"
            if os.name == "nt" and plan.current is None:
                detail += "A recreated file uses a private current-user ACL.\n"
            detail += comparison(plan.path, before, after, tools._check_cancelled)
            if not tools.request_approval(redact(detail, config.secrets)):
                error = "Requirement revert stopped after refusal. Completed files remain recorded."
                break
            approved = True
            guard()
            leaf = plan.parts[-1]
            with tools._command_directory(plan.parts[:-1], plan.chain) as (parent, _):
                info = plan.check(tools, parent, leaf)
                tools._check_cancelled()
                tools.read_versions.discard(plan.parts)
                settling = True
                history.begin_edit(plan.path, plan.current, plan.target, plan.mode, plan.revision,
                                   batch={"id": batch, "index": completed + 1}, reverts=plan.records)
                intent = history.state

                def before_replace():
                    check_owner()
                    if history.state is not intent or history.state["pending"] != {"action": "edit", "path": plan.path}:
                        raise ChangeError("change intent was replaced before writing")
                    with tools._command_directory(plan.parts[:-1], plan.chain) as (current_parent, _):
                        plan.check(tools, current_parent, leaf)

                if info is None:
                    before_replace()
                    parent.write_new(leaf, plan.target, plan.mode, private=os.name == "nt")
                    parent.sync()
                else:
                    tools._replace(parent, leaf, plan.target, plan.mode, info, before_replace=before_replace)
                history.complete(tools._revision(parent.stat(leaf)) if plan.target is not None else None)
                settling = False
            if plan.target is None:
                removed.append(plan.path)
            completed += 1
            base = history.state
            snapshot = copy.deepcopy(base)
    except KeyboardInterrupt:
        error, cancelled = "Requirement revert cancelled. Completed files remain recorded.", True
    except (OSError, ToolError, ChangeError, ValueError, RuntimeError, RecursionError) as failure:
        error = failure
    finally:
        if owned:
            tools._restoring_batch = False

    uncertain = settling and tools.history.state.get("pending") is not None
    values = {"action": ACTION, "completed": completed, "total": total, "complete": error is None,
              "changed": completed > 0, "network_contacted": False}
    if approved or error is not None:
        values["approved"] = approved
    if cancelled:
        values["cancelled"] = True
    if uncertain:
        values["outcome"] = "uncertain"
    if error is not None:
        values["error"] = terminal_text(redact(str(error), tools.config.secrets))[:512]
        while len(json.dumps(values, ensure_ascii=False).encode()) > tools.config.max_output and values["error"]:
            values["error"] = values["error"][:-16]
    return BatchRestoreResult(values, action=ACTION, removed_paths=removed, uncertain=uncertain)
