"""Five tools. File access uses no-follow directory FDs; commands are NOT sandboxed."""

from contextlib import contextmanager
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

MAX_FILE = 512 * 1024
SKIP_DIRS = {"node_modules", ".venv", "__pycache__"}


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
    def __init__(self, config, approve):
        self.config, self.approve = config, approve
        self.root_fd = os.open(config.workdir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        if self.root_fd is not None:
            os.close(self.root_fd)
            self.root_fd = None

    def _parts(self, path):
        if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path or "\\" in path:
            raise ToolError("path must be a nonempty relative POSIX path")
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or any(protected(p) for p in parsed.parts):
            raise ToolError("path escapes workdir or names a protected component")
        return parsed.parts

    @contextmanager
    def _directory(self, parts):
        fd = os.dup(self.root_fd)
        try:
            for part in parts:
                new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = new_fd
            yield fd
        finally:
            os.close(fd)

    @contextmanager
    def _parent(self, path):
        parts = self._parts(path)
        if not parts:
            raise ToolError("a file path is required")
        with self._directory(parts[:-1]) as fd:
            yield fd, parts[-1]

    def _read(self, parent, name, limit):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ToolError("only regular files are supported")
            return handle.read(limit + 1)

    def _decode(self, data):
        if b"\0" in data:
            raise ToolError("binary files are not supported")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise ToolError("file must be UTF-8 text") from None

    def execute(self, name, arguments):
        try:
            if not isinstance(arguments, dict):
                raise ToolError("tool arguments must be an object")
            method = {"list_files": self.list_files, "read_file": self.read_file,
                      "search": self.search, "edit_file": self.edit_file, "run": self.run}.get(name)
            if method is None:
                raise ToolError("unknown tool")
            result = method(**arguments)
            return redact(result, self.config.api_key)
        except (OSError, ToolError, TypeError, ValueError) as exc:
            return {"error": redact(str(exc), self.config.api_key)}

    def list_files(self, path="."):
        entries, size, truncated = [], 0, False
        with self._directory(self._parts(path)) as fd, os.scandir(fd) as iterator:
            for entry in iterator:
                if protected(entry.name) or entry.is_symlink():
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
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    scanned += 1
                    if scanned > 5000 or budget <= 0 or output_size >= self.config.max_output:
                        truncated = True
                        return
                    if protected(entry.name) or entry.is_symlink():
                        continue
                    relative = "/".join((*prefix, entry.name))
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in SKIP_DIRS:
                                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                                try:
                                    walk(child, (*prefix, entry.name), depth + 1)
                                finally:
                                    os.close(child)
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
                    except (OSError, ToolError):
                        continue  # Files may disappear or be unreadable/binary during traversal.

        with self._directory(parts) as fd:
            walk(fd, parts, 0)
        return {"matches": matches, "truncated": truncated}

    def edit_file(self, path, old, new):
        if not isinstance(old, str) or not isinstance(new, str):
            raise ToolError("old and new must be strings")
        if len(old.encode("utf-8")) > MAX_FILE or len(new.encode("utf-8")) > MAX_FILE:
            raise ToolError("edit exceeds 512 KiB")
        with self._parent(path) as (fd, name):
            try:
                before_raw = self._read(fd, name, MAX_FILE)
                before_stat = os.stat(name, dir_fd=fd, follow_symlinks=False)
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
                current_stat = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                current_stat = None
            if current_stat != before_stat or (current_stat and self._read(fd, name, MAX_FILE) != before_raw):
                raise ToolError("file changed during approval; request a fresh diff")
            temp = f".sprig-{uuid.uuid4().hex}.tmp"
            try:
                temp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                with os.fdopen(temp_fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    if before_stat:
                        os.fchmod(handle.fileno(), stat.S_IMODE(before_stat.st_mode) & 0o777)
                    os.fsync(handle.fileno())
                os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(temp, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return {"approved": True, "changed": True, "path": path}

    def run(self, argv, cwd=".", timeout=None):
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
        # Unlike file tools, the approved program controls its own filesystem and network access.
        with self._directory(parts):
            pass
        env = {key: value for key, value in os.environ.items()
               if key not in {"SPRIG_API_KEY", "OPENAI_API_KEY"}
               and not (self.config.api_key and self.config.api_key in value)}
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
                            if len(chunk) > space:
                                reason = "output_limit"
                                break
                    if reason:
                        break
                if not reason:
                    try:
                        process.wait(timeout=max(0.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        reason = "timeout"
        finally:
            # Also clean up descendants that inherited the process group / pipe.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
        return {"approved": True, "returncode": process.returncode,
                "output": output.decode("utf-8", errors="replace"),
                "timed_out": reason == "timeout", "output_limited": reason == "output_limit"}
