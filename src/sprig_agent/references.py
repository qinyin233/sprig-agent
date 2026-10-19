"""Explicit live file context; persisted state contains paths and observations only."""

import os
from pathlib import PurePosixPath, PureWindowsPath
import re

from .config import redact
from .revisions import valid_read_id

MAX_FILES = 16
MAX_PATH_BYTES = 8192
MAX_SNAPSHOT = 32768
MAX_CONTENT = 131072


def reference_paths(paths):
    if not isinstance(paths, (list, tuple)) or len(paths) > MAX_FILES:
        raise ValueError("file references must contain at most 16 paths")
    selected, seen, total = [], set(), 0
    for value in paths:
        if (not isinstance(value, str) or not value or len(value) > 4096 or "\\" in value
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
                or len(value.encode("utf-8")) > 4096):
            raise ValueError("file references require relative POSIX paths of at most 4096 UTF-8 bytes")
        path = PurePosixPath(value)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("file reference escapes the workspace or does not name a file")
        value = path.as_posix()
        key = value.casefold() if os.name == "nt" else value
        if key not in seen:
            seen.add(key)
            selected.append(value)
            total += len(value.encode("utf-8"))
    if total > MAX_PATH_BYTES:
        raise ValueError("file reference paths exceed 8192 UTF-8 bytes")
    return tuple(selected)


def user_reference_paths(root, paths):
    """Normalize explicit UI/CLI paths lexically; never follow an alias here."""
    if not isinstance(paths, (list, tuple)) or len(paths) > MAX_FILES:
        raise ValueError("select at most 16 file references")
    values = []
    for value in paths:
        if not isinstance(value, str) or not value or len(value) > 32768:
            raise ValueError("a file reference path is required")
        if os.name == "nt":
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            if value.startswith(("\\\\?\\", "\\\\.\\")):
                raise ValueError("device paths cannot be file references")
            path, base = PureWindowsPath(value), PureWindowsPath(str(root))
            if path.drive or path.root:
                if (not path.is_absolute() or len(path.parts) <= len(base.parts)
                        or tuple(part.casefold() for part in path.parts[:len(base.parts)])
                        != tuple(part.casefold() for part in base.parts)):
                    raise ValueError("file reference must be inside the selected workspace")
                value = PurePosixPath(*path.parts[len(base.parts):]).as_posix()
            else:
                value = PurePosixPath(*path.parts).as_posix()
        else:
            path, base = PurePosixPath(value), PurePosixPath(str(root))
            if path.is_absolute():
                if len(path.parts) <= len(base.parts) or path.parts[:len(base.parts)] != base.parts:
                    raise ValueError("file reference must be inside the selected workspace")
                value = PurePosixPath(*path.parts[len(base.parts):]).as_posix()
        values.append(value)
    return reference_paths(values)


def reference_state(value=None):
    if value is None:
        return {"version": 1, "files": [], "observed": []}
    if (not isinstance(value, dict) or set(value) != {"version", "files", "observed"}
            or type(value["version"]) is not int or value["version"] != 1):
        raise ValueError("invalid file reference state")
    files = reference_paths(value["files"])
    rows = value["observed"]
    if not isinstance(rows, list) or len(rows) > MAX_FILES:
        raise ValueError("invalid file reference observations")
    output, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) - {"path", "bytes", "content_attached", "sha256"}
                or not {"path", "bytes", "content_attached"}.issubset(row)
                or row.get("path") not in files or row["path"] in seen
                or type(row.get("bytes")) is not int or not 0 <= row["bytes"] < 2**63
                or type(row.get("content_attached")) is not bool):
            raise ValueError("invalid file reference observation")
        digest = row.get("sha256")
        if "sha256" in row and (not row["content_attached"] or not isinstance(digest, str)
                                   or not re.fullmatch(r"[0-9a-f]{64}|\[REDACTED\]", digest)):
            raise ValueError("invalid file reference digest")
        output.append(dict(row))
        seen.add(row["path"])
    return {"version": 1, "files": list(files), "observed": output}


class FileReferences:
    def __init__(self, config, tools, check_cancelled=lambda: None):
        self.config, self.tools = config, tools
        self.check_cancelled = check_cancelled
        self.files = reference_paths(config.reference_files)
        self.observed = []

    def state(self):
        return reference_state({"version": 1, "files": list(self.files), "observed": self.observed})

    def restore(self, value):
        state = reference_state(value)
        self.files, self.observed = tuple(state["files"]), state["observed"]

    def _metadata(self, path):
        from .tools import ToolError
        self.check_cancelled()
        self.tools._check_cancelled()
        try:
            self.tools._require_tool("read_file")
            self.tools._parts(path)
            with self.tools._parent(path) as (parent, name), parent.open_read(name) as handle:
                info = os.fstat(handle.fileno())
            if not 0 <= info.st_size < 2**63:
                raise ValueError("referenced file size is unsupported")
            return info.st_size, self.tools._revision(info)
        except (OSError, ValueError, ToolError) as error:
            raise ValueError("cannot prepare referenced file: " + redact(str(error), self.config.secrets)) from None

    def prepare(self):
        frames, observed, revisions, total = [], [], [], 0
        for path in self.files:
            self.check_cancelled()
            self.tools._check_cancelled()
            if any(secret in path for secret in self.config.secrets):
                raise ValueError("configured credentials cannot appear in file reference paths")
            size, revision = self._metadata(path)
            frame = {"path": path, "bytes": size, "content_attached": False}
            row = dict(frame)
            if size <= min(MAX_SNAPSHOT, self.config.max_output):
                result = self.tools.execute("read_file", {"path": path, "offset": 1, "limit": 10000, "line_numbers": False})
                if "error" in result:
                    raise ValueError("cannot prepare referenced file: " + result["error"])
                content = result["content"]
                length = len(content.encode("utf-8"))
                if not result.get("truncated") and length <= min(MAX_SNAPSHOT, self.config.max_output):
                    total += length
                    if total > MAX_CONTENT:
                        raise ValueError("live reference contents exceed 128 KiB; remove references or use targeted reads")
                    frame.update(content_attached=True, content=content)
                    row["content_attached"] = True
                    if "sha256" in result:
                        frame["sha256"] = row["sha256"] = result["sha256"]
                        if valid_read_id(result.get("read_id")):
                            frame["read_id"] = result["read_id"]
            if not frame["content_attached"]:
                from .tools import MAX_READ
                if size > MAX_READ:
                    frame["read_file_scan_limit_exceeded"] = True
                frame["note"] = ("Path/size reference only; no file content, hash or read ID attached. "
                                 "Use targeted read_file within its scan limits; commands still require approval.")
            frames.append(frame)
            observed.append(row)
            revisions.append((path, size, revision))
        # Detect ordinary changes/privacy revocations during preparation. These
        # snapshots are observations, not a multi-file atomic read or approval.
        for path, size, revision in revisions:
            if self._metadata(path) != (size, revision):
                raise ValueError("referenced file changed while preparing context; no request sent")
        return frames, observed
