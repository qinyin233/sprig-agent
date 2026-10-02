"""Bounded, approval-first tools; commands are NOT sandboxed."""

from contextlib import contextmanager
import ast
import difflib
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
from .filesystem import Directory, is_link
from .policy import FilePolicy, command_environment
from .output import CommandOutput

MAX_FILE = 512 * 1024
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
    schema("read_file", "Read a UTF-8 workspace file; output may be truncated.",
           {"path": STRING}, ["path"]),
    schema("search", "Find literal text recursively in UTF-8 files, with bounded results.",
           {"query": STRING, "path": STRING}, ["query"]),
    schema("project_map", "Bounded workspace file map with Python class/function names and line numbers; inspect relevant files afterwards.",
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
    def __init__(self, config, approve, cancel_event=None, emit_output=None):
        self.config, self.approve = config, approve
        self.cancel_event = cancel_event
        self.root = Directory(config.workdir)
        self.policy = FilePolicy(self.root)
        self.emit_output = emit_output

    def _check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise KeyboardInterrupt

    def close(self):
        self.root.close()

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
        except (OSError, ToolError, TypeError, ValueError) as exc:
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

    def read_file(self, path):
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
                            for line_number, line in enumerate(self._decode(raw).splitlines(), 1):
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
                        if entry.name.endswith(".py"):
                            raw = self._read(directory, entry.name, min(MAX_FILE, budget))
                            budget -= len(raw)
                            if len(raw) > MAX_FILE or budget < 0:
                                truncated = True
                                record["symbols_unavailable"] = True
                            else:
                                try:
                                    tree = ast.parse(self._decode(raw))
                                    symbols, pending = [], [(tree.body, "")]
                                    while pending:
                                        nodes, parent = pending.pop()
                                        for node in nodes:
                                            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                                                name = parent + node.name
                                                symbols.append({"name": name, "line": node.lineno,
                                                                "kind": "class" if isinstance(node, ast.ClassDef) else "function"})
                                                if isinstance(node, ast.ClassDef):
                                                    pending.append((node.body, name + "."))
                                    record["symbols"] = sorted(symbols, key=lambda item: item["line"])
                                except (SyntaxError, ValueError, RecursionError, ToolError):
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

    def edit_file(self, path, old, new):
        if self.config.read_only:
            return {"approved": False, "changed": False, "error": "read-only mode blocks file writes"}
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("old and new must be strings")
        if len(old.encode("utf-8")) > MAX_FILE or len(new.encode("utf-8")) > MAX_FILE:
            raise ToolError("edit exceeds 512 KiB")
        with self._parent(path) as (fd, name):
            try:
                before_raw = self._read(fd, name, MAX_FILE)
                before_stat = fd.stat(name)
            except FileNotFoundError:
                before_raw, before_stat = b"", None
            if len(before_raw) > MAX_FILE:
                raise ToolError("file exceeds 512 KiB")
            before = self._decode(before_raw)
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
            if before_stat is not None and before == after:
                return {"changed": False}
            diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
                           for line in difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                                           fromfile=path if before_stat else "/dev/null", tofile=path))
            if not self.approve(f"WRITE {json.dumps(path)}\n{diff or '(create empty file)'}"):
                return {"approved": False, "changed": False}
            # Recheck after approval: an editor may have changed the file while the user read the diff.
            try:
                current_stat = fd.stat(name)
            except FileNotFoundError:
                current_stat = None
            if self._revision(current_stat) != self._revision(before_stat) or (current_stat and self._read(fd, name, MAX_FILE) != before_raw):
                raise ToolError("file changed during approval; request a fresh diff")
            self._check_cancelled()
            temp = f".sprig-{uuid.uuid4().hex}.tmp"
            try:
                fd.write_new(temp, data, stat.S_IMODE(before_stat.st_mode) & 0o777 if before_stat else 0o600,
                             permissions_from=name if before_stat else None)
                fd.replace(temp, name)
            finally:
                try:
                    fd.unlink(temp)
                except FileNotFoundError:
                    pass
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
