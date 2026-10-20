"""Revision-pinned file lifecycle actions, with a fresh approval for every stage."""

import hashlib
import json
import os
import stat

from .changes import ChangeError
from .revisions import valid_read_id


def _source(tools, path, read_id):
    parts = tools._parts(path)
    if not valid_read_id(read_id):
        raise ValueError("file_action requires a complete read_id from read_file for this source")
    expected = tools.read_versions.resolve(read_id, parts)
    if expected is None:
        raise ValueError("read_id is not valid for this file/current run; read_file again")
    with tools._parent(path) as (parent, name):
        raw, info = tools._snapshot(parent, name)
    if raw is None or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("source revision changed; read_file again before file_action")
    text = tools._decode(raw)
    if any(secret in text for secret in tools.config.credential_secrets):
        raise ValueError("configured API key must not appear in change backups")
    return parts, raw, info, text


def _remove(tools, path, raw, info, text, rules, *, move_to=None, after_approval=lambda: None):
    parts = tools._parts(path)
    with tools._parent(path) as (parent, name):
        tools._unchanged(parent, name, raw, info)
        description = "DELETE " + json.dumps(path) + "\n" + (tools._diff(path, text, None) or "(delete empty file)")
        if move_to is not None:
            description += ("\nStep 2/2 of move to " + json.dumps(move_to) +
                            "; destination copy already exists. Undo uses two file-history steps. "
                            "On success, live references to this source follow the destination.")
        else:
            description += "\nApproved deletion removes this path from live references. Saved older messages are unchanged."
        if not tools.request_approval(description):
            return {"approved": False, "changed": False, "action": "delete", "path": path}
        after_approval()
        tools._parts(path)
        tools.instructions.recheck_action(parts[:-1], rules)
        tools._unchanged(parent, name, raw, info)
        tools._check_cancelled()
        tools.history._ready()
        tools.read_versions.discard(parts)
        try:
            tools.history.begin_edit(path, raw, None, stat.S_IMODE(info.st_mode) & 0o777, tools._revision(info))
            tools._replace(parent, name, None, 0, info)
            tools.history.complete(None)
        except (OSError, ChangeError) as error:
            return {"approved": True, "changed": False, "outcome": "uncertain", "path": path,
                    "action": "delete", "error": "Delete outcome is uncertain; inspect files and /changes: " + str(error)}
    return {"approved": True, "changed": True, "action": "delete", "path": path}


def _mkdir(tools, path):
    parts = tools._parts(path)
    rules = tools.instructions.prepare_action(parts[:-1])
    with tools._parent(path) as (parent, name):
        try:
            parent.stat(name)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("mkdir target already exists; no change made")
        if not tools.request_approval("MKDIR " + json.dumps(path) + "\nCreate exactly this directory; parent must exist. "
                             "Directory creation is not recorded in file undo history."):
            return {"approved": False, "changed": False, "action": "mkdir", "path": path}
        tools._parts(path)
        tools.instructions.recheck_action(parts[:-1], rules)
        tools._check_cancelled()
        tools.history._ready()
        # mkdir is exclusive: a late file, directory or link is never replaced.
        try:
            parent.mkdir(name)
            parent.sync()
        except OSError as error:
            return {"approved": True, "changed": False, "outcome": "uncertain", "action": "mkdir", "path": path,
                    "error": "Directory creation outcome is uncertain; inspect current state: " + str(error)}
    return {"approved": True, "changed": True, "action": "mkdir", "path": path, "undo_available": False}


def _move(tools, path, destination, raw, info, text, parts, rules):
    from .tools import ToolError
    target_parts = tools._parts(destination)
    key = lambda value: tuple(part.casefold() if os.name == "nt" else part for part in value)
    if not target_parts or key(parts) == key(target_parts):
        raise ValueError("move requires a different destination file path")
    target_rules = tools.instructions.prepare_action(target_parts[:-1])
    with tools._parent(path) as (source_parent, source_name), tools._parent(destination) as (parent, name):
        tools._unchanged(source_parent, source_name, raw, info)
        try:
            parent.stat(name)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("move destination already exists; no file is overwritten")
        description = ("COPY FOR MOVE " + json.dumps(path) + " -> " + json.dumps(destination) +
                       "\nStep 1/2: create an exclusive copy. Source deletion needs a separate new approval. "
                       "Refusal, cancellation or errors can leave both files; this is not an atomic rename.\n" +
                       (tools._diff(destination, None, text) or "(create empty file)"))
        if os.name == "nt":
            description += "\nThe new Windows copy has a private current-user ACL; filesystem metadata is not fully preserved."
        if not tools.request_approval(description):
            return {"approved": False, "changed": False, "action": "move", "path": path, "destination": destination}
        tools._parts(path)
        tools._parts(destination)
        tools.instructions.recheck_action(parts[:-1], rules)
        tools.instructions.recheck_action(target_parts[:-1], target_rules)
        tools._unchanged(source_parent, source_name, raw, info)
        tools._check_cancelled()
        tools.history._ready()
        tools.read_versions.discard(target_parts)
        mode = stat.S_IMODE(info.st_mode) & 0o777
        try:
            tools.history.begin_edit(destination, None, raw, mode, None)
            # An exclusive create must never clobber a target appearing after approval.
            parent.write_new(name, raw, mode, private=os.name == "nt")
            parent.sync()
            tools.history.complete(tools._revision(parent.stat(name)))
        except (OSError, ChangeError) as error:
            return {"approved": True, "changed": False, "outcome": "uncertain", "action": "move", "path": path,
                    "destination": destination, "source_removed": False,
                    "error": "Move copy outcome is uncertain; source preserved. Inspect files and /changes: " + str(error)}
        try:
            copied, copied_stat = tools._snapshot(parent, name)
            if copied != raw:
                raise ValueError("Move copy changed before source deletion; source preserved. Inspect both files.")
            # Recheck destination privacy, rules and identity after deletion approval.
            def recheck_copy():
                tools._parts(destination)
                tools.instructions.recheck_action(target_parts[:-1], target_rules)
                tools._unchanged(parent, name, copied, copied_stat)
            result = _remove(tools, path, raw, info, text, rules, move_to=destination, after_approval=recheck_copy)
        except (OSError, ValueError, ChangeError, ToolError) as error:
            result = {"error": str(error), "changed": False}
    removed = result.get("approved") is True and result.get("changed") is True and "error" not in result
    output = {**result, "action": "move", "path": path, "destination": destination, "copied": True,
              "source_removed": None if result.get("outcome") == "uncertain" else removed,
              "changed": True, "partial": not removed}
    if not removed and "error" not in output:
        output["error"] = "Move stopped after copy; source deletion was not approved. Both files remain; inspect before continuing."
    return output


def execute(tools, action, path, read_id=None, destination=None):
    tools._require_tool("file_action")
    if tools.config.read_only:
        return {"approved": False, "changed": False, "error": "read-only mode blocks file actions"}
    tools._check_cancelled()
    tools.history._ready()
    if any(secret in value for value in (path, destination) if isinstance(value, str) for secret in tools.config.secrets):
        raise ValueError("configured secrets cannot appear in file action paths")
    if action == "mkdir":
        if read_id is not None or destination is not None:
            raise ValueError("mkdir only accepts action and path")
        return _mkdir(tools, path)
    if action not in {"delete", "move"} or (action == "delete" and destination is not None):
        raise ValueError("file_action requires mkdir, delete or move with the appropriate fields")
    if action == "move" and not isinstance(destination, str):
        raise ValueError("move requires destination")
    parts, raw, info, text = _source(tools, path, read_id)
    rules = tools.instructions.prepare_action(parts[:-1])
    if action == "delete":
        return _remove(tools, path, raw, info, text, rules)
    return _move(tools, path, destination, raw, info, text, parts, rules)
