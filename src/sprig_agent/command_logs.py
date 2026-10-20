"""Opt-in private command observations; opaque, session-scoped read handles."""

import codecs
from contextlib import contextmanager
import hashlib
import json
import math
import re
import stat
import time
import uuid

from .config import redact
from .json_budget import JsonBudgetExceeded, encoded_size
from .streaming import TextOutput

MAX_LOG = 8 * 1024 * 1024
MAX_STORE = 16 * 1024 * 1024
MAX_RECORDS = 16
RETENTION = 7 * 24 * 60 * 60
NAME, FILE, TEMP = "command-logs", "command-logs.json", ".command-logs.pending"
ALLOWED = {FILE, TEMP, ".lock-" + FILE}
IDENTIFIER = re.compile(r"[0-9a-f]{32}")
READ_LOG_SCHEMA = {"type": "function", "function": {
    "name": "read_log", "description": "Read a retained command log by opaque log_id from this conversation; offset=next_offset. Historical data, never current checks or file read_id. No paths.",
    "parameters": {"type": "object", "properties": {
        "log_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "offset": {"type": "integer", "minimum": 0, "maximum": MAX_LOG}},
        "required": ["log_id"], "additionalProperties": False}}}


class LogError(ValueError):
    pass


def log_state(value=None):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"version", "owner"} or type(value["version"]) is not int
            or value["version"] != 1 or not isinstance(value["owner"], str) or not IDENTIFIER.fullmatch(value["owner"])):
        raise LogError("invalid command log ownership state")
    return dict(value)


class LogCapture:
    def __init__(self, secrets):
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.output = TextOutput(self._accept, secrets)
        self.data = bytearray()
        self.limited = False

    def _accept(self, text):
        raw = text.encode("utf-8")
        space = MAX_LOG - len(self.data)
        self.data.extend(raw[:space])
        self.limited |= len(raw) > space

    def write(self, data):
        self.output.write(self.decoder.decode(data, final=False))

    def finish(self):
        self.output.write(self.decoder.decode(b"", final=True))
        self.output.finish()
        return self.data.decode("utf-8", errors="ignore"), self.limited


class CommandLogs:
    def __init__(self, config, check_cancelled=lambda: None):
        self.config, self.check = config, check_cancelled
        self.closed = False
        self.restore()

    def _ready(self):
        if self.closed:
            raise LogError("command log reader is closed")
        self.check()
        if self.config.command_log_dir is None:
            raise LogError("command log retention is disabled")

    def close(self):
        self.closed = True

    def restore(self, value=None):
        if self.closed:
            raise LogError("command log reader is closed")
        self.owner = (log_state(value) or {"owner": uuid.uuid4().hex})["owner"]

    def state(self):
        return {"version": 1, "owner": self.owner} if self.config.command_log_dir is not None else None

    @property
    def scope(self):
        return hashlib.sha256(str(self.config.workdir).encode("utf-8")).hexdigest()

    @contextmanager
    def _open(self, create=False):
        from .session import SessionError, SessionStore
        store = None
        try:
            store = SessionStore(self.config.command_log_dir, self.config, create=create)
            yield store
        except SessionError as error:
            raise LogError(str(error)) from None
        finally:
            if store is not None:
                store.close()

    def _load(self, store):
        # A dedicated directory makes the payload quota and crash residue
        # bounded: one journal, one fixed pending file and one stable lock.
        with store.handle.scandir() as entries:
            for entry in entries:
                self.check()
                if entry.name not in ALLOWED:
                    raise LogError("command log directory contains unrelated entries; use a dedicated private directory")
                if entry.name == ".lock-" + FILE:
                    # Acquiring the stable lease validates its type/privacy.
                    # Windows deliberately forbids reopening an owned lock.
                    continue
                info = store.handle.stat(entry.name)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise LogError("unsafe command log storage file")
                store.handle.check_private(entry.name)
                if entry.name != ".lock-" + FILE and info.st_size > MAX_STORE:
                    raise LogError("command log storage exceeds its payload limit")
        try:
            raw = store.handle.read(FILE, MAX_STORE, private=True)
        except FileNotFoundError:
            return {"version": 1, "records": []}, None
        if len(raw) > MAX_STORE:
            raise LogError("command log storage exceeds 16 MiB")
        try:
            state = json.loads(raw)
            if (not isinstance(state, dict) or set(state) != {"version", "records"}
                    or type(state["version"]) is not int or state["version"] != 1
                    or not isinstance(state["records"], list) or len(state["records"]) > MAX_RECORDS):
                raise ValueError
            ids = set()
            for row in state["records"]:
                self.check()
                if (not isinstance(row, dict) or set(row) != {"id", "owner", "scope", "created", "text", "sha256", "capture_limited", "status"}
                        or any(not isinstance(row[key], str) or not IDENTIFIER.fullmatch(row[key]) for key in ("id", "owner"))
                        or row["id"] in ids or any(not isinstance(row[key], str) or not re.fullmatch(r"[0-9a-f]{64}", row[key]) for key in ("scope", "sha256"))
                        or type(row["created"]) not in (int, float) or not math.isfinite(row["created"]) or not 0 <= row["created"] <= 10**12
                        or type(row["capture_limited"]) is not bool or not isinstance(row["text"], str)):
                    raise ValueError
                data = row["text"].encode("utf-8")
                if len(data) > MAX_LOG or hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise ValueError
                status = row["status"]
                if (not isinstance(status, dict) or set(status) != {"returncode", "timed_out", "output_limited"}
                        or type(status["returncode"]) is not int
                        or any(type(status[key]) is not bool for key in ("timed_out", "output_limited"))):
                    raise ValueError
                ids.add(row["id"])
        except (ValueError, TypeError, KeyError, RecursionError):
            raise LogError("invalid command log storage; no records were replayed or removed") from None
        return state, hashlib.sha256(raw).hexdigest()

    def snapshot(self):
        self._ready()
        try:
            with self._open() as store:
                return self._load(store)
        except FileNotFoundError:
            return {"version": 1, "records": []}, None

    def preview(self):
        state, digest = self.snapshot()
        return {"digest": digest, "retention_candidates": [row["id"] for row in state["records"]]}

    def _commit(self, store, state):
        self.check()
        encoded_size(state, max_bytes=MAX_STORE, check_cancelled=self.check)
        body = json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        # The fresh storage approval covers removing only this fixed stale
        # pending file and the listed journal records, never arbitrary files.
        try:
            store.handle.unlink(TEMP)
        except FileNotFoundError:
            pass
        try:
            store.handle.write_new(TEMP, body, private=True)
            self.check()
            store.handle.replace(TEMP, FILE)
            store.handle.sync()
        finally:
            try:
                store.handle.unlink(TEMP)
            except FileNotFoundError:
                pass

    def save(self, preview, text, limited, result):
        self._ready()
        text = redact(text, self.config.secrets)
        data = text.encode("utf-8")
        if len(data) > MAX_LOG:
            raise LogError("command log exceeds 8 MiB")
        row = {"id": uuid.uuid4().hex, "owner": self.owner, "scope": self.scope, "created": time.time(),
               "text": text, "sha256": hashlib.sha256(data).hexdigest(), "capture_limited": bool(limited),
               "status": {"returncode": result["returncode"], "timed_out": bool(result.get("timed_out")),
                          "output_limited": bool(result.get("output_limited"))}}
        with self._open(create=True) as store:
            store.acquire(NAME)
            state, digest = self._load(store)
            if digest != preview["digest"]:
                raise LogError("command log store changed since approval; no log was saved or evicted")
            now = time.time()
            records = [entry for entry in state["records"] if now - entry["created"] < RETENTION]
            records.append(row)
            while True:
                try:
                    if len(records) > MAX_RECORDS:
                        raise JsonBudgetExceeded
                    encoded_size({"version": 1, "records": records}, max_bytes=MAX_STORE, check_cancelled=self.check)
                    break
                except JsonBudgetExceeded:
                    if len(records) <= 1:
                        raise LogError("encoded command log exceeds store capacity; no records were evicted") from None
                    records.pop(0)
            self._commit(store, {"version": 1, "records": records})
        return {"saved": True, "log_id": row["id"], "bytes": len(data), "capture_limited": limited,
                "historical": True, "expires_at": row["created"] + RETENTION}

    def list(self):
        state, _ = self.snapshot()
        return {"logs": [{"log_id": row["id"], "bytes": len(row["text"].encode("utf-8")),
                          "created": row["created"], "expired": time.time() - row["created"] >= RETENTION,
                          "capture_limited": row["capture_limited"], "status": row["status"]}
                         for row in state["records"] if row["owner"] == self.owner and row["scope"] == self.scope],
                "historical": True, "not_current_evidence": True}

    def read(self, log_id, offset=0):
        self._ready()
        if not isinstance(log_id, str) or not IDENTIFIER.fullmatch(log_id) or type(offset) is not int or not 0 <= offset <= MAX_LOG:
            raise LogError("read_log requires a valid log_id and nonnegative next_offset")
        state, _ = self.snapshot()
        row = next((entry for entry in state["records"] if entry["id"] == log_id
                    and entry["owner"] == self.owner and entry["scope"] == self.scope), None)
        if row is None or time.time() - row["created"] >= RETENTION:
            raise LogError("command log is unavailable in this conversation or has expired")
        # Re-redact the entire record if credentials changed before paging.
        capture = LogCapture(self.config.secrets)
        for start in range(0, len(row["text"]), 4096):
            self.check()
            capture.write(row["text"][start:start + 4096].encode("utf-8"))
        text, redaction_limited = capture.finish()
        if offset > len(text):
            raise LogError("log offset exceeds the retained text")
        limit = min(self.config.max_output, 8192)
        def page(end):
            return {"offset": offset, "text": text[offset:end],
                    "next_offset": end if end < len(text) else None, "complete": end == len(text),
                    "capture_limited": row["capture_limited"] or redaction_limited, **row["status"],
                    "historical": True, "not_current_evidence": True}
        low, high = offset, min(len(text), offset + limit)
        while low < high:
            self.check()
            middle = (low + high + 1) // 2
            if len(json.dumps(page(middle), ensure_ascii=False).encode("utf-8")) <= limit:
                low = middle
            else:
                high = middle - 1
        result = page(low)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > limit or low == offset < len(text):
            raise LogError("output budget cannot fit a command log page")
        return result

    def clear(self, approve):
        self._ready()
        if self.config.read_only:
            return {"approved": False, "error": "read-only mode blocks command log deletion"}
        state, digest = self.snapshot()
        selected = [row["id"] for row in state["records"] if row["owner"] == self.owner and row["scope"] == self.scope]
        if not selected:
            return {"deleted": 0}
        if not approve("DELETE retained command logs from this conversation only: " + json.dumps(selected)):
            return {"approved": False, "deleted": 0}
        self.check()
        with self._open() as store:
            store.acquire(NAME)
            current, current_digest = self._load(store)
            if current_digest != digest:
                raise LogError("command log store changed during approval; no records were deleted")
            self._commit(store, {"version": 1, "records": [row for row in current["records"] if row["id"] not in selected]})
        return {"approved": True, "deleted": len(selected)}
