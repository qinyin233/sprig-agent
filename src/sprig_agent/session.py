"""Explicit, private, atomic local transcripts. Loading never runs an action."""

import json
import os
from pathlib import Path
import re
import stat
import uuid

from .config import redact
from .provider import ProviderError, validate_message

MAX_SESSION = 8 * 1024 * 1024


class SessionError(Exception):
    pass


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
                normalized = validate_message(message)
            except ProviderError as exc:
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
    def __init__(self, directory, config):
        if os.name != "posix":
            raise SessionError("private sessions require POSIX owner-only permissions")
        self.directory = Path(directory).expanduser().absolute()
        self.config = config
        # A nested workdir must not allow storing transcripts elsewhere in the same repo.
        forbidden = [config.workdir]
        for parent in (config.workdir, *config.workdir.parents):
            marker = parent / ".git"
            if marker.is_file() or (marker / "HEAD").is_file():
                forbidden.append(parent)
        resolved = self.directory.resolve()
        if any(resolved.is_relative_to(root) for root in forbidden):
            raise SessionError("session directory must be outside the workspace/repository")
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in self.directory.parts[1:]:
                try:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise SessionError("session directory must be owned by you with mode 0700")
            self.fd, fd = fd, None
        finally:
            if fd is not None:
                os.close(fd)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _filename(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name):
            raise SessionError("session name must use 1–64 letters, digits, hyphens or underscores")
        return name + ".json"

    def _check_file(self, filename):
        info = os.stat(filename, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise SessionError("session file must be a regular owner-only (0600) file")
        return info

    def ensure_new(self, name):
        filename = self._filename(name)
        try:
            os.stat(filename, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise SessionError("session already exists; use --resume or choose a new name")

    def save(self, name, messages):
        filename = self._filename(name)
        body = json.dumps(redact({"version": 1, "binding": self.config.binding(), "messages": messages},
                                 self.config.api_key), ensure_ascii=False, indent=2).encode("utf-8")
        if len(body) > MAX_SESSION:
            raise SessionError("session exceeds 8 MiB; no data was overwritten")
        try:
            self._check_file(filename)
        except FileNotFoundError:
            pass
        temp = "." + uuid.uuid4().hex + ".tmp"
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, filename, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temp, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def load(self, name):
        filename = self._filename(name)
        self._check_file(filename)
        fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise SessionError("unsafe session file")
            raw = handle.read(MAX_SESSION + 1)
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
        return redact(validate_history(saved.get("messages")), self.config.api_key)
