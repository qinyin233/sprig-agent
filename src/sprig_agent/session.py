"""Explicit, private, atomic local transcripts. Loading never runs an action."""

import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from .config import redact
from .filesystem import Directory
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
        # A nested workdir must not allow storing transcripts elsewhere in the same repo.
        forbidden = [config.workdir]
        for parent in (config.workdir, *config.workdir.parents):
            marker = parent / ".git"
            if marker.is_file() or (marker / "HEAD").is_file():
                forbidden.append(parent)
        resolved = self.directory.resolve()
        if any(resolved.is_relative_to(root) for root in forbidden):
            raise SessionError("session directory must be outside the workspace/repository")
        directory_handle = Directory(self.directory.anchor)
        try:
            for part in self.directory.parts[1:]:
                try:
                    child = directory_handle.child(part)
                except FileNotFoundError:
                    try:
                        directory_handle.mkdir(part)
                    except FileExistsError:
                        pass  # Another process may have created the same directory.
                    child = directory_handle.child(part)
                directory_handle.close()
                directory_handle = child
            directory_handle.check_private()
            self.handle, directory_handle = directory_handle, None
        except ValueError as exc:
            raise SessionError(str(exc)) from None
        finally:
            if directory_handle is not None:
                directory_handle.close()

    def close(self):
        self.handle.close()

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
        try:
            self.handle.stat(filename)
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
            self.handle.write_new(temp, body, private=True)
            self.handle.replace(temp, filename)
            self.handle.sync()
        finally:
            try:
                self.handle.unlink(temp)
            except FileNotFoundError:
                pass

    def load(self, name):
        filename = self._filename(name)
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
        return redact(validate_history(saved.get("messages")), self.config.api_key)
