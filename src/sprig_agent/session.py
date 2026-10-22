"""Explicit, private, atomic local transcripts. Loading never runs an action."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid
from contextlib import contextmanager

from .config import redact
from .filesystem import Directory
from .json_budget import JsonBudgetExceeded, encoded_size
from .provider import ProviderError, validate_message
from .references import reference_state
from .summary import summary_state
from .command_logs import log_state

MAX_SESSION = 8 * 1024 * 1024
MAX_LIST_ENTRIES = 4096
MAX_LIST_SESSIONS = 256
MAX_LIST_BYTES = MAX_SESSION


class SessionError(Exception):
    pass


def application_check(message):
    """Validate application-origin metadata; it never authorizes an operation."""
    if "sprig_check" not in message:
        return None
    phase = message["sprig_check"]
    prefixes = {"baseline": "sprig-baseline-", "edit-check": "sprig-edit-check-", "acceptance": "sprig-check-"}
    calls = message.get("tool_calls")
    if (not isinstance(phase, str) or phase not in prefixes or message.get("role") != "assistant"
            or message.get("content") is not None or not isinstance(calls, list) or len(calls) != 1):
        raise ValueError("invalid application check marker")
    call = calls[0]
    function = call.get("function") if isinstance(call, dict) else None
    if (not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("id"), str)
            or not re.fullmatch(re.escape(prefixes[phase]) + r"[0-9a-f]{32}", call["id"])
            or not isinstance(function, dict) or function.get("name") != "run"
            or not isinstance(function.get("arguments"), str)):
        raise ValueError("invalid application check marker")
    return phase


def validate_history(messages):
    if not isinstance(messages, list) or not messages or len(messages) > 5000:
        raise SessionError("invalid session history")
    clean, pending, seen = [], [], set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise SessionError("invalid session message")
        role, content = message.get("role"), message.get("content")
        if not isinstance(role, str):
            raise SessionError("invalid session role")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not pending or call_id != pending[0] or not isinstance(content, str):
                raise SessionError("invalid tool result order")
            pending.pop(0)
            clean.append({"role": "tool", "tool_call_id": call_id, "content": content})
        elif pending:
            raise SessionError("unfinished tool calls before another message")
        elif role == "assistant":
            try:
                normalized = validate_message(message, preserve_reasoning=True, wire_api="saved")
                phase = application_check(message)
                if phase is not None:
                    normalized["sprig_check"] = phase
            except (ProviderError, ValueError) as exc:
                raise SessionError(str(exc)) from None
            pending = [call["id"] for call in normalized.get("tool_calls", [])]
            if any(call_id in seen for call_id in pending):
                raise SessionError("duplicate historical tool call ID")
            seen.update(pending)
            clean.append(normalized)
        elif role in {"system", "user"} and isinstance(content, str):
            if (role == "system") != (index == 0):
                raise SessionError("session must start with one system message")
            clean.append({"role": role, "content": content})
        else:
            raise SessionError("invalid session role or content")
    if clean[0]["role"] != "system":
        raise SessionError("missing system message")
    # A crash could have occurred after a side effect but before the result was saved.
    for call_id in pending:
        clean.append({"role": "tool", "tool_call_id": call_id, "content": json.dumps({
            "error": "Interrupted: this action may or may not have run. It was NOT replayed. Inspect current state before proposing another action."})})
    return clean


class SessionStore:
    def __init__(self, directory, config, *, create=True):
        self.directory = Path(directory).expanduser().absolute()
        # macOS exposes these root-owned system paths as aliases into /private.
        # Normalize only these known aliases; user-created symlinks anywhere
        # below them still go through the no-follow directory traversal.
        if sys.platform == "darwin":
            for name in ("var", "tmp", "etc"):
                alias, target = Path("/") / name, Path("/private") / name
                if (self.directory.parts[:2] == alias.parts and alias.is_symlink()
                        and alias.resolve() == target):
                    self.directory = target.joinpath(*self.directory.parts[2:])
                    break
        self.config = config
        self.loaded_references = reference_state()
        self.loaded_summary = None
        self.loaded_logs = None
        # A nested workdir must not allow storing transcripts elsewhere in the same repo.
        forbidden = [config.workdir]
        for parent in (config.workdir, *config.workdir.parents):
            marker = parent / ".git"
            if marker.is_file() or (marker / "HEAD").is_file():
                forbidden.append(parent)
        resolved = self.directory.resolve()
        if any(resolved.is_relative_to(root) for root in forbidden):
            raise SessionError("session directory must be outside the workspace/repository")
        # A linked workspace has a different root from its original checkout.
        # Check the destination too, so transcripts/backups cannot enter another
        # checkout or a bare repository simply because the active root moved.
        for parent in (resolved, *resolved.parents):
            marker = parent / ".git"
            if (marker.is_file() or (marker / "HEAD").is_file()
                    or ((parent / "HEAD").is_file() and (parent / "config").is_file()
                        and (parent / "objects").is_dir())):
                raise SessionError("session directory must be outside every Git repository")
        directory_handle = Directory(self.directory.anchor)
        try:
            for part in self.directory.parts[1:]:
                try:
                    child = directory_handle.child(part)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        directory_handle.mkdir(part)
                    except FileExistsError:
                        pass  # Another process may have created the same directory.
                    child = directory_handle.child(part)
                directory_handle.close()
                directory_handle = child
            directory_handle.check_private()
            self.handle, directory_handle = directory_handle, None
            self._leases = {}
        except ValueError as exc:
            raise SessionError(str(exc)) from None
        finally:
            if directory_handle is not None:
                directory_handle.close()

    def close(self):
        for lease in self._leases.values():
            lease.close()
        self._leases.clear()
        self.handle.close()

    def acquire(self, name):
        filename = self._filename(name)
        if name not in self._leases:
            try:
                self._leases[name] = self.handle.lock(".lock-" + filename)
            except (OSError, ValueError):
                raise SessionError("session is busy in another process or its lock file is unsafe") from None

    def release(self, name):
        lease = self._leases.pop(name, None)
        if lease is not None:
            lease.close()

    @contextmanager
    def _reservation(self, name):
        """Keep active ownership; release a new reservation when an operation fails."""
        self._filename(name)
        owned = name in self._leases
        self.acquire(name)
        try:
            yield
        except BaseException:
            if not owned:
                self.release(name)
            raise

    def _filename(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise SessionError("session name must use 1–64 letters, digits, hyphens or underscores")
        return name + ".json"

    def _check_file(self, filename):
        info = self.handle.stat(filename)
        if not stat.S_ISREG(info.st_mode):
            raise SessionError("session file must be a regular owner-only (0600) file")
        try:
            self.handle.check_private(filename)
        except ValueError as exc:
            raise SessionError(str(exc)) from None
        return info

    def ensure_new(self, name):
        filename = self._filename(name)
        with self._reservation(name):
            try:
                self.handle.stat(filename)
            except FileNotFoundError:
                return
            raise SessionError("session already exists; use --resume or choose a new name")

    def save_new(self, name, messages, *, references=None, summary=None, logs=None):
        """Reserve and save a fresh name together, retaining ownership only on success."""
        with self._reservation(name):
            self.ensure_new(name)
            options = {"references": references} if references is not None else {}
            if summary is not None:
                options["summary"] = summary
            if logs is not None:
                options["logs"] = logs
            self.save(name, messages, **options)

    def save(self, name, messages, *, references=None, summary=None, logs=None):
        filename = self._filename(name)
        payload = {"version": 1, "binding": self.config.binding(), "messages": messages}
        if references is not None:
            try:
                selected = reference_state(references)
            except ValueError:
                raise SessionError("invalid file reference state; no data was overwritten") from None
            if selected["files"]:
                if redact(selected["files"], self.config.secrets) != selected["files"]:
                    raise SessionError("configured credentials cannot appear in saved file reference paths")
                payload["file_references"] = selected
        payload = redact(payload, self.config.secrets)
        if logs is not None:
            try:
                payload["command_logs"] = log_state(redact(logs, self.config.secrets))
            except ValueError:
                raise SessionError("invalid command log ownership; no data was overwritten") from None
        if summary is not None:
            try:
                payload["summary_checkpoint"] = summary_state(redact(summary, self.config.secrets), payload["messages"],
                                                               binding=self.config.binding())
            except ValueError:
                raise SessionError("invalid summary checkpoint; no data was overwritten") from None
        try:
            encoded_size(payload, max_bytes=MAX_SESSION, indent=2)
        except JsonBudgetExceeded:
            raise SessionError("session exceeds 8 MiB; no data was overwritten") from None
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        if len(body) > MAX_SESSION:
            raise SessionError("session exceeds 8 MiB; no data was overwritten")
        with self._reservation(name):
            try:
                self._check_file(filename)
            except FileNotFoundError:
                pass
            temp = "." + uuid.uuid4().hex + ".tmp"
            try:
                self.handle.write_new(temp, body, private=True)
                self.handle.replace(temp, filename)
                self.handle.sync()
            finally:
                try:
                    self.handle.unlink(temp)
                except FileNotFoundError:
                    pass

    def load(self, name):
        self.loaded_references = reference_state()
        self.loaded_summary = None
        self.loaded_logs = None
        filename = self._filename(name)
        with self._reservation(name):
            return self._load(filename)

    def _load(self, filename):
        self._check_file(filename)
        try:
            raw = self.handle.read(filename, MAX_SESSION, private=True)
        except ValueError as exc:
            raise SessionError(str(exc)) from None
        if len(raw) > MAX_SESSION:
            raise SessionError("session exceeds 8 MiB")
        try:
            saved = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            raise SessionError("invalid session JSON") from None
        if not isinstance(saved, dict) or saved.get("version") != 1:
            raise SessionError("unsupported session format")
        if saved.get("binding") != self.config.binding():
            raise SessionError("session endpoint/model/workdir changed; history will not be sent. Resume with the original settings or start a new session")
        history = redact(validate_history(saved.get("messages")), self.config.secrets)
        try:
            selected = reference_state(saved.get("file_references"))
            checkpoint = summary_state(saved.get("summary_checkpoint"), history, binding=self.config.binding())
            logs = log_state(saved.get("command_logs"))
        except ValueError:
            raise SessionError("invalid saved file reference, summary or command log state; no referenced files were read") from None
        self.loaded_references = selected
        self.loaded_summary = checkpoint
        self.loaded_logs = logs
        return history

    def list(self):
        """List local metadata without loading transcripts into a conversation."""
        return self.scan()["sessions"]

    def scan(self, *, cancel_event=None):
        result = self._scan(cancel_event=cancel_event)
        result["sessions"] = [{key: value for key, value in record.items() if not key.startswith("_")}
                              for record in result["sessions"]]
        return redact(result, self.config.secrets)

    @staticmethod
    def _stamp(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def _scan(self, *, cancel_event=None):
        """Bound discovery and reads, reporting limits without returning transcript text."""
        records, budget, scanned, reason = [], MAX_LIST_BYTES, 0, None

        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt

        check_cancelled()
        with self.handle.scandir() as entries:
            for entry in entries:
                check_cancelled()
                scanned += 1
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\.json", entry.name):
                    if scanned >= MAX_LIST_ENTRIES:
                        reason = "entries"
                        break
                    continue
                record = {"name": entry.name[:-5]}
                owned = record["name"] in self._leases
                try:
                    self.acquire(record["name"])
                    info = self._check_file(entry.name)
                    limit = min(MAX_SESSION, budget)
                    check_cancelled()
                    raw = self.handle.read(entry.name, limit, private=True)
                    budget -= len(raw)
                    check_cancelled()
                    if len(raw) > limit:
                        record["error"] = "session metadata read limit reached"
                        # A per-file limit also makes the result incomplete,
                        # even when a larger total read budget was configured.
                        reason = "bytes"
                    else:
                        saved = json.loads(raw)
                        if not isinstance(saved, dict) or saved.get("version") != 1 or not isinstance(saved.get("binding"), dict):
                            raise ValueError
                        history = validate_history(saved.get("messages"))
                        selected = reference_state(saved.get("file_references"))
                        checkpoint = summary_state(saved.get("summary_checkpoint"), history, binding=saved["binding"])
                        log_state(saved.get("command_logs"))
                        if self._stamp(self._check_file(entry.name)) != self._stamp(info):
                            raise ValueError("session changed during scan")
                        binding = {key: saved["binding"][key][:4096] for key in ("model", "base_url", "workdir")
                                   if isinstance(saved["binding"].get(key), str)}
                        record.update(binding=binding, messages=len(history), modified_ns=info.st_mtime_ns,
                                      bytes=info.st_size, _revision=self._stamp(info),
                                      _digest=hashlib.sha256(raw).digest(), _active=owned)
                        if selected["files"]:
                            record["reference_files"] = len(selected["files"])
                        if checkpoint is not None:
                            record["summary_source_end"] = checkpoint["end"]
                except SessionError as error:
                    if "busy" in str(error):
                        record["busy"] = True
                    else:
                        record["error"] = "invalid or unsafe session"
                except (OSError, ValueError, RecursionError):
                    record["error"] = "invalid or unsafe session"
                finally:
                    if not owned:
                        self.release(record["name"])
                records.append(record)
                if reason or budget <= 0:
                    reason = "bytes"
                    break
                if len(records) >= MAX_LIST_SESSIONS:
                    reason = "sessions"
                    break
                if scanned >= MAX_LIST_ENTRIES:
                    reason = "entries"
                    break
        check_cancelled()
        return {"sessions": sorted(records, key=lambda record: record["name"]),
                "limited": reason is not None, "limit_reason": reason,
                "scanned_entries": scanned, "read_bytes": MAX_LIST_BYTES - budget}

    def prune(self, approve, *, cancel_event=None, dry_run=False):
        """Explicit retention cleanup; scan completely, approve and recheck each idle file."""
        def check_cancelled():
            if cancel_event is not None and cancel_event.is_set():
                raise KeyboardInterrupt

        if self.config.read_only and not dry_run:
            return {"error": "read-only mode blocks session pruning", "deleted": []}
        age, count = self.config.session_max_age_days, self.config.session_max_count
        if age is None and count is None:
            return {"error": "configure storage.session_max_age_days or session_max_count before pruning", "deleted": []}
        listing = self._scan(cancel_event=cancel_event)
        if listing["limited"]:
            return {"error": "session scan incomplete; nothing deleted", "deleted": [],
                    "limited": True, "limit_reason": listing["limit_reason"]}
        records = sorted((record for record in listing["sessions"] if "_revision" in record),
                         key=lambda record: (-record["modified_ns"], record["name"]))
        cutoff = time.time_ns() - age * 86400 * 10**9 if age is not None else None
        candidates = [record for index, record in enumerate(records)
                      if not record["_active"] and ((count is not None and index >= count)
                        or (cutoff is not None and record["modified_ns"] < cutoff))]
        report = {"candidates": len(candidates), "deleted": [], "skipped": [], "declined": False,
                  "dry_run": dry_run,
                  "protected": sum(record.get("_active", False) or "_revision" not in record
                                   for record in listing["sessions"])}
        if dry_run:
            report["preview"] = [{key: record[key] for key in ("name", "bytes", "modified_ns")} for record in candidates]
            return redact(report, self.config.secrets)
        for record in candidates:
            check_cancelled()
            name, filename = record["name"], self._filename(record["name"])
            # Keep a live lease throughout review; another cooperating Sprig
            # process must not resume or overwrite a candidate under review.
            try:
                self.acquire(name)
            except (OSError, ValueError, SessionError):
                report["skipped"].append({"name": name, "reason": "busy_or_unsafe"})
                continue
            try:
                if self._stamp(self._check_file(filename)) != record["_revision"]:
                    report["skipped"].append({"name": name, "reason": "changed"})
                    continue
                prompt = ("DELETE retained local session " + name + "\n"
                          + json.dumps({"bytes": record["bytes"], "modified_ns": record["modified_ns"],
                                        "policy": {"max_age_days": age, "max_count": count}})
                          + "\nThis permanently deletes this transcript. Each candidate requires new approval.")
                if approve(redact(prompt, self.config.secrets)) is not True:
                    report["declined"] = True
                    break
                check_cancelled()
                info = self._check_file(filename)
                if self._stamp(info) != record["_revision"]:
                    report["skipped"].append({"name": name, "reason": "changed"})
                    continue
                raw = self.handle.read(filename, MAX_SESSION, private=True)
                check_cancelled()
                if (hashlib.sha256(raw).digest() != record["_digest"]
                        or self._stamp(self._check_file(filename)) != record["_revision"]):
                    report["skipped"].append({"name": name, "reason": "changed"})
                    continue
                self.handle.unlink(filename)
                report["deleted"].append(name)
                self.handle.sync()
            except (OSError, ValueError, SessionError):
                if name in report["deleted"]:
                    report["error"] = "session removed but directory sync failed; verify local storage before continuing"
                    break
                report["skipped"].append({"name": name, "reason": "changed_or_unsafe"})
            finally:
                self.release(name)
        check_cancelled()
        return redact(report, self.config.secrets)

    def delete(self, name):
        """Caller must obtain explicit approval; active leases exclude other writers."""
        with self._reservation(name):
            filename = self._filename(name)
            self._check_file(filename)
            self.handle.unlink(filename)
            self.handle.sync()
            self.release(name)
