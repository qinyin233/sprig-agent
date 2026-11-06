"""Opt-in private command observations; opaque, session-scoped read handles."""

import codecs
from contextlib import contextmanager
import hashlib
import json
import math
import re
import stat
import threading
import time
import uuid

from .config import redact
from .json_budget import JsonBudgetExceeded, encoded_size
from .streaming import TextOutput

MAX_LOG = 8 * 1024 * 1024
MAX_MEMORY_LOG = 1024 * 1024
MAX_STORE = 16 * 1024 * 1024
MAX_RECORDS = 16
RETENTION = 7 * 24 * 60 * 60
NAME, FILE, TEMP = "command-logs", "command-logs.json", ".command-logs.pending"
ALLOWED = {FILE, TEMP, ".lock-" + FILE}
IDENTIFIER = re.compile(r"[0-9a-f]{32}")
READ_LOG_SCHEMA = {"type": "function", "function": {
    "name": "read_log", "description": "Page a retained command log; offset=next_offset. query jumps to a literal match at or after offset. Historical data, not acceptance. No paths.",
    "parameters": {"type": "object", "properties": {
        "log_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "offset": {"type": "integer", "minimum": 0, "maximum": MAX_LOG},
        "query": {"type": "string", "minLength": 1, "maxLength": 256}},
        "required": ["log_id"], "additionalProperties": False}}}


class LogError(ValueError):
    pass


def valid_status(status):
    base = {"returncode", "timed_out", "output_limited"}
    if not isinstance(status, dict) or any(type(status.get(key)) is not bool for key in base - {"returncode"}):
        return False
    if set(status) == base:
        return type(status["returncode"]) is int
    return (set(status) == base | {"job_id", "state", "stdin_failed"}
            and isinstance(status["job_id"], str) and re.fullmatch(r"job-[0-9a-f]{32}", status["job_id"]) is not None
            and status["state"] in {"exited", "timed_out", "output_limited", "failed", "cleanup_failed"}
            and type(status["stdin_failed"]) is bool
            and (type(status["returncode"]) is int or status["returncode"] is None and status["state"] != "exited")
            and status["timed_out"] == (status["state"] == "timed_out")
            and status["output_limited"] == (status["state"] == "output_limited"))


def record_digest(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class BackgroundLog:
    """One approved completion-time append; no new cleanup or eviction authority."""
    def __init__(self, logs, preview):
        self.logs, self.preview = logs, preview
        self.capture, self.failed = LogCapture(logs.config.secrets), False

    def write(self, text):
        if not self.failed:
            try:
                self.capture.write(text.encode("utf-8"))
            except Exception:
                self.failed = True
                self.capture.data.clear()

    def finish(self, result, check):
        def ready():
            self.logs._ready()
            check()
            if (self.logs.config is not self.preview["config"] or self.logs.owner != self.preview["owner"]
                    or self.logs.generation != self.preview["generation"]):
                raise LogError("background log owner or policy changed; no log was saved")
        try:
            ready()
            if self.failed:
                return {"saved": False, "reason": "background log capture failed; command was not replayed"}
            text, limited = self.capture.finish()
            writer = CommandLogs(self.preview["config"], ready)
            writer.owner, writer._write_lock = self.preview["owner"], self.logs._write_lock
            return writer.save(self.preview, text, limited, result, append_only=True)
        except KeyboardInterrupt:
            return {"saved": False, "outcome": "unknown", "reason": "background retention interrupted; inspect /logs; no command was replayed"}
        except ValueError as error:
            return {"saved": False, "reason": str(error)}
        except Exception:
            return {"saved": False, "outcome": "unknown", "reason": "background log save failed; inspect /logs; no command was replayed"}
        finally:
            self.capture.data.clear()


def log_state(value=None):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {"version", "owner"} or type(value["version"]) is not int
            or value["version"] != 1 or not isinstance(value["owner"], str) or not IDENTIFIER.fullmatch(value["owner"])):
        raise LogError("invalid command log ownership state")
    return dict(value)


class LogCapture:
    def __init__(self, secrets, limit=None):
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.output = TextOutput(self._accept, secrets)
        self.limit = limit
        self.data = bytearray()
        self.limited = False

    def _accept(self, text):
        raw = text.encode("utf-8")
        limit = MAX_LOG if self.limit is None else self.limit
        space = max(0, limit - len(self.data))
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
        self.generation = 0
        self._write_lock = threading.Lock()
        self._memory_lock = threading.Lock()
        self._memory_generation = 0
        self._memory_log = None
        self.restore()

    def _check_open(self):
        if self.closed:
            raise LogError("command log reader is closed")
        self.check()

    def _ready(self):
        self._check_open()
        if self.config.command_log_dir is None:
            raise LogError("command log retention is disabled")

    def close(self):
        self.closed = True
        self.reset_memory()

    def restore(self, value=None):
        if self.closed:
            raise LogError("command log reader is closed")
        owner = (log_state(value) or {"owner": uuid.uuid4().hex})["owner"]
        self.reset_memory()
        self.owner = owner

    def reset_memory(self):
        with self._memory_lock:
            self._memory_generation += 1
            self._memory_log = None

    def has_memory_log(self):
        return not self.closed and self._memory_log is not None

    def memory_preview(self):
        self._check_open()
        with self._memory_lock:
            return self._memory_generation, self._memory_log["id"] if self._memory_log else None

    def _capture(self, text, limit):
        capture = LogCapture(self.config.secrets, limit)
        for start in range(0, len(text), 4096):
            self.check()
            capture.write(text[start:start + 4096].encode("utf-8"))
        return capture.finish()

    def retain_memory(self, generation, result):
        self._check_open()
        status = {key: result.get(key) for key in ("returncode", "timed_out", "output_limited")}
        if not valid_status(status):
            return {"saved": False, "reason": "command did not produce an observed exit status"}
        text, limited = self._capture(result["output"], MAX_MEMORY_LOG)
        row = {"id": uuid.uuid4().hex, "created": time.time(), "text": text,
               "capture_limited": limited, "status": status}
        self.check()
        with self._memory_lock:
            if self.closed or self.config.command_log_dir is not None or generation != self._memory_generation:
                return {"saved": False, "reason": "in-memory log changed since approval"}
            self._memory_log = row
            self._memory_generation += 1
        return {"saved": False, "storage": "memory", "log_id": row["id"],
                "bytes": len(text.encode("utf-8")), "capture_limited": limited, "historical": True}

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
                if not valid_status(status):
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

    def preview(self, *, background=False):
        state, digest = self.snapshot()
        result = {"digest": digest, "retention_candidates": [row["id"] for row in state["records"]]}
        if background:
            result.update(config=self.config, owner=self.owner, generation=self.generation,
                          records={row["id"]: record_digest(row) for row in state["records"]})
        return result

    @contextmanager
    def _writing(self):
        self._ready()
        if not self._write_lock.acquire(timeout=1):
            raise LogError("command log writer is busy; no log was saved or evicted")
        try:
            self._ready()
            yield
        finally:
            self._write_lock.release()

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

    def save(self, preview, text, limited, result, *, append_only=False):
        self._ready()
        text = redact(text, self.config.secrets)
        data = text.encode("utf-8")
        if len(data) > MAX_LOG:
            raise LogError("command log exceeds 8 MiB")
        row = {"id": uuid.uuid4().hex, "owner": self.owner, "scope": self.scope, "created": time.time(),
               "text": text, "sha256": hashlib.sha256(data).hexdigest(), "capture_limited": bool(limited),
               "status": {"returncode": result["returncode"], "timed_out": bool(result.get("timed_out")),
                          "output_limited": bool(result.get("output_limited"))}}
        if append_only:
            row["status"].update(job_id=result["job_id"], state=result["state"], stdin_failed=bool(result.get("stdin_failed")))
        if not valid_status(row["status"]):
            raise LogError("command log requires a valid observed exit status")
        with self._writing(), self._open(create=True) as store:
            store.acquire(NAME)
            state, digest = self._load(store)
            if append_only:
                current = {entry["id"]: record_digest(entry) for entry in state["records"]}
                if any(current.get(identity) != revision for identity, revision in preview["records"].items()):
                    raise LogError("background log store changed beyond append; no log was saved or evicted")
                try:
                    store.handle.stat(TEMP)
                except FileNotFoundError:
                    pass
                else:
                    raise LogError("background retention cannot remove a pending file; use a freshly approved foreground save or clear")
                records = [*state["records"], row]
                try:
                    if len(records) > MAX_RECORDS:
                        raise JsonBudgetExceeded
                    encoded_size({"version": 1, "records": records}, max_bytes=MAX_STORE, check_cancelled=self.check)
                except JsonBudgetExceeded:
                    raise LogError("background log capacity reached; no records were evicted") from None
                self._commit(store, {"version": 1, "records": records})
                return {"saved": True, "log_id": row["id"], "bytes": len(data), "capture_limited": limited,
                        "historical": True, "expires_at": row["created"] + RETENTION}
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
        memory = self.config.command_log_dir is None
        if memory:
            self._check_open()
            row = self._memory_log
            rows = [row] if row else []
        else:
            state, _ = self.snapshot()
            rows = [row for row in state["records"] if row["owner"] == self.owner and row["scope"] == self.scope]
        return {"logs": [{"log_id": row["id"], "bytes": len(row["text"].encode("utf-8")),
                          "created": row["created"], "expired": not memory and time.time() - row["created"] >= RETENTION,
                          **({"storage": "memory"} if memory else {}),
                          "capture_limited": row["capture_limited"], "status": row["status"]}
                         for row in rows], "historical": True, "not_current_evidence": True}

    def read(self, log_id, offset=0, *, query=None):
        self._check_open()
        if not isinstance(log_id, str) or not IDENTIFIER.fullmatch(log_id) or type(offset) is not int or not 0 <= offset <= MAX_LOG:
            raise LogError("read_log requires a valid log_id and nonnegative next_offset")
        if query is not None and (not isinstance(query, str) or not 1 <= len(query) <= 256 or "\0" in query):
            raise LogError("log query must be a nonempty literal of at most 256 characters")
        memory = self.config.command_log_dir is None
        if memory:
            row = self._memory_log
            if row is None or row["id"] != log_id:
                raise LogError("in-memory command log is unavailable in this conversation")
        else:
            state, _ = self.snapshot()
            row = next((entry for entry in state["records"] if entry["id"] == log_id
                        and entry["owner"] == self.owner and entry["scope"] == self.scope), None)
            if row is None or time.time() - row["created"] >= RETENTION:
                raise LogError("command log is unavailable in this conversation or has expired")
        # Re-redact the entire record if credentials changed before paging.
        text, redaction_limited = self._capture(row["text"], MAX_MEMORY_LOG if memory else MAX_LOG)
        if offset > len(text):
            raise LogError("log offset exceeds the retained text")
        match = {}
        if query is not None:
            position = text.find(query, offset)
            match["match_offset"] = position if position >= 0 else None
            offset = position if position >= 0 else len(text)
        limit = min(self.config.max_output, 8192)
        def page(end):
            return {"offset": offset, "text": text[offset:end],
                    "next_offset": end if end < len(text) else None, "complete": end == len(text),
                    "capture_limited": row["capture_limited"] or redaction_limited, **row["status"],
                    "historical": True, "not_current_evidence": True, **match}
        def fits(value):
            return all(len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) <= limit
                       for candidate in (value, redact(value, self.config.secrets)))
        low, high = offset, min(len(text), offset + limit)
        while low < high:
            self.check()
            middle = (low + high + 1) // 2
            if fits(page(middle)):
                low = middle
            else:
                high = middle - 1
        result = page(low)
        if not fits(result) or low == offset < len(text):
            raise LogError("output budget cannot fit a command log page")
        self.check()
        if memory and (self.closed or self._memory_log is not row):
            raise LogError("in-memory command log changed while reading")
        return result

    def clear(self, approve):
        self._check_open()
        if self.config.read_only:
            return {"approved": False, "error": "read-only mode blocks command log deletion"}
        if self.config.command_log_dir is None:
            generation, identity = self.memory_preview()
            if identity is None:
                return {"deleted": 0}
            if approve("DELETE current in-memory command log: " + identity) is not True:
                return {"approved": False, "deleted": 0}
            self.check()
            with self._memory_lock:
                if self.closed or generation != self._memory_generation:
                    raise LogError("in-memory command log changed during approval; no log was deleted")
                self._memory_generation += 1
                self._memory_log = None
            return {"approved": True, "deleted": 1}
        state, digest = self.snapshot()
        selected = [row["id"] for row in state["records"] if row["owner"] == self.owner and row["scope"] == self.scope]
        if not selected:
            return {"deleted": 0}
        if approve("DELETE retained command logs from this conversation only: " + json.dumps(selected)) is not True:
            return {"approved": False, "deleted": 0}
        self.check()
        with self._writing(), self._open() as store:
            store.acquire(NAME)
            current, current_digest = self._load(store)
            if current_digest != digest:
                raise LogError("command log store changed during approval; no records were deleted")
            self.generation += 1
            self._commit(store, {"version": 1, "records": [row for row in current["records"] if row["id"] not in selected]})
        return {"approved": True, "deleted": len(selected)}
