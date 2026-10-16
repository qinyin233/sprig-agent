"""Bounded, approval-first tools; commands are NOT sandboxed."""

from contextlib import contextmanager
import difflib
import hashlib
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

from .config import TOOL_NAMES, redact
from .changes import ChangeError, ChangeHistory, unpack
from .filesystem import Directory, is_link
from .index import SymbolIndex, language_for
from .instructions import InstructionError, ProjectInstructions
from .planning import TaskPlan, validate_plan
from .revisions import READ_ID_PATTERN, ReadVersions, valid_read_id
from .map_selection import MapSelection, encoded_size
from .policy import FilePolicy, command_environment
from .output import CommandOutput
from .edit_match import failure_details
from .line_edits import replace_lines, validate_edits
from .search import FileGlob, RegexBudgetExceeded, RegexMatcher, matching_lines
from .syntax import IndexUnavailable

MAX_FILE = 512 * 1024
MAX_READ = 8 * 1024 * 1024
MAX_EDITS = 32
_MISSING = object()
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
    schema("read_file", "Read UTF-8, physical 1-based lines. Validated full/ranged reads return sha256 and short read_id for edit_lines. Ranges scan <=8 MiB. Byte-truncated prefixes/partial lines have no read_id. Inspect complete relevant lines before editing.",
           {"path": STRING, "offset": {"type": "integer", "minimum": 1, "maximum": 1000000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000}}, ["path"]),
    schema("search", "Search literal text recursively, or RE2 with regex=true (no lookaround/backreferences; ASCII \\w/\\d/\\b). ignore_case: full Unicode folding for literals, simple folding for RE2. Case-sensitive glob matches basenames or workspace-relative paths: * stays within /, ** spans components. context: 0-10 surrounding lines; limit: 1-1000 matching physical CR/LF lines, default 100. Query <=1000 chars; RE2 budgets: 1 MiB and cooperative 1s. Whole-file privacy, read/output bounds and cancellation apply. Truncated, timed_out or skipped results cannot establish absence.",
           {"query": STRING, "path": STRING, "glob": {"type": "string", "minLength": 1, "maxLength": 256},
            "ignore_case": {"type": "boolean"}, "regex": {"type": "boolean"},
            "context": {"type": "integer", "minimum": 0, "maximum": 10},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ["query"]),
    schema("project_map", "Map paths and 1-based declarations for Python, JS/TS/TSX, Rust, Go, C/C++ and Java. query matches literal names/paths case-insensitively; exact then prefix matches rank first. Use a focused query if the full map is truncated. Syntax only; unsupported files have paths, unavailable parses are flagged. Read implementation before editing.",
           {"path": STRING, "query": {"type": "string", "minLength": 1, "maxLength": 256}}),
    schema("edit_file", "Exact text edits with complete diff approval, atomic write and undo. Choose old/new (old='' creates only) OR edits (<=32 unique replacements in ORIGINAL text). Parent must exist. Reread after a match failure. Prefer edit_lines for focused line changes.",
           {"path": STRING, "old": STRING, "new": STRING,
            "edits": {"type": "array", "minItems": 1, "maxItems": MAX_EDITS,
                      "items": {"type": "object", "properties": {"old": STRING, "new": STRING},
                                "required": ["old", "new"], "additionalProperties": False}}}, ["path"]),
    schema("edit_lines", "Whole-line edits after read_file: read_id pins this file's hash; stale IDs fail before diff approval. IDs expire after writes/commands/continuation. 1-based inclusive; insert end_line=start_line-1. Preserve LF/CRLF; no overlap.",
           {"path": STRING, "read_id": {"type": "string", "pattern": READ_ID_PATTERN, "maxLength": 64},
            "edits": {"type": "array", "minItems": 1, "maxItems": MAX_EDITS,
                           "items": {"type": "object", "properties": {
                               "start_line": {"type": "integer", "minimum": 1, "maximum": 1000000},
                               "end_line": {"type": "integer", "minimum": 0, "maximum": 1000000}, "new": STRING},
                               "required": ["start_line", "end_line", "new"], "additionalProperties": False}}}, ["path", "read_id", "edits"]),
    schema("update_plan", "Replace your task checklist (<=16 steps; <=1 in_progress; <=4 KiB total). Model-reported progress only: not approval or test evidence. No files or commands. Revise after new guidance; [] clears it.",
           {"plan": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {
               "step": {"type": "string", "minLength": 1, "maxLength": 256},
               "status": {"enum": ["pending", "in_progress", "completed"]}},
               "required": ["step", "status"], "additionalProperties": False}},
            "explanation": {"type": "string", "maxLength": 512}}, ["plan"]),
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
        self.policy = FilePolicy(self.root, (config.config_path, *config.config_paths), config.excluded_paths)
        self.index = SymbolIndex()
        self.plan = TaskPlan()
        self.read_versions = ReadVersions()
        self.emit_output = emit_output
        self.instructions = ProjectInstructions(self._read_instruction, self._check_cancelled,
                                                config.project_instructions, config.instruction_files)

    def _read_instruction(self, path, limit):
        self.policy.reload()
        if self.policy.excluded(PurePosixPath(path).parts):
            return None
        try:
            with self._parent(path) as (directory, name):
                raw = self._read(directory, name, limit)
            if len(raw) > limit:
                raise InstructionError("project instruction file exceeds 16 KiB: " + path)
            return redact(self._decode(raw).removeprefix("\ufeff"), self.config.secrets)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, ToolError):
            # Fixed diagnostics: decoding and OS errors must not echo rule text.
            raise InstructionError("cannot safely load project instructions: " + path
                                   + "; check privacy, UTF-8, regular-file and 16 KiB limits; remove invalid explicit files or disable AGENTS.md discovery") from None

    def _check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise KeyboardInterrupt

    def _require_tool(self, name):
        self.root.ensure_open()
        if name in TOOL_NAMES and not self.config.tool_enabled(name):
            raise ToolError("tool is disabled by configuration")

    def close(self):
        self.root.close()
        self.history.close()
        self.index.clear()
        self.read_versions.clear()

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
        self.root.ensure_open()
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
            self.root.ensure_open()
            self._require_tool(name)
            if not isinstance(arguments, dict):
                raise ToolError("tool arguments must be an object")
            method = {"list_files": self.list_files, "read_file": self.read_file,
                      "search": self.search, "project_map": self.project_map,
                      "edit_file": self.edit_file, "edit_lines": self.edit_lines,
                      "run": self.run, "update_plan": self.update_plan}.get(name)
            if method is None:
                raise ToolError("unknown tool")
            result = method(**arguments)
            return redact(result, self.config.secrets)
        except (OSError, ToolError, ChangeError, TypeError, ValueError) as exc:
            return {"error": redact(str(exc), self.config.secrets)}

    def update_plan(self, plan, explanation=""):
        self._require_tool("update_plan")
        selected = redact({"plan": plan, "explanation": explanation}, self.config.secrets)
        result = validate_plan(selected["plan"], selected["explanation"])
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > self.config.max_output:
            raise ToolError("plan exceeds max-output; shorten the descriptions")
        self._check_cancelled()
        self.plan.update(result)
        return result

    def list_files(self, path="."):
        self._require_tool("list_files")
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
        self._require_tool("read_file")
        if type(offset) is not int or not 1 <= offset <= 1000000:
            raise ToolError("offset must be an integer from 1 to 1000000 (1-based line number)")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ToolError("limit must be an integer from 1 to 10000")
        parts = self._parts(path)
        if self.config.project_instructions:
            self.instructions.register(parts[:-1])
        if offset != 1 or limit is not None:
            return self._read_result(parts, self._read_page(path, offset, limit or 2000))
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
        result = {"path": path, "content": text, "truncated": truncated}
        if not truncated and not any(secret in text for secret in self.config.credential_secrets):
            result["sha256"] = hashlib.sha256(raw).hexdigest()
        return self._read_result(parts, result)

    def _read_result(self, parts, result):
        if "sha256" in result and not result.get("line_truncated"):
            result["read_id"] = self.read_versions.remember(parts, result["sha256"])
        return result

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
        text = self._decode(raw)
        # Iterate rather than allocating one list entry per line for a file
        # containing millions of blank lines. Keep CR/LF endings unchanged.
        for line in io.StringIO(text, newline=""):
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
        result = {"path": path, "content": "".join(selected), "offset": offset, "end_line": end,
                "total_lines": total, "truncated": truncated, "line_truncated": partial,
                "next_offset": end + 1 if truncated and not partial else None}
        # Never expose a fingerprint of configured credentials that were masked
        # from content, including a secret outside the requested line range.
        if not any(secret in text for secret in self.config.credential_secrets):
            result["sha256"] = hashlib.sha256(raw).hexdigest()
        return result

    def search(self, query, path=".", *, glob=_MISSING, ignore_case=False, regex=False, context=0, limit=100):
        self._require_tool("search")
        if not isinstance(query, str) or not query or len(query) > 1000:
            raise ToolError("query must be a nonempty string (at most 1000 characters)")
        if not isinstance(ignore_case, bool):
            raise ToolError("ignore_case must be a boolean")
        if not isinstance(regex, bool):
            raise ToolError("regex must be a boolean")
        if isinstance(context, bool) or not isinstance(context, int) or not 0 <= context <= 10:
            raise ToolError("context must be an integer between 0 and 10")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ToolError("limit must be an integer between 1 and 1000")
        file_glob = FileGlob(glob) if glob is not _MISSING else None
        matcher = RegexMatcher(query, ignore_case, self._check_cancelled) if regex else None
        parts = self._parts(path)
        result = {"matches": [], "truncated": False, "skipped_files": 0,
                  "files_read": 0, "bytes_read": 0, "entries_scanned": 0}
        if regex:
            result["timed_out"] = False
        # Reserve the whole JSON envelope and counter growth, including redaction.
        def measure(value):
            size = encoded_size(value)
            return max(size, encoded_size(redact(value, self.config.secrets))) if self.config.secrets else size
        envelope = {**result, "skipped_files": 5000, "files_read": 5000,
                    "bytes_read": 8 * 1024 * 1024 + 1, "entries_scanned": 5001}
        remaining = self.config.max_output - measure(envelope)
        budget, stopped = 8 * 1024 * 1024, False

        def scan_file(directory, name, prefix):
            nonlocal budget, remaining, stopped
            self._check_cancelled()
            if file_glob is not None and not file_glob.matches((*prefix, name)):
                return
            if budget <= 0:
                result["truncated"] = stopped = True
                return
            raw = self._read(directory, name, min(MAX_FILE, budget))
            self._check_cancelled()
            budget -= len(raw)
            result["bytes_read"] += len(raw)
            result["files_read"] += 1
            if len(raw) > MAX_FILE or budget < 0:
                result["skipped_files"] += 1
                result["truncated"] = True
                return
            text = self._decode(raw)  # Check all contents before returning even one hit.
            try:
                for hit in matching_lines(text, query, ignore_case, context, self._check_cancelled, matcher):
                    self._check_cancelled()
                    match = {"path": "/".join((*prefix, name)), **hit}
                    size = measure(match) + 2
                    if len(result["matches"]) >= limit or size > remaining:
                        result["truncated"] = stopped = True
                        return
                    result["matches"].append(match)
                    remaining -= size
            except RegexBudgetExceeded:
                result["timed_out"] = result["truncated"] = stopped = True

        def walk(fd, prefix, depth):
            nonlocal stopped
            if depth > 32:
                result["truncated"] = True
                return
            with fd.scandir() as iterator:
                for entry in iterator:
                    self._check_cancelled()
                    result["entries_scanned"] += 1
                    if result["entries_scanned"] > 5000:
                        result["truncated"] = stopped = True
                        return
                    if self.policy.excluded((*prefix, entry.name)) or is_link(entry):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if not skip_directory(entry.name):
                                with fd.child(entry.name) as child:
                                    walk(child, (*prefix, entry.name), depth + 1)
                        elif entry.is_file(follow_symlinks=False):
                            scan_file(fd, entry.name, prefix)
                    except (OSError, ToolError, ValueError):
                        result["skipped_files"] += 1
                        result["truncated"] = True
                    if stopped:
                        return

        self._check_cancelled()
        with self._directory(parts[:-1]) as parent:
            info = parent.stat(parts[-1]) if parts else None
            if info is not None and stat.S_ISREG(info.st_mode):
                scan_file(parent, parts[-1], parts[:-1])
            else:
                with self._directory(parts) as fd:
                    walk(fd, parts, 0)
        self._check_cancelled()
        return result

    def project_map(self, path=".", query=""):
        self._require_tool("project_map")
        if not isinstance(query, str) or len(query.encode("utf-8")) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in query):
            raise ToolError("query must be a literal string of at most 256 UTF-8 bytes without control characters")
        if query and not query.strip():
            raise ToolError("query must contain a non-whitespace character")
        query = query.strip().casefold()
        def measure(record):
            size = encoded_size(record)
            if self.config.secrets:
                size = max(size, encoded_size(redact(record, self.config.secrets)))
            return size

        selection = MapSelection(self.config.max_output, measure) if query else None
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
                    if scanned > 5000 or budget <= 0 or (selection is None and size >= self.config.max_output):
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
                                except IndexUnavailable:
                                    truncated = True
                                    record["symbols_unavailable"] = True
                                except (ValueError, ToolError):
                                    record["symbols_unavailable"] = True
                        if selection is not None:
                            selection.add(record, query, self._check_cancelled)
                            continue
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

        with self._directory(parts) as directory, self.index.scan(lambda: not truncated):
            walk(directory, parts, 0)
        if selection is not None:
            return selection.result(truncated, self._check_cancelled)
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
            return redact(result, self.config.secrets)
        except (OSError, ToolError, ChangeError, ValueError) as exc:
            return {"error": redact(str(exc), self.config.secrets)}

    def _restore_change(self, action):
        self._require_tool("edit_file")
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
            if any(secret in (text or "") for secret in self.config.credential_secrets for text in (before, after)):
                raise ToolError("configured API key must not appear in change backups")
            description = action.upper() + " " + json.dumps(path) + "\n" + (self._diff(path, before, after) or "(empty file create/delete)")
            if not self.approve(description):
                return {"approved": False, "changed": False}
            self._parts(path)  # File privacy rules may have changed during review.
            self._unchanged(fd, name, current, info)
            self._check_cancelled()
            try:
                self.read_versions.discard(self._parts(path))
                self.history.begin_restore(action, path)
                self._replace(fd, name, target, record["mode"], info)
                self.history.complete(self._revision(fd.stat(name)) if target is not None else None)
            except (OSError, ChangeError) as error:
                return {"approved": True, "changed": False, "outcome": "uncertain", "path": path, "action": action,
                        "error": "Restore outcome is uncertain; inspect current files and the pending backup before continuing: " + str(error)}
        return {"approved": True, "changed": True, "path": path, "action": action,
                "note": "Workspace changed; previous model conclusions and acceptance results need rechecking."}

    def edit_lines(self, path, read_id=_MISSING, edits=_MISSING):
        self._require_tool("edit_lines")
        if self.config.read_only:
            return {"approved": False, "changed": False, "error": "read-only mode blocks file writes"}
        if read_id is _MISSING:
            return {"error": "edit_lines requires read_id from read_file", "changed": False,
                    "edit_failure": {"reason": "missing_revision", "source": "read_id"}}
        if not valid_read_id(read_id):
            return {"error": "read_id must be a complete identifier returned by read_file", "changed": False,
                    "edit_failure": {"reason": "invalid_revision", "source": "read_id"}}
        parts = self._parts(path)
        digest = self.read_versions.resolve(read_id, parts)
        if digest is None:
            return {"error": "read_id is not valid for this file/current run; read_file again", "changed": False,
                    "edit_failure": {"reason": "unknown_read_id", "source": "read_id"}}
        if edits is _MISSING:
            raise ToolError("edit_lines requires edits")
        result = self.edit_file(path, sha256=digest, line_edits=edits)
        if "edit_failure" in result:
            result["edit_failure"] = {**result["edit_failure"], "source": "read_id"}
        return result

    def edit_file(self, path, old=_MISSING, new=_MISSING, *, edits=_MISSING, sha256=_MISSING, line_edits=_MISSING):
        self._require_tool("edit_file")
        if self.config.read_only:
            return {"approved": False, "changed": False, "error": "read-only mode blocks file writes"}
        multiple = edits is not _MISSING
        by_line = line_edits is not _MISSING
        if by_line:
            if multiple or old is not _MISSING or new is not _MISSING:
                raise ToolError("choose old/new, edits, or sha256 plus line_edits")
            if sha256 is _MISSING:
                return {"error": "line_edits require full sha256 from read_file; prefer edit_lines with read_id", "changed": False,
                        "edit_failure": {"reason": "missing_revision", "source": "sha256"}}
            if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
                return {"error": "sha256 must be 64 lowercase hex characters from read_file; prefer edit_lines with read_id", "changed": False,
                        "edit_failure": {"reason": "invalid_revision", "source": "sha256"}}
            validate_edits(line_edits, MAX_FILE, self._check_cancelled)
            replacements = []
        elif sha256 is not _MISSING:
            raise ToolError("sha256 requires line_edits")
        elif multiple:
            if old is not _MISSING or new is not _MISSING:
                raise ToolError("provide old/new or edits, not both")
            if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
                raise ToolError("edits must contain between 1 and 32 replacements")
            if any(not isinstance(edit, dict) or set(edit) != {"old", "new"} for edit in edits):
                raise ToolError("each edit must contain only old and new")
            replacements = [(edit["old"], edit["new"]) for edit in edits]
        else:
            replacements = [(old, new)]
        total = 0
        for previous, replacement in replacements:
            self._check_cancelled()
            if not isinstance(previous, str) or not isinstance(replacement, str):
                raise ToolError("old and new must be strings")
            if multiple and not previous:
                raise ToolError("edits require nonempty old text in an existing file")
            sizes = len(previous.encode("utf-8")), len(replacement.encode("utf-8"))
            if max(sizes) > MAX_FILE:
                raise ToolError("edit exceeds 512 KiB")
            total += sum(sizes)
            if multiple and total > MAX_FILE:
                raise ToolError("combined edits exceed 512 KiB")
        parts = self._parts(path)
        rules = self.instructions.prepare_action(parts[:-1])
        with self._parent(path) as (fd, name):
            before_raw, before_stat = self._snapshot(fd, name)
            before = self._decode(before_raw or b"")
            if by_line:
                if before_stat is None:
                    raise ToolError("line_edits require an existing file")
                if hashlib.sha256(before_raw).hexdigest() != sha256:
                    return {"error": "file revision changed; read the file again before proposing line_edits", "changed": False,
                            "edit_failure": {"reason": "stale_revision"}}
                after = replace_lines(before, line_edits, self._check_cancelled)
            elif before_stat is None:
                if multiple:
                    raise ToolError("edits require an existing file")
                if old:
                    raise ToolError("new files require old to be empty")
                after = new
            else:
                spans = []
                for index, (previous, replacement) in enumerate(replacements, 1):
                    self._check_cancelled()
                    start = before.find(previous) if previous else -1
                    # Search from start+1: even overlapping repeated matches
                    # are ambiguous (for example 'aa' within 'aaa').
                    second = before.find(previous, start + 1) if start >= 0 else -1
                    if start < 0 or second >= 0:
                        return {"error": "old must match exactly once in an existing file", "changed": False,
                                "edit_failure": failure_details(before, previous, index,
                                    (start, second) if second >= 0 else (), self._check_cancelled)}
                    spans.append((start, start + len(previous), replacement))
                spans.sort(key=lambda span: span[0])
                if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
                    raise ToolError("replacement matches overlap in the original file")
                chunks, end = [], 0
                for start, stop, replacement in spans:
                    chunks.extend((before[end:start], replacement))
                    end = stop
                chunks.append(before[end:])
                after = "".join(chunks)
            data = after.encode("utf-8")
            if len(data) > MAX_FILE:
                raise ToolError("result exceeds 512 KiB")
            self._decode(data)
            if any(secret in before or secret in after for secret in self.config.credential_secrets):
                raise ToolError("configured API key must not appear in change backups")
            if before_stat is not None and before == after:
                return {"changed": False}
            diff = self._diff(path, before if before_stat else None, after)
            if not self.approve(f"WRITE {json.dumps(path)}\n{diff or '(create empty file)'}"):
                return {"approved": False, "changed": False}
            # Recheck after approval: an editor may have changed the file while the user read the diff.
            self._parts(path)
            self.instructions.recheck_action(parts[:-1], rules)
            self._unchanged(fd, name, before_raw, before_stat)
            self._check_cancelled()
            self.read_versions.discard(parts)
            mode = stat.S_IMODE(before_stat.st_mode) & 0o777 if before_stat else 0o600
            self.history.begin_edit(path, before_raw, data, mode, self._revision(before_stat))
            self._replace(fd, name, data, mode, before_stat)
            self.history.complete(self._revision(fd.stat(name)))
        result = {"approved": True, "changed": True, "path": path}
        if by_line:
            result["sha256"] = hashlib.sha256(data).hexdigest()
        return result

    def run(self, argv, cwd=".", timeout=None):
        self._require_tool("run")
        if self.config.read_only:
            return {"approved": False, "error": "read-only mode blocks command execution"}
        if (not isinstance(argv, list) or not argv or len(argv) > 256
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]):
            raise ToolError("argv must be a nonempty array of strings (at most 256)")
        if sum(len(arg) for arg in argv) > 65536:
            raise ToolError("argv is too large")
        if any(secret in arg for secret in self.config.credential_secrets for arg in argv):
            raise ToolError("configured API key must not appear in command arguments")
        seconds = self.config.timeout if timeout is None else timeout
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 0 < seconds <= self.config.timeout):
            raise ToolError("command timeout must be positive and no larger than the configured timeout")
        parts = self._parts(cwd)
        with self._directory(parts):
            pass
        directory = self.config.workdir.joinpath(*parts)
        rules = self.instructions.prepare_action(parts)
        description = (f"RUN (NOT SANDBOXED; may access outside files/network)\n"
                       f"cwd: {json.dumps(str(directory))}\nargv: {json.dumps(argv)}\n"
                       f"timeout: {seconds}s; output cap: {self.config.max_output} bytes")
        if self.config.env_allowlist or self.config.command_env or self.config.inherit_env:
            description += ("\nenvironment: " + ("expanded inheritance" if self.config.inherit_env else "minimal inheritance")
                            + "; additional inherited names: " + json.dumps(self.config.env_allowlist)
                            + "; override names: " + json.dumps([name for name, _ in self.config.command_env]))
        if not self.approve(description):
            return {"approved": False}
        self._check_cancelled()
        # Unlike file tools, the approved program controls its own filesystem and network access.
        self._parts(cwd)
        with self._directory(parts):
            pass
        env = command_environment(self.config)
        self.instructions.recheck_action(parts, rules)
        stream = CommandOutput(self.emit_output, self.config.secrets)
        self.read_versions.clear()
        if os.name == "nt":
            from .windows import run_command
            try:
                result = run_command(argv, directory, env, seconds, self.config.max_output, self.cancel_event, stream.write)
            finally:
                self.read_versions.clear()
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
            self.read_versions.clear()
            # Also clean up descendants that inherited the process group / pipe.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            self.read_versions.clear()
        stream.finish()
        return {"approved": True, "returncode": process.returncode, "streamed": bool(self.emit_output),
                "output": output.decode("utf-8", errors="replace"),
                "timed_out": reason == "timeout", "output_limited": reason == "output_limit"}
