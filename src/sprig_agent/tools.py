"""Bounded, approval-first tools; commands are NOT sandboxed."""

from contextlib import contextmanager
import difflib
import io
import json
import math
import os
from pathlib import PurePosixPath
import selectors
import signal
import stat
import subprocess
import time
import uuid

from .config import redact
from .changes import ChangeError, ChangeHistory, unpack
from .filesystem import Directory, is_link
from .index import SymbolIndex, language_for
from .policy import FilePolicy, command_environment
from .output import CommandOutput

MAX_FILE = 512 * 1024
MAX_READ = 8 * 1024 * 1024
SKIP_DIRS = {"node_modules", ".venv", "__pycache__"}


def skip_directory(name):
    return name in SKIP_DIRS or name.startswith(".venv-")


def schema(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(required), "additionalProperties": False}}}


STRING = {"type": "string"}
TOOL_SCHEMAS = [
    schema("list_files", "List one workspace directory; no symlinks or protected files.",
           {"path": STRING}),
    schema("read_file", "Read UTF-8 text. Use 1-based offset and limit to inspect specific lines or continue a large file. Ranged reads scan at most 8 MiB, enforce file privacy, and report next_offset; a single oversized line is explicitly marked incomplete.",
           {"path": STRING, "offset": {"type": "integer", "minimum": 1, "maximum": 1000000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["path"]),
    schema("search", "Find literal text recursively in UTF-8 files, with bounded results.",
           {"query": STRING, "path": STRING}, ["query"]),
    schema("project_map", "Bounded workspace file map with declarations and 1-based lines for Python, JavaScript, TypeScript/TSX, Rust, Go, C/C++ and Java. Syntax only; inspect relevant files afterwards. Unsupported files have paths only; unavailable or oversized parses are marked.",
           {"path": STRING}),
    schema("edit_file", "Replace exactly one occurrence after diff approval. Empty old creates a new file only. Parent directory must exist.",
           {"path": STRING, "old": STRING, "new": STRING}, ["path", "old", "new"]),
    schema("run", "Run argv after explicit approval. NOT sandboxed; can access network and files outside workspace.",
           {"argv": {"type": "array", "items": STRING, "minItems": 1}, "cwd": STRING,
            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 120}}, ["argv"]),
]


class ToolError(Exception):
    pass


def protected(name):
    return name == ".git" or name == ".env" or name.startswith(".env.")


class Tools:
    def __init__(self, config, approve, cancel_event=None, emit_output=None, history=None):
        self.config, self.approve = config, approve
        self.cancel_event = cancel_event
        self.root = Directory(config.workdir)
        self.history = history or ChangeHistory(config)
        self.policy = FilePolicy(self.root)
        self.index = SymbolIndex()
        self.emit_output = emit_output

    def _check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise KeyboardInterrupt

    def close(self):
        self.root.close()
        self.history.close()
        self.index.clear()

    def _parts(self, path):
        if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path or "\\" in path:
            raise ToolError("path must be a nonempty relative POSIX path")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or any(protected(p.lower() if os.name == "nt" else p) for p in parsed.parts):
            raise ToolError("path escapes workdir or names a protected component")
        self.policy.reload()
        if self.policy.excluded(parsed.parts):
            raise ToolError("path is excluded by file privacy policy")
        if os.name == "nt":
            from .windows import validate_component
            for part in parsed.parts:
                validate_component(part)
        return parsed.parts

    @contextmanager
    def _directory(self, parts):
        directory = self.root
        opened = []
        try:
            for part in parts:
                directory = directory.child(part)
                opened.append(directory)
            yield directory
        finally:
            for directory in reversed(opened):
                directory.close()

    @contextmanager
    def _parent(self, path):
        parts = self._parts(path)
        if not parts:
            raise ToolError("a file path is required")
        with self._directory(parts[:-1]) as fd:
            yield fd, parts[-1]

    def _read(self, parent, name, limit):
        return parent.read(name, limit)

    @staticmethod
    def _revision(info):
        if info is None:
            return None
        # Reading during review can change atime without changing the file.
        return tuple(getattr(info, name, None) for name in (
            "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid", "st_size",
            "st_mtime_ns", "st_ctime_ns", "st_file_attributes"))

    def _decode(self, data):
        if b"\0" in data:
            raise ToolError("binary files are not supported")
        try:
            text = data.decode("utf-8")
            self.policy.check_text(text)
            return text
        except UnicodeDecodeError:
            raise ToolError("file must be UTF-8 text") from None

    def execute(self, name, arguments):
        self._check_cancelled()
        try:
            if not isinstance(arguments, dict):
                raise ToolError("tool arguments must be an object")
            method = {"list_files": self.list_files, "read_file": self.read_file,
                      "search": self.search, "project_map": self.project_map,
                      "edit_file": self.edit_file, "run": self.run}.get(name)
            if method is None:
                raise ToolError("unknown tool")
            result = method(**arguments)
            return redact(result, self.config.api_key)
        except (OSError, ToolError, ChangeError, TypeError, ValueError) as exc:
            return {"error": redact(str(exc), self.config.api_key)}

    def list_files(self, path="."):
        entries, size, truncated = [], 0, False
        parts = self._parts(path)
        with self._directory(parts) as fd, fd.scandir() as iterator:
            for entry in iterator:
                if self.policy.excluded((*parts, entry.name)) or is_link(entry):
                    continue
                kind = "dir" if entry.is_dir(follow_symlinks=False) else "file"
                size += len(entry.name.encode("utf-8")) + 30
                if size > self.config.max_output:
                    truncated = True
                    break
                entries.append({"name": entry.name, "type": kind})
        return {"entries": sorted(entries, key=lambda item: item["name"]), "truncated": truncated}

    def read_file(self, path, offset=1, limit=None):
        if type(offset) is not int or not 1 <= offset <= 1000000:
            raise ToolError("offset must be an integer from 1 to 1000000 (1-based line number)")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ToolError("limit must be an integer from 1 to 10000")
        if offset != 1 or limit is not None:
            return self._read_page(path, offset, limit or 2000)
        with self._parent(path) as (fd, name):
            raw = self._read(fd, name, self.config.max_output)
        truncated = len(raw) > self.config.max_output
        if truncated:
            # A byte cap may bisect a UTF-8 character. Ignore only that trailing fragment.
            import codecs
            decoder = codecs.getincrementaldecoder("utf-8")()
            text = decoder.decode(raw[:self.config.max_output], final=False)
            if "\0" in text:
                raise ToolError("binary files are not supported")
            self.policy.check_text(text)
        else:
            text = self._decode(raw)
        return {"path": path, "content": text, "truncated": truncated}

    def _read_page(self, path, offset, limit):
        # Validate the whole bounded file before exposing a page: selecting a
        # clean line must not bypass the credential/binary policy elsewhere.
        raw = bytearray()
        with self._parent(path) as (directory, name), directory.open_read(name) as handle:
            if os.fstat(handle.fileno()).st_size > MAX_READ:
                raise ToolError("ranged read exceeds the 8 MiB scan limit")
            while True:
                self._check_cancelled()
                chunk = handle.read(min(65536, MAX_READ - len(raw) + 1))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_READ:
                    raise ToolError("ranged read exceeds the 8 MiB scan limit")
        self._check_cancelled()
        selected, size, partial, capped, total = [], 0, False, False, 0
        # Iterate rather than allocating one list entry per line for a file
        # containing millions of blank lines. Keep CR/LF endings unchanged.
        for line in io.StringIO(self._decode(raw), newline=""):
            total += 1
            if total % 256 == 0:
                self._check_cancelled()
            if total < offset or total >= offset + limit or capped:
                continue
            encoded = line.encode("utf-8")
            if size + len(encoded) > self.config.max_output:
                if not selected:
                    import codecs
                    selected.append(codecs.getincrementaldecoder("utf-8")().decode(
                        encoded[:self.config.max_output], final=False))
                    partial = True
                capped = True
                continue
            selected.append(line)
            size += len(encoded)
        self._check_cancelled()
        if offset > total and not (offset == 1 and not total):
            raise ToolError(f"offset {offset} is beyond the end of the file ({total} lines)")
        end = offset - 1 + len(selected)
        truncated = partial or end < total
        return {"path": path, "content": "".join(selected), "offset": offset, "end_line": end,
                "total_lines": total, "truncated": truncated, "line_truncated": partial,
                "next_offset": end + 1 if truncated and not partial else None}

    def search(self, query, path="."):
        if not isinstance(query, str) or not query or len(query) > 1000:
            raise ToolError("query must be a nonempty literal string (at most 1000 characters)")
        parts = self._parts(path)
        matches, scanned, budget, output_size, truncated = [], 0, 8 * 1024 * 1024, 0, False

        def walk(fd, prefix, depth):
            nonlocal scanned, budget, output_size, truncated
            if depth > 32:
                truncated = True
                return
            with fd.scandir() as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > 5000 or budget <= 0 or output_size >= self.config.max_output:
                        truncated = True
                        return
                    if self.policy.excluded((*prefix, entry.name)) or is_link(entry):
                        continue
                    relative = "/".join((*prefix, entry.name))
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not skip_directory(entry.name):
                                with fd.child(entry.name) as child:
                                    walk(child, (*prefix, entry.name), depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            raw = self._read(fd, entry.name, min(MAX_FILE, budget))
                            budget -= len(raw)
                            if len(raw) > MAX_FILE or budget < 0:
                                truncated = True
                                continue
                            for line_number, line in enumerate(io.StringIO(self._decode(raw), newline=""), 1):
                                line = line.rstrip("\r\n")
                                if query in line:
                                    match = {"path": relative, "line": line_number, "text": line}
                                    size = len(json.dumps(match).encode("utf-8"))
                                    if output_size + size > self.config.max_output:
                                        truncated = True
                                        output_size = self.config.max_output
                                        return
                                    matches.append(match)
                                    output_size += size
                    except (OSError, ToolError, ValueError):
                        continue  # Files may disappear or be unreadable/binary during traversal.

        with self._directory(parts) as fd:
            walk(fd, parts, 0)
        return {"matches": matches, "truncated": truncated}

    def project_map(self, path="."):
        parts = self._parts(path)
        files, scanned, budget, size, truncated = [], 0, 8 * 1024 * 1024, 0, False

        def walk(directory, prefix, depth):
            nonlocal scanned, budget, size, truncated
            if depth > 32:
                truncated = True
                return
            with directory.scandir() as iterator:
                for entry in iterator:
                    self._check_cancelled()
                    scanned += 1
                    if scanned > 5000 or budget <= 0 or size >= self.config.max_output:
                        truncated = True
                        return
                    if self.policy.excluded((*prefix, entry.name)) or is_link(entry):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not skip_directory(entry.name):
                                with directory.child(entry.name) as child:
                                    walk(child, (*prefix, entry.name), depth + 1)
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        record = {"path": "/".join((*prefix, entry.name))}
                        language = language_for(entry.name)
                        if language:
                            raw = self._read(directory, entry.name, min(MAX_FILE, budget))
                            self._check_cancelled()
                            budget -= len(raw)
                            if len(raw) > MAX_FILE or budget < 0:
                                truncated = True
                                record["symbols_unavailable"] = True
                            else:
                                try:
                                    symbols = self.index.symbols(self._decode(raw), language, self._check_cancelled)
                                    self._check_cancelled()
                                    if symbols is None:
                                        record["symbols_unavailable"] = True
                                    else:
                                        record["symbols"] = symbols
                                except (ValueError, ToolError):
                                    record["symbols_unavailable"] = True
                        record_size = len(json.dumps(record, ensure_ascii=False).encode("utf-8")) + 2
                        if size + record_size > self.config.max_output:
                            # Keep a large module discoverable even when its symbol list is too big.
                            record = {"path": record["path"], "symbols_unavailable": True}
                            record_size = len(json.dumps(record, ensure_ascii=False).encode("utf-8")) + 2
                            truncated = True
                        if size + record_size > self.config.max_output:
                            return
                        files.append(record)
                        size += record_size
                    except (OSError, ToolError):
                        truncated = True

        with self._directory(parts) as directory:
            walk(directory, parts, 0)
        return {"files": sorted(files, key=lambda item: item["path"]), "truncated": truncated}

    @staticmethod
    def _diff(path, before, after):
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                       for line in difflib.unified_diff((before or "").splitlines(True), (after or "").splitlines(True),
                                                       fromfile=path if before is not None else "/dev/null",
                                                       tofile=path if after is not None else "/dev/null"))

    def _snapshot(self, fd, name):
        try:
            data, info = self._read(fd, name, MAX_FILE), fd.stat(name)
        except FileNotFoundError:
            return None, None
        if len(data) > MAX_FILE:
            raise ToolError("file exceeds 512 KiB")
        return data, info

    def _unchanged(self, fd, name, before_raw, before_stat):
        data, info = self._snapshot(fd, name)
        if self._revision(info) != self._revision(before_stat) or data != before_raw:
            raise ToolError("file changed during approval; request a fresh diff")

    def _replace(self, fd, name, data, mode, before_stat):
        if data is None:
            fd.unlink(name)
        else:
            temp = f".sprig-{uuid.uuid4().hex}.tmp"
            try:
                fd.write_new(temp, data, mode, permissions_from=name if before_stat else None)
                fd.replace(temp, name)
            finally:
                try:
                    fd.unlink(temp)
                except FileNotFoundError:
                    pass
        fd.sync()

    def change_action(self, action):
        """Local user controls only; deliberately absent from model tool schemas."""
        try:
            self._check_cancelled()
            if action == "changes":
                result = self.history.summary()
            elif action == "discard-history":
                description = "DISCARD exact edit backups (workspace files unchanged). Undo/redo will no longer be available.\n" + json.dumps(self.history.summary(), ensure_ascii=False)
                if not self.approve(description):
                    result = {"approved": False}
                else:
                    self._check_cancelled()
                    self.history.discard()
                    result = {"approved": True, "discarded": True}
            else:
                result = self._restore_change(action)
            return redact(result, self.config.api_key)
        except (OSError, ToolError, ChangeError, ValueError) as exc:
            return {"error": redact(str(exc), self.config.api_key)}

    def _restore_change(self, action):
        if self.config.read_only:
            return {"approved": False, "changed": False, "error": "read-only mode blocks undo/redo"}
        record, revision = self.history.plan(action)
        path = record["path"]
        expected = unpack(record["after"] if action == "undo" else record["before"])
        target = unpack(record["before"] if action == "undo" else record["after"])
        with self._parent(path) as (fd, name):
            current, info = self._snapshot(fd, name)
            if current != expected or self._revision(info) != revision:
                raise ToolError("file changed outside the recorded edit; undo/redo blocked to preserve external changes")
            before = None if current is None else self._decode(current)
            after = None if target is None else self._decode(target)
            if self.config.api_key and any(self.config.api_key in (text or "") for text in (before, after)):
                raise ToolError("configured API key must not appear in change backups")
            description = action.upper() + " " + json.dumps(path) + "\n" + (self._diff(path, before, after) or "(empty file create/delete)")
            if not self.approve(description):
                return {"approved": False, "changed": False}
            self._parts(path)  # File privacy rules may have changed during review.
            self._unchanged(fd, name, current, info)
            self._check_cancelled()
            try:
                self.history.begin_restore(action, path)
                self._replace(fd, name, target, record["mode"], info)
                self.history.complete(self._revision(fd.stat(name)) if target is not None else None)
            except (OSError, ChangeError) as error:
                return {"approved": True, "changed": False, "outcome": "uncertain", "path": path, "action": action,
                        "error": "Restore outcome is uncertain; inspect current files and the pending backup before continuing: " + str(error)}
        return {"approved": True, "changed": True, "path": path, "action": action,
                "note": "Workspace changed; previous model conclusions and acceptance results need rechecking."}

    def edit_file(self, path, old, new):
        if self.config.read_only:
            return {"approved": False, "changed": False, "error": "read-only mode blocks file writes"}
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("old and new must be strings")
        if len(old.encode("utf-8")) > MAX_FILE or len(new.encode("utf-8")) > MAX_FILE:
            raise ToolError("edit exceeds 512 KiB")
        with self._parent(path) as (fd, name):
            before_raw, before_stat = self._snapshot(fd, name)
            before = self._decode(before_raw or b"")
            if before_stat is None:
                if old:
                    raise ToolError("new files require old to be empty")
                after = new
            else:
                if not old or before.count(old) != 1:
                    raise ToolError("old must match exactly once in an existing file")
                after = before.replace(old, new, 1)
            data = after.encode("utf-8")
            if len(data) > MAX_FILE:
                raise ToolError("result exceeds 512 KiB")
            self._decode(data)
            if self.config.api_key and (self.config.api_key in before or self.config.api_key in after):
                raise ToolError("configured API key must not appear in change backups")
            if before_stat is not None and before == after:
                return {"changed": False}
            diff = self._diff(path, before if before_stat else None, after)
            if not self.approve(f"WRITE {json.dumps(path)}\n{diff or '(create empty file)'}"):
                return {"approved": False, "changed": False}
            # Recheck after approval: an editor may have changed the file while the user read the diff.
            self._parts(path)
            self._unchanged(fd, name, before_raw, before_stat)
            self._check_cancelled()
            mode = stat.S_IMODE(before_stat.st_mode) & 0o777 if before_stat else 0o600
            self.history.begin_edit(path, before_raw, data, mode, self._revision(before_stat))
            self._replace(fd, name, data, mode, before_stat)
            self.history.complete(self._revision(fd.stat(name)))
        return {"approved": True, "changed": True, "path": path}

    def run(self, argv, cwd=".", timeout=None):
        if self.config.read_only:
            return {"approved": False, "error": "read-only mode blocks command execution"}
        if (not isinstance(argv, list) or not argv or len(argv) > 256
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]):
            raise ToolError("argv must be a nonempty array of strings (at most 256)")
        if sum(len(arg) for arg in argv) > 65536:
            raise ToolError("argv is too large")
        if self.config.api_key and any(self.config.api_key in arg for arg in argv):
            raise ToolError("configured API key must not appear in command arguments")
        seconds = self.config.timeout if timeout is None else timeout
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 0 < seconds <= self.config.timeout):
            raise ToolError("command timeout must be positive and no larger than the configured timeout")
        parts = self._parts(cwd)
        with self._directory(parts):
            pass
        directory = self.config.workdir.joinpath(*parts)
        description = (f"RUN (NOT SANDBOXED; may access outside files/network)\n"
                       f"cwd: {json.dumps(str(directory))}\nargv: {json.dumps(argv)}\n"
                       f"timeout: {seconds}s; output cap: {self.config.max_output} bytes")
        if not self.approve(description):
            return {"approved": False}
        self._check_cancelled()
        # Unlike file tools, the approved program controls its own filesystem and network access.
        self._parts(cwd)
        with self._directory(parts):
            pass
        env = command_environment(self.config)
        stream = CommandOutput(self.emit_output, self.config.api_key)
        if os.name == "nt":
            from .windows import run_command
            result = run_command(argv, directory, env, seconds, self.config.max_output, self.cancel_event, stream.write)
            stream.finish()
            if self.emit_output:
                result["streamed"] = True
            return result
        process = subprocess.Popen(argv, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   start_new_session=True, shell=False)
        output = bytearray()
        reason = None
        deadline = time.monotonic() + seconds
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    self._check_cancelled()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        reason = "timeout"
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), min(65536, self.config.max_output - len(output) + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            space = self.config.max_output - len(output)
                            output.extend(chunk[:space])
                            stream.write(chunk[:space])
                            if len(chunk) > space:
                                reason = "output_limit"
                                break
                    if reason:
                        break
                if not reason:
                    while process.poll() is None:
                        self._check_cancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            reason = "timeout"
                            break
                        try:
                            process.wait(timeout=min(0.1, remaining))
                        except subprocess.TimeoutExpired:
                            pass
        finally:
            # Also clean up descendants that inherited the process group / pipe.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
        stream.finish()
        return {"approved": True, "returncode": process.returncode, "streamed": bool(self.emit_output),
                "output": output.decode("utf-8", errors="replace"),
                "timed_out": reason == "timeout", "output_limited": reason == "output_limit"}
