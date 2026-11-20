"""Bounded exact edit backups; optional private journal, never automatic replay."""
import base64
import copy
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import uuid

from .session import SessionStore
from .requirement_edits import validate_scope

MAX_BACKUPS = 4 * 1024 * 1024
MAX_JOURNAL = 8 * 1024 * 1024
MAX_RECORDS = 16
RECORD_ID = re.compile(r"[a-f0-9]{32}")


class ChangeError(Exception):
    pass


def pack(data):
    return None if data is None else base64.b64encode(data).decode("ascii")


def unpack(data):
    return None if data is None else base64.b64decode(data, validate=True)


def backup_size(record):
    return sum(len(unpack(record[name]) or b"") for name in ("before", "after"))


class ChangeHistory:
    def __init__(self, config, directory=None):
        self.workdir = str(config.workdir)
        self.store = None
        self.state = self.empty()
        self.edit_scope = None
        if directory is not None:
            self.store = SessionStore(directory, config)
            self.name = "changes-" + hashlib.sha256(self.workdir.encode("utf-8")).hexdigest()[:32]
            self.filename = self.name + ".json"
            try:
                self.store.acquire(self.name)
                try:
                    self.store._check_file(self.filename)
                except FileNotFoundError:
                    return
                raw = self.store.handle.read(self.filename, MAX_JOURNAL, private=True)
                if len(raw) > MAX_JOURNAL:
                    raise ChangeError("change journal exceeds 8 MiB")
                try:
                    self.state = self.validate(json.loads(raw))
                except (ValueError, UnicodeError, RecursionError):
                    raise ChangeError("invalid change journal; no workspace action was replayed") from None
            except BaseException:
                self.store.close()
                raise

    def empty(self):
        return {"version": 1, "workdir": self.workdir, "records": [], "cursor": 0, "anchors": {}, "pending": None}

    def validate(self, state):
        try:
            if (not isinstance(state, dict) or state.get("version") != 1 or state.get("workdir") != self.workdir
                    or not isinstance(state["records"], list) or len(state["records"]) > MAX_RECORDS
                    or type(state["cursor"]) is not int or not 0 <= state["cursor"] <= len(state["records"])
                    or not isinstance(state["anchors"], dict)):
                raise ValueError
            paths, batches, last_batch = set(), {}, None
            for record in state["records"]:
                if (not isinstance(record, dict) or not isinstance(record["id"], str) or len(record["id"]) != 32
                        or not isinstance(record["path"], str) or not 0 < len(record["path"]) <= 4096
                        or type(record["mode"]) is not int or not 0 <= record["mode"] <= 0o777
                        or (record["before"] is None and record["after"] is None)):
                    raise ValueError
                for field in ("before", "after"):
                    if record[field] is not None and (not isinstance(record[field], str) or len(unpack(record[field])) > 512 * 1024):
                        raise ValueError
                if "origin" in record and record["origin"] != "command":
                    raise ValueError
                if "scope" in record:
                    validate_scope(record["scope"])
                label = record.get("batch")
                if "batch" in record:
                    if (not isinstance(label, dict) or set(label) != {"id", "index"}
                            or not isinstance(label["id"], str) or not RECORD_ID.fullmatch(label["id"])
                            or type(label["index"]) is not int or not 1 <= label["index"] <= 16):
                        raise ValueError
                    identity, position = label["id"], label["index"]
                    key = tuple(part.casefold() if os.name == "nt" else part for part in PurePosixPath(record["path"]).parts)
                    previous = batches.get(identity)
                    if previous is not None and (last_batch != identity or position != previous[0] + 1 or key in previous[1]):
                        raise ValueError
                    batches[identity] = (position, (previous[1] if previous else set()) | {key})
                    last_batch = identity
                else:
                    last_batch = None
                paths.add(record["path"])
            if set(state["anchors"]) != paths or sum(map(backup_size, state["records"])) > MAX_BACKUPS:
                raise ValueError
            for revision in state["anchors"].values():
                if revision is not None and (not isinstance(revision, list) or len(revision) != 10
                        or any(value is not None and type(value) is not int for value in revision)):
                    raise ValueError
            pending = state["pending"]
            if pending is not None and (not isinstance(pending, dict) or pending.get("action") not in {"edit", "undo", "redo"}
                                        or pending.get("path") not in paths):
                raise ValueError
            return state
        except (KeyError, TypeError, ValueError, RecursionError):
            raise ChangeError("invalid change journal; no workspace action was replayed") from None

    def close(self):
        if self.store is not None:
            self.store.close()

    def _save(self, state):
        # Keep a pending state in memory even if persistence fails: no second
        # action is allowed while the outcome of the first is uncertain.
        self.state = state
        if self.store is None:
            return
        body = json.dumps(state, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_JOURNAL:
            raise ChangeError("change journal exceeds 8 MiB; action blocked")
        try:
            self.store._check_file(self.filename)
        except FileNotFoundError:
            pass
        temp = "." + uuid.uuid4().hex + ".tmp"
        try:
            self.store.handle.write_new(temp, body, private=True)
            self.store.handle.replace(temp, self.filename)
            self.store.handle.sync()
        finally:
            try:
                self.store.handle.unlink(temp)
            except FileNotFoundError:
                pass

    def _ready(self):
        if self.state["pending"] is not None:
            raise ChangeError("an interrupted change has an uncertain outcome; inspect files and /changes before explicitly discarding its history. No action was replayed")

    def summary(self):
        return {"persistent": self.store is not None, "pending": self.state["pending"],
                "retained_bytes": sum(map(backup_size, self.state["records"])), "limit_bytes": MAX_BACKUPS,
                "record_limit": MAX_RECORDS,
                "records": [{"id": record["id"], "path": record["path"],
                             "applied": None if self.state["pending"] and self.state["pending"]["path"] == record["path"] else index < self.state["cursor"],
                             "before_bytes": None if record["before"] is None else len(unpack(record["before"])),
                             "after_bytes": None if record["after"] is None else len(unpack(record["after"]))}
                            | ({"batch": copy.deepcopy(record["batch"])} if "batch" in record else {})
                            | ({"origin": record["origin"]} if "origin" in record else {})
                            for index, record in enumerate(self.state["records"])]}

    def _entry(self, path, before, after, mode):
        record = {"id": uuid.uuid4().hex, "path": path, "before": pack(before), "after": pack(after), "mode": mode}
        if self.edit_scope is not None:
            record["scope"] = dict(validate_scope(self.edit_scope))
        return record

    def _append(self, records, revisions):
        self._ready()
        state = copy.deepcopy(self.state)
        state["records"] = state["records"][:state["cursor"]]
        state["records"].extend(records)
        while len(state["records"]) > MAX_RECORDS or sum(map(backup_size, state["records"])) > MAX_BACKUPS:
            state["records"].pop(0)
        state["cursor"] = len(state["records"])
        paths = {record["path"] for record in state["records"]}
        state["anchors"].update({path: None if revision is None else list(revision)
                                 for path, revision in revisions.items()})
        state["anchors"] = {key: value for key, value in state["anchors"].items() if key in paths}
        return state

    def begin_edit(self, path, before, after, mode, revision, *, batch=None):
        record = self._entry(path, before, after, mode)
        if batch is not None:
            record["batch"] = copy.deepcopy(batch)
        state = self._append([record], {path: revision})
        state["pending"] = {"action": "edit", "path": path}
        if batch is not None:
            self.validate(state)  # A malformed association must fail before a journal or workspace write.
        self._save(state)

    def record_command(self, observed):
        """Commit a settled command's observed file deltas as one journal update."""
        identity = uuid.uuid4().hex
        records, revisions = [], {}
        for index, (path, before, after, mode, revision) in enumerate(observed, 1):
            records.append(self._entry(path, before, after, mode)
                           | {"origin": "command", "batch": {"id": identity, "index": index}})
            revisions[path] = revision
        if not records or len(records) > MAX_RECORDS or sum(map(backup_size, records)) > MAX_BACKUPS:
            raise ChangeError("command backups exceed the retained history limits")
        state = self.validate(self._append(records, revisions))
        try:
            self._save(state)
        except BaseException:
            state["pending"] = {"action": "edit", "path": records[0]["path"]}
            self.state = state
            raise
        return identity

    def plan_batch(self, action):
        state = self.validate(copy.deepcopy(self.state))
        self._ready()
        if action not in {"undo", "redo"}:
            raise ChangeError("unknown batch restore action")
        cursor, records = state["cursor"], state["records"]
        index = cursor - 1 if action == "undo" else cursor
        if not 0 <= index < len(records) or "batch" not in records[index]:
            raise ChangeError("no retained batch at the history cursor; use individual undo/redo")
        identity = records[index]["batch"]["id"]
        start, stop = index, index + 1
        while start and records[start - 1].get("batch", {}).get("id") == identity:
            start -= 1
        while stop < len(records) and records[stop].get("batch", {}).get("id") == identity:
            stop += 1
        if records[start]["batch"]["index"] != 1:
            raise ChangeError("batch backups are incomplete after retention; use individual undo/redo")
        return identity, list(reversed(records[start:cursor])) if action == "undo" else records[cursor:stop]

    def plan_to(self, action, identity):
        self._ready()
        if action not in {"undo", "redo"}:
            raise ChangeError("unknown history range action")
        if not isinstance(identity, str) or not RECORD_ID.fullmatch(identity):
            raise ChangeError("use the full change ID from /changes or /changes review")
        state = self.validate(copy.deepcopy(self.state))
        cursor, records = state["cursor"], state["records"]
        matches = [index for index, record in enumerate(records) if record["id"] == identity]
        if len(matches) != 1:
            raise ChangeError("the requested change ID is not uniquely retained")
        index = matches[0]
        if action == "undo" and index < cursor:
            return list(reversed(records[index:cursor]))
        if action == "redo" and index >= cursor:
            return records[cursor:index + 1]
        raise ChangeError("the requested record is not on the " + action + " side of the history cursor")

    def plan(self, action):
        self._ready()
        cursor = self.state["cursor"]
        if action not in {"undo", "redo"}:
            raise ChangeError("unknown change action")
        index = cursor - 1 if action == "undo" else cursor
        if index < 0 or index >= len(self.state["records"]):
            raise ChangeError("no change to " + action)
        record = copy.deepcopy(self.state["records"][index])
        revision = self.state["anchors"][record["path"]]
        return record, None if revision is None else tuple(revision)

    def begin_restore(self, action, path):
        self._ready()
        state = copy.deepcopy(self.state)
        state["cursor"] += -1 if action == "undo" else 1
        state["pending"] = {"action": action, "path": path}
        self._save(state)

    def complete(self, revision):
        state = copy.deepcopy(self.state)
        pending = copy.deepcopy(state["pending"])
        path = pending["path"]
        state["anchors"][path] = None if revision is None else list(revision)
        state["pending"] = None
        try:
            self._save(state)
        except BaseException:
            self.state["pending"] = pending
            raise ChangeError("workspace change may have completed, but its journal checkpoint failed; inspect current files. Further changes are blocked") from None

    def discard(self):
        self._save(self.empty())
