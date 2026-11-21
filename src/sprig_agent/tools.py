"""Bounded, approval-first workspace tools."""

from contextlib import ExitStack, contextmanager, nullcontext
import difflib
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid

from .config import TOOL_NAMES, redact, terminal_text
from .command_sandbox import Launch, Sandbox
from .changes import ChangeError, ChangeHistory, unpack
from .filesystem import Directory, is_link
from .index import SymbolIndex, language_for
from .instructions import InstructionError, ProjectInstructions
from .planning import TaskPlan, validate_plan
from .revisions import MAX_RESUME_READS, READ_ID_PATTERN, ReadVersions, valid_read_id
from .map_selection import MapSelection, encoded_size
from .policy import FilePolicy, command_environment
from .output import CommandOutput, SampledCommandOutput
from .command_logs import BackgroundLog, CommandLogs, LogCapture
from .questions import answer_result, validate_questions
from .skills import Skills
from .mcp import McpManager
from .mcp_process import StdioApprovalExpired, StdioPreparation, finish_session, poll_unreaped
from .web_fetch import WebFetcher
from .jobs import Jobs
from .lsp import LanguageServers
from .lsp_client import LspError
from .edit_match import failure_details
from .edit_batch import MAX_BATCH_FILES, EditPreview, digest, preflight
from .line_edits import replace_lines, validate_edits
from .line_patch import apply as apply_line_patch
from .search import (FileGlob, MAX_RANGE_LINES, MAX_READ_RANGES, MAX_SEARCH_WINDOW_FILES, RegexBudgetExceeded,
                     MAX_WINDOW_MATCHES, RegexMatcher, match_window_prefixes, matching_lines)
from .syntax import IndexUnavailable
from .image_reads import ImageReadResult, read_image
from .file_glob import discover as discover_files

MAX_FILE = 512 * 1024
MAX_READ = 8 * 1024 * 1024
MAX_EDITS = 32
MAX_SEARCH_WINDOW_BYTES = 4096
MAX_CHECK_READ_VERSIONS = 16
DIRECTORY_GUIDANCE = "For optional directory arguments, omit the field or use '.' for the selected workspace; paths in argv do not define cwd."
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
    schema("list_files", "List one directory; no links/private paths.",
           {"path": STRING}),
    schema("glob", "Case-sensitive glob relative to path; basename any depth, ** dirs. Check flags.",
           {"pattern": STRING, "path": STRING,
            "hidden": {"type": "boolean"}, "limit": {"type": "integer"}}, ["pattern"]),
    schema("read_file", "Numbered ranges=[[start,end],...]; symbol; cursor=start/next_cursor; image=true; range_complete/symbol_complete",
           {"path": STRING, "offset": {"type": "integer", "minimum": 1, "maximum": 1000000},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10000}, "line_numbers": {"type": "boolean"},
            "ranges": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}},
            "cursor": {"type": "string", "maxLength": 80},
            "symbol": {"type": "string", "minLength": 1, "maxLength": 1024}, "image": {"type": "boolean"}}, ["path"]),
    schema("grep", "RE2 ASCII \\w/\\d/\\b; no lookaround/backrefs; simple folding; literal=true; physical CR/LF; timed_out",
           {"pattern": STRING, "path": STRING, "glob": STRING,
            "ignore_case": {"type": "boolean"}, "literal": {"type": "boolean"},
            "context": {"type": "integer", "minimum": 0, "maximum": 10},
            "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            "matches_per_file": {"type": "integer", "minimum": 1, "maximum": MAX_WINDOW_MATCHES},
            "cursor": STRING,
            "output_mode": {"enum": ["content", "files", "count", "read", "windows"]}}, ["pattern"]),
    schema("project_map", "File/dir declarations; literal name/path. include_calls: syntax, not resolved refs. Check flags; read source.",
           {"path": STRING, "query": {"type": "string", "minLength": 1, "maxLength": 256},
            "include_calls": {"type": "boolean"}}),
    schema("edit_file", "Unique old/new (old='' creates) OR <=32 edits in original text. Full diff approval, atomic write, undo. Reread mismatches.",
           {"path": STRING, "old": STRING, "new": STRING,
            "edits": {"type": "array", "minItems": 1, "maxItems": MAX_EDITS,
                      "items": {"type": "object", "properties": {"old": STRING, "new": STRING},
                                "required": ["old", "new"], "additionalProperties": False}}}, ["path"]),
    schema("edit_lines", "1-based inclusive; insert end=start-1. lines[] adds EOL. patch: @@ exact context or numbered.",
           {"path": STRING, "read_id": {"type": "string", "pattern": READ_ID_PATTERN, "maxLength": 64},
            "patch": STRING,
            "edits": {"type": "array", "minItems": 1, "maxItems": MAX_EDITS,
                           "items": {"type": "object", "properties": {
                               "start_line": {"type": "integer", "minimum": 1, "maximum": 1000000},
                               "end_line": {"type": "integer", "minimum": 0, "maximum": 1000000},
                               "new": STRING, "lines": {"type": "array", "items": STRING}},
                               "required": ["start_line", "end_line"], "additionalProperties": False}}}, ["path", "read_id"]),
    schema("update_plan", "Plan <=16, <=1 in_progress; [] clears. Not approval/acceptance.",
           {"plan": {"type": "array", "maxItems": 16, "items": {"type": "object", "properties": {
               "step": {"type": "string", "minLength": 1, "maxLength": 256},
               "status": {"enum": ["pending", "in_progress", "completed"]}},
               "required": ["step", "status"], "additionalProperties": False}},
            "explanation": {"type": "string", "maxLength": 512}}, ["plan"]),
    schema("file_action", "mkdir; delete/move UTF-8 <=512KiB with read_id. No overwrite/recursive delete; move asks twice and may leave both files.",
           {"action": {"enum": ["mkdir", "delete", "move"]}, "path": STRING, "destination": STRING,
            "read_id": STRING}, ["action", "path"]),
    schema("run", "Approve argv; NOT sandboxed.",
           {"argv": {"type": "array", "items": STRING, "minItems": 1}, "cwd": STRING,
            "timeout": {"type": "number", "exclusiveMinimum": 0, "maximum": 120},
            "backup": {"type": "array", "items": STRING}}, ["argv"]),
]
GIT_STATUS_SCHEMA = schema("git_status", "Approved Git status by literal path. Index/worktree codes; ?? untracked. No authorship inference.",
                           {"path": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 512}})
GIT_DIFF_SCHEMA = schema("git_diff", "Approved single-file Git patch. staged=true compares HEAD to index. Continue with snapshot and next_offset.",
                         {"path": STRING, "staged": {"type": "boolean"}, "offset": {"type": "integer", "minimum": 0},
                          "snapshot": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}, ["path"])
PATCH_FILES_SCHEMA = schema("patch_files", "Preflight independent existing files, then approve each diff. Each patch uses exact @@ hunks and its own read_id.",
                            {"files": {"type": "array", "minItems": 2, "maxItems": MAX_BATCH_FILES, "items": {
                                "type": "object", "properties": {"path": STRING, "read_id": STRING, "patch": STRING},
                                "required": ["path", "read_id", "patch"], "additionalProperties": False}}}, ["files"])


class ToolError(Exception):
    pass


class _RangeOutputLimit(ToolError):
    """The complete numbered payload exceeds the configured output cap."""


class _WorkspacePathMissing(FileNotFoundError):
    def __init__(self, directory_parts):
        super().__init__("Workspace path not found.")
        self.directory_parts = directory_parts


def protected(name):
    return name == ".git" or name == ".env" or name.startswith(".env.")


class Tools:
    def __init__(self, config, approve, cancel_event=None, emit_output=None, history=None, *, ask=None):
        self.config, self.approve = config, approve
        self._conversation_version = 0
        self.cancel_event = cancel_event
        self.ask = ask
        self.root = Directory(config.workdir)
        self.history = history or ChangeHistory(config)
        self.policy = FilePolicy(self.root, (config.config_path, *config.config_paths), config.excluded_paths)
        self.index = SymbolIndex()
        self.plan = TaskPlan()
        self.read_versions = ReadVersions()
        self.logs = CommandLogs(config, self._check_cancelled)
        self._edit_preview = False
        self._edit_batch = None
        self._action_observer = None
        self._patch_superseded = None
        self.emit_output = emit_output
        self.instructions = ProjectInstructions(self._read_instruction, self._check_cancelled,
                                                config.project_instructions, config.instruction_files)
        self.skills = Skills(config, self.root.ensure_open, self._check_cancelled, self.policy)
        self.mcp_manager = McpManager(config, self._check_mcp, self.request_approval, self._prepare_stdio_process)
        self.web = WebFetcher(config, self._check_mcp, self.request_approval)
        self.jobs = Jobs(config, self._check_mcp)
        self.lsp_manager = LanguageServers(config, self._check_mcp, self.request_approval, self._prepare_stdio_process,
                                           self._lsp_document, self._lsp_rename, self._lsp_code_action)
        self._jobs_revision = self.jobs.revision

    def _check_mcp(self):
        self.root.ensure_open()
        self._check_cancelled()

    @contextmanager
    def _prepare_stdio_process(self, server):
        self._check_mcp()
        config, version = self.config, self._conversation_version
        parts = self._parts(server.cwd)
        if any(secret in arg for secret in config.credential_secrets for arg in server.command):
            raise ToolError("stdio argv cannot contain configured credentials; use declared environment references")
        with ExitStack() as directories:
            directory, chain = directories.enter_context(self._command_directory(parts))
            rules = self.instructions.prepare_action(parts)

            def check_policy():
                self._check_mcp()
                if self.config is not config or self._conversation_version != version:
                    raise StdioApprovalExpired("stdio owner or policy changed; request fresh approval")
                try:
                    self._parts(server.cwd)
                except (OSError, ToolError, ValueError):
                    raise StdioApprovalExpired("stdio working directory is no longer allowed; request fresh approval") from None

            @contextmanager
            def checked_directory():
                check_policy()
                with ExitStack() as stack:
                    try:
                        current, _ = stack.enter_context(self._command_directory(parts, chain))
                    except (OSError, ToolError, ValueError):
                        raise StdioApprovalExpired("stdio working directory changed; request fresh approval") from None
                    yield current, stack

            def check_directory():
                with checked_directory():
                    pass

            def check_rules():
                try:
                    self.instructions.recheck_action(parts, rules)
                except InstructionError:
                    raise StdioApprovalExpired("stdio project instructions changed; request fresh approval") from None
                self.read_versions.clear()

            def recheck():
                with checked_directory():
                    check_rules()

            @contextmanager
            def launch():
                with checked_directory() as (current, validated):
                    check_rules()
                    if any(secret in arg for secret in config.credential_secrets for arg in server.command):
                        raise StdioApprovalExpired("configured credentials cannot appear in stdio argv")
                    env = command_environment(config, minimal=True)
                    for name, value in server.env:
                        if os.name == "nt":
                            for previous in list(env):
                                if previous.upper() == name.upper():
                                    del env[previous]
                        env[name] = value
                    if os.name == "nt":
                        directories.enter_context(validated.pop_all())
                    with self._command_launch(current, list(server.command), env) as command:
                        yield command, directories if os.name == "nt" else None

            yield StdioPreparation(directory.path, check_directory, recheck, launch)

    def rebind_model(self, config):
        """Idle model changes cannot change tool policy or drop known secrets."""
        from .config import validate_model_change
        self.root.ensure_open()
        self._check_cancelled()
        try:
            validate_model_change(self.config, config)
        except ValueError as error:
            raise ToolError(str(error)) from None
        self._conversation_version += 1
        self.jobs.reset()
        self.lsp_manager.reset()
        self.mcp_manager.reset()
        self.web.reset()
        self.read_versions.clear()
        self.index.clear()
        self.logs.reset_memory()
        self.config = config
        self.skills.config = config
        self.logs.config = config
        self.mcp_manager.config = config
        self.web.config = config
        self.jobs.config = config
        self.lsp_manager.config = config
        if self.history.store is not None:
            self.history.store.config = config

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

    @contextmanager
    def observe_actions(self, observer):
        previous, self._action_observer = self._action_observer, observer
        try:
            yield
        finally:
            self._action_observer = previous

    @contextmanager
    def guard_patch(self, superseded):
        previous, self._patch_superseded = self._patch_superseded, superseded
        try:
            yield
        finally:
            self._patch_superseded = previous

    def request_approval(self, description):
        observer = self._action_observer
        if observer is not None:
            observer("waiting")
        self._check_cancelled()
        scope = self.history.edit_scope
        if scope is not None:
            description = f"User-selected edit scope is {scope['name']} (revision {scope['revision']}).\n" + description
        approved = self.approve(description) is True
        if observer is not None:
            observer("approved" if approved else "rejected")
        self._check_cancelled()
        if approved and observer is not None:
            observer("running")
            self._check_cancelled()
        return approved

    def _require_tool(self, name):
        self.root.ensure_open()
        if self._jobs_revision != self.jobs.revision:
            self.read_versions.clear()
            self.index.clear()
            self._jobs_revision = self.jobs.revision
        if name in TOOL_NAMES and not self.config.tool_enabled(name):
            raise ToolError("tool is disabled by configuration")

    def close(self):
        self.jobs.close()
        self.lsp_manager.reset(closing=True)
        self.mcp_manager.reset()
        self.web.reset()
        self.root.close()
        self.history.close()
        self.index.clear()
        self.read_versions.clear()
        self.logs.close()

    def _parts(self, path):
        if not isinstance(path, str) or not path or len(path) > 4096 or "\0" in path or "\\" in path:
            raise ToolError("path must be a nonempty relative POSIX path using '/' separators. " + DIRECTORY_GUIDANCE)
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts or any(protected(p.lower() if os.name == "nt" else p) for p in parsed.parts):
            raise ToolError("path escapes workdir or names a protected component. " + DIRECTORY_GUIDANCE)
        self.policy.reload()
        if self.policy.excluded(parsed.parts):
            raise ToolError("path is excluded by file privacy policy")
        if os.name == "nt":
            from .windows import validate_component
            for part in parsed.parts:
                validate_component(part)
        return parsed.parts

    @contextmanager
    def _directory(self, parts, *, root=None, identities=None):
        directory = self.root if root is None else root
        directory.ensure_open()
        opened = []
        try:
            if identities is not None:
                identities.append(directory.identity())
            for index, part in enumerate(parts):
                try:
                    directory = directory.child(part)
                except FileNotFoundError:
                    raise _WorkspacePathMissing(parts[:index]) from None
                opened.append(directory)
                if identities is not None:
                    identities.append(directory.identity())
            yield directory
        finally:
            for directory in reversed(opened):
                directory.close()

    @contextmanager
    def _command_directory(self, parts, expected=None):
        changed = "command working directory changed; request fresh approval"
        with ExitStack() as stack:
            try:
                named_root = stack.enter_context(Directory(self.root.path))
            except (OSError, ValueError):
                raise ToolError(changed) from None
            if named_root.identity() != self.root.identity():
                raise ToolError(changed)
            try:
                identities = []
                cwd = stack.enter_context(self._directory(parts, root=named_root, identities=identities))
            except (OSError, ValueError):
                if expected is None:
                    raise
                raise ToolError(changed) from None
            chain = tuple(identities)
            if expected is not None and chain != expected:
                raise ToolError(changed)
            yield cwd, chain

    @contextmanager
    def _command_launch(self, directory, argv, env):
        if sys.platform != "linux":
            self._check_cancelled()
            yield Launch(argv, directory.path, env, ())
            return
        import fcntl
        # Popen uses cwd before closing the child descriptors. Keep the
        # reference above the stdio slots and close it before user code runs.
        fd = fcntl.fcntl(directory.fd, fcntl.F_DUPFD_CLOEXEC, 3)
        try:
            path = Path(f"/proc/self/fd/{fd}")
            try:
                info = path.stat()
            except OSError:
                raise ToolError("cannot bind command working directory through /proc/self/fd") from None
            if (info.st_dev, info.st_ino) != directory.identity():
                raise ToolError("cannot bind command working directory through /proc/self/fd")
            self._check_cancelled()
            yield Launch(argv, path, env, ())
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
        return parent.read(name, limit)

    @staticmethod
    def _scope_stat(parent, parts):
        if not parts:
            return None
        try:
            return parent.stat(parts[-1])
        except FileNotFoundError:
            raise _WorkspacePathMissing(parts[:-1]) from None

    @contextmanager
    def _scope_parent(self, parts):
        with self._directory(parts[:-1]) as parent:
            yield parent, self._scope_stat(parent, parts)

    def _missing_path_result(self, error):
        self._check_cancelled()
        result = {"error": str(error)}
        directory = "/".join(error.directory_parts) or "."
        if (self.policy.excluded(error.directory_parts) or "\n" in directory or terminal_text(directory) != directory
                or redact(directory, self.config.secrets) != directory):
            return result
        try:
            self.policy.check_text(directory)
        except ValueError:
            return result
        hint = {**result, "suggested_directory": directory}
        if self.config.tool_enabled("list_files"):
            hint["guidance"] = "Use list_files at suggested_directory to choose a path."
        return hint if encoded_size(hint) <= self.config.max_output else result

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
        batch = self._edit_batch if name in {"edit_file", "edit_lines"} else None
        if batch is not None and batch.failed:
            return batch.stopped_result()
        try:
            self.root.ensure_open()
            self._require_tool(name)
            if not isinstance(arguments, dict):
                raise ToolError("tool arguments must be an object")
            if name == "patch_files":
                from .multi_patch import apply
                if set(arguments) != {"files"}:
                    raise ToolError("patch_files requires exactly the files array")
                return redact(apply(self, arguments["files"], superseded=self._patch_superseded), self.config.secrets)
            method = {"list_files": self.list_files, "glob": self.glob, "read_file": self.read_file,
                      "search": self.search, "grep": self.grep, "project_map": self.project_map,
                      "edit_file": self.edit_file, "edit_lines": self.edit_lines,
                      "run": self.run, "update_plan": self.update_plan, "file_action": self.file_action,
                      "read_log": self.read_log, "question": self.question, "skill": self.skill, "mcp": self.mcp,
                      "web_fetch": self.web_fetch, "job": self.job, "lsp": self.lsp, "git_status": self.git_status,
                      "git_diff": self.git_diff}.get(name)
            if method is None:
                raise ToolError("unknown tool")
            result = method(**arguments)
        except _WorkspacePathMissing as exc:
            result = self._missing_path_result(exc)
        except (OSError, ToolError, ChangeError, TypeError, ValueError) as exc:
            result = {"error": str(exc)}
        if isinstance(result, (EditPreview, ImageReadResult)):
            return result
        if batch is not None:
            batch.finish(arguments, result)
        return redact(result, self.config.secrets)

    def git_status(self, path=".", limit=128):
        self._require_tool("git_status")
        from .git_status import git_status
        return git_status(self, path, limit)

    def git_diff(self, path, staged=False, offset=0, snapshot=None):
        self._require_tool("git_diff")
        from .git_status import git_diff
        return git_diff(self, path, staged, offset, snapshot)

    def git_observation_available(self):
        if self.config.read_only or not self.config.tool_enabled("run"):
            return False
        from .git_status import checkout_available
        return checkout_available(self.config.workdir)

    def edit_batch(self, requests, guard=None, *, model_action=True):
        return preflight(self, requests, guard, model_action=model_action)

    def web_fetch(self, url=None, result_id=None, offset=0, format="text"):
        self._require_tool("web_fetch")
        self._check_cancelled()
        rules = self.instructions.prepare_action(()) if url is not None else None
        def recheck():
            self._check_mcp()
            self.instructions.recheck_action((), rules)
        return self.web.fetch(url, result_id, offset, format, recheck=recheck)

    def job(self, action="list", job_id=None, offset=0, end=None, seconds=0, text=None, columns=None, rows=None):
        self._require_tool("job")
        self._check_cancelled()
        if action != 'resize' and (columns is not None or rows is not None):
            raise ToolError('terminal dimensions apply only to resize')
        if action in {"input", "eof", "resize"}:
            if type(offset) is not int or offset != 0 or end is not None or type(seconds) not in (int, float) or seconds != 0:
                raise ToolError("job input and resize do not accept output offsets or wait durations")
            return self._job_input(action, job_id, text, (columns, rows) if action == 'resize' else None)
        if text is not None:
            raise ToolError("text applies only to job input")
        if action == "cancel":
            self.jobs.control("status", job_id)
            if not self.request_approval("STOP owned background command " + str(job_id)
                                         + "; terminate its process tree. Prior filesystem/network effects are not undone."):
                return {"approved": False, "job_id": job_id}
            self.root.ensure_open()
        result = self.jobs.control(action, job_id, offset, end, seconds)
        if action == "cancel":
            result["approved"] = True
        return result

    def _job_input(self, action, identity, text, size=None):
        proposal = self.jobs.prepare_input(action, identity, text, size)
        if self.config is not proposal.config:
            raise ToolError("stdin policy changed; request fresh approval")
        row = proposal.row
        cwd = row.cwd.relative_to(self.config.workdir).as_posix()
        parts = self._parts(cwd)
        rules = self.instructions.prepare_action(parts)
        with self._command_directory(parts, row.cwd_chain):
            def recheck():
                self._check_mcp()
                self._require_tool("job")
                if self.config is not proposal.config:
                    raise ToolError("stdin policy changed; request fresh approval")
                self._parts(cwd)
                with self._command_directory(parts, row.cwd_chain):
                    pass
                if text is not None and any(secret in text for secret in self.config.credential_secrets):
                    raise ToolError("configured credentials must not appear in stdin input")
                self.instructions.recheck_action(parts, rules)
            recheck()
            if action == 'resize':
                description = f'RESIZE owned terminal {identity} to {size[0]} columns × {size[1]} rows'
            elif action == 'input':
                description = 'SEND UTF-8 ' + ('TERMINAL INPUT' if row.terminal_size else 'STDIN') + ' to owned background command ' + str(identity)
            else:
                description = 'SEND EOF to owned background command ' + str(identity)
            description += ("\ncwd: " + json.dumps(cwd, ensure_ascii=False)
                            + "\ncommand preview" + (" (truncated)" if row.command_truncated else "") + ": "
                            + terminal_text(redact(row.label, self.config.secrets)))
            description += ("\nThe program may perform further filesystem or network actions. "
                            "The original job deadline and output caps still apply. "
                            "A failed or partial operation stops the job and can leave incomplete effects.")
            if text is not None:
                description += ("\nUTF-8 input bytes: " + str(len(proposal.data)) + "; previously written: " + str(row.stdin_bytes)
                                + "/65536\nExact text (no newline added): " + json.dumps(text, ensure_ascii=False))
            if not self.request_approval(description):
                return {"approved": False, "job_id": identity}
            recheck()
            self.read_versions.clear()
            self.index.clear()
            try:
                return self.jobs.deliver_input(proposal, recheck)
            finally:
                self.read_versions.clear()
                self.index.clear()

    def _lsp_document(self, path):
        self._check_mcp()
        if not self.config.tool_enabled("read_file"):
            raise LspError("LSP source synchronization requires read_file")
        try:
            parts = self._parts(path)
            with self._parent(path) as (directory, name):
                raw = self._read(directory, name, MAX_FILE + 1)
            if len(raw) > MAX_FILE:
                raise LspError("LSP source exceeds 512 KiB")
            text = self._decode(raw)
            if any(secret in text for secret in self.config.credential_secrets):
                raise LspError("LSP source contains configured credentials; synchronization is blocked")
            return {"path": "/".join(parts), "uri": self.config.workdir.joinpath(*parts).as_uri(), "text": text,
                    "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        except (ToolError, OSError, UnicodeError) as error:
            raise LspError("cannot safely synchronize LSP source; check workspace scope, privacy, UTF-8 and regular-file limits") from None

    def lsp(self, action="status", **arguments):
        self._require_tool("lsp")
        try:
            return self.lsp_manager.control(action, **arguments)
        finally:
            if action not in {"status", "result"}:
                self.read_versions.clear()
                self.index.clear()

    def _lsp_rename(self, plans, guard):
        return self._lsp_text_edits(plans, guard, 'rename')

    def _lsp_code_action(self, plans, guard):
        return self._lsp_text_edits(plans, guard, 'apply_code_action')

    def _lsp_text_edits(self, plans, guard, action):
        label = 'rename' if action == 'rename' else 'code action'
        requests=[]
        for plan in plans:
            doc=plan['document']
            if doc['text']==plan['after']:
                continue
            # Whole-file line replacement keeps the full source hash guard and
            # all ordinary privacy, diff, identity, backup and approval checks.
            total=sum(1 for _ in io.StringIO(doc['text'][1:] if doc['text'].startswith('\ufeff') else doc['text'],newline=''))
            after=plan['after'][1:] if doc['text'].startswith('\ufeff') else plan['after']
            requests.append(('edit_file',{'path':doc['path'],'sha256':doc['sha256'],
                'line_edits':[{'start_line':1,'end_line':total,'new':after}]},None))
        result={'action':action,'approved':True,'changed':False,'changed_files':0,'requested_files':len(requests),
                'unversioned_target_files':sum(not plan['version_confirmed'] for plan in plans),'not_acceptance':True}
        if not requests:
            guard();return result
        def detail(value):
            text=str(redact(value.get('error','Operation was refused or did not complete'),self.config.secrets))
            while len(json.dumps(text,ensure_ascii=False).encode('utf-8'))>384:
                text=text[:max(1,len(text)//2)]
            return text
        def bounded(value):
            # All other fields are bounded counters/enums and a 32-byte ID.
            # Only diagnostics vary; never discard completed-prefix facts.
            if len(json.dumps(value,ensure_ascii=False).encode('utf-8'))>min(self.config.max_output,16384):
                value.pop('detail',None)
                value['detail_omitted']=True
            return value
        def expired():
            try:
                guard()
            except (ValueError,OSError) as error:
                result.update(error=f'LSP {label} owner expired; inspect completed files and backups before continuing',
                    reason='owner_expired',partial=result['changed'],detail=detail({'error':str(error)}))
                return True
            return False
        with self.edit_batch(requests,guard,model_action=self.instructions.model_action_active) as blocked:
            if blocked is not None:
                failed=next((value for value in blocked if value.get('batch_preflight')=='failed'),blocked[0])
                return bounded({**result,'error':f'LSP {label} batch preflight failed; no writes executed','reason':'preflight','detail':detail(failed)})
            result['batch_id']=self._edit_batch.identity
            for index,(name,arguments,_) in enumerate(requests):
                if expired():return bounded(result)
                value=self.execute(name,arguments)
                if value.get('changed') is True and value.get('approved') is True and 'error' not in value and not value.get('outcome'):
                    result['changed_files']+=1;result['changed']=True
                if ('error' in value or value.get('approved') is False or value.get('outcome') or value.get('skipped')):
                    uncertain=bool(value.get('outcome') or self.history.state['pending'] is not None)
                    result.update(partial=result['changed'] or uncertain,stopped_file_index=index+1,
                        reason='rejected' if value.get('approved') is False else 'file_error',
                        detail=detail(value),
                        error=f'LSP {label} stopped; inspect the changed prefix and backups before requesting a new operation')
                    if value.get('approved') is False:result['approved']=False
                    if uncertain:result['outcome']='uncertain'
                    return bounded(result)
        if expired():return bounded(result)
        return bounded(result)

    def file_action(self, action, path, read_id=None, destination=None):
        from .file_actions import execute
        return execute(self, action, path, read_id, destination)

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

    def glob(self, pattern, path=".", *, hidden=False, limit=100):
        self._require_tool("glob")
        matcher = FileGlob(pattern)
        if PureWindowsPath(pattern).drive:
            raise ToolError("glob pattern must be relative to the selected directory")
        if type(hidden) is not bool or type(limit) is not int or not 1 <= limit <= 1000:
            raise ToolError("glob hidden must be boolean and limit an integer from 1 to 1000")
        parts = self._parts(path)
        if len(parts) > 32:
            raise ToolError("glob scope exceeds 32 path components; narrow the workspace")
        return discover_files(self, parts, matcher, hidden, limit, skip_directory)

    def read_file(self, path, offset=1, limit=None, *, line_numbers=_MISSING, cursor=_MISSING, symbol=_MISSING,
                  ranges=_MISSING, image=False):
        self._require_tool("read_file")
        if type(image) is not bool:
            raise ToolError("image must be a boolean")
        if image:
            if (offset != 1 or type(offset) is not int or limit is not None
                    or any(value is not _MISSING for value in (line_numbers, cursor, symbol, ranges))):
                raise ToolError("image=true cannot be combined with text range, symbol or formatting options")
            return read_image(self, path)
        if type(offset) is not int or not 1 <= offset <= 1000000:
            raise ToolError("offset must be an integer from 1 to 1000000 (1-based line number)")
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10000):
            raise ToolError("limit must be an integer from 1 to 10000")
        if line_numbers is not _MISSING and type(line_numbers) is not bool:
            raise ToolError("line_numbers must be a boolean")
        if symbol is not _MISSING:
            if (not isinstance(symbol, str) or not symbol.strip() or len(symbol) > 1024
                    or any(ord(char) < 32 or ord(char) == 127 or 0xd800 <= ord(char) <= 0xdfff for char in symbol)
                    or len(symbol.encode("utf-8")) > 1024):
                raise ToolError("symbol must be an exact indexed name of at most 1024 UTF-8 bytes without control characters")
            if offset != 1 or limit is not None or cursor is not _MISSING:
                raise ToolError("symbol cannot be combined with offset, limit or cursor; continue using ordinary line reads")
        if cursor is not _MISSING and (offset != 1 or limit is not None or line_numbers is not _MISSING):
            raise ToolError("cursor cannot be combined with line options")
        if ranges is not _MISSING:
            if offset != 1 or limit is not None or cursor is not _MISSING or symbol is not _MISSING:
                raise ToolError("ranges cannot be combined with offset, limit, cursor or symbol; choose one read mode")
            if line_numbers is False:
                raise ToolError("ranges always returns numbered lines; omit line_numbers or set it to true")
        ranged = offset != 1 or limit is not None
        numbered = ranged if line_numbers is _MISSING else line_numbers
        parts = self._parts(path)
        if self.config.project_instructions:
            self.instructions.register(parts[:-1])
        if ranges is not _MISSING:
            return self._read_ranges(path, parts, ranges)
        if symbol is not _MISSING:
            return self._read_result(parts, self._read_symbol(path, symbol,
                                     line_numbers=True if line_numbers is _MISSING else line_numbers))
        if cursor is not _MISSING:
            return self._read_bytes(path, parts, cursor)
        if ranged or numbered:
            return self._read_result(parts, self._read_page(path, offset, limit or 2000, line_numbers=numbered))
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
        if truncated:
            result["read_guidance"] = ("Prefix only; no edit read_id. Use offset/limit for complete numbered lines, "
                                       "symbol for a known indexed declaration, or cursor='start' for byte pages, within scan limits.")
        if not truncated and not any(secret in text for secret in self.config.credential_secrets):
            result["sha256"] = hashlib.sha256(raw).hexdigest()
        return self._read_result(parts, result)

    def _read_result(self, parts, result):
        if "sha256" in result and not result.get("line_truncated"):
            result["read_id"] = self.read_versions.remember(parts, result["sha256"])
        return result

    def _read_validated(self, path, *, scan_limit=MAX_READ):
        # Validate the whole bounded file before exposing a page: selecting a
        # clean line must not bypass the credential/binary policy elsewhere.
        if type(scan_limit) is not int or not 0 < scan_limit <= MAX_READ:
            raise ToolError("invalid source scan limit")
        raw = bytearray()
        error = ("ranged read exceeds the 8 MiB scan limit" if scan_limit == MAX_READ else
                 "read exceeds the 512 KiB scan limit; use ordinary ranged reads or byte pages")
        with self._parent(path) as (directory, name), directory.open_read(name) as handle:
            if os.fstat(handle.fileno()).st_size > scan_limit:
                raise ToolError(error)
            while True:
                self._check_cancelled()
                chunk = handle.read(min(65536, scan_limit - len(raw) + 1))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > scan_limit:
                    raise ToolError(error)
        self._check_cancelled()
        text = self._decode(raw)
        return raw, text

    def verification_stamp(self, paths):
        """Fingerprint only explicitly selected files, with no model or edit handles."""
        if not self.config.tool_enabled("read_file"):
            raise ToolError("reading watched check files is disabled")
        stamp = []
        for path in paths:
            self._check_cancelled()
            parts = self._parts(path)
            with self._command_directory(parts[:-1]) as (parent, chain):
                name = parts[-1]
                named = parent.stat(name)
                with parent.open_read(name) as handle:
                    opened = os.fstat(handle.fileno())
                    if (not os.path.samestat(named, opened) or named.st_size != opened.st_size
                            or named.st_mtime_ns != opened.st_mtime_ns or opened.st_size > MAX_FILE):
                        raise ToolError("watched check file changed or exceeds 512 KiB")
                    raw = bytearray()
                    while len(raw) <= MAX_FILE:
                        self._check_cancelled()
                        chunk = handle.read(min(65536, MAX_FILE - len(raw) + 1))
                        if not chunk:
                            break
                        raw.extend(chunk)
                    if (len(raw) != opened.st_size or
                            self._revision(opened) != self._revision(os.fstat(handle.fileno()))):
                        raise ToolError("watched check file changed while reading")
                text = self._decode(raw)
                if any(secret in text for secret in self.config.secrets):
                    raise ToolError("watched check file contains a configured credential")
            self._parts(path)  # Privacy or path permissions may have changed during the read.
            with self._command_directory(parts[:-1], chain) as (parent, _):
                revision = self._revision(parent.stat(name))
            if revision != self._revision(named):
                raise ToolError("watched check file changed while reading")
            stamp.append((chain, revision, hashlib.sha256(raw).digest()))
        self._check_cancelled()
        return tuple(stamp)

    def revalidate_check_reads(self, entries, owner, revision):
        """Restore recent handles only after rereading their complete source."""
        config, version = owner
        verified = []
        for identity, (parts, digest) in entries[-MAX_CHECK_READ_VERSIONS:]:
            self._check_cancelled()
            if self._matching_read_version("/".join(parts), digest, config.credential_secrets):
                verified.append((identity, (parts, digest)))
        self._check_cancelled()
        if (self.config is config and self._conversation_version == version
                and self.jobs.revision == revision and not self.jobs.unsettled()):
            self.read_versions.restore_verified(verified)

    def _matching_read_version(self, path, digest, secrets):
        try:
            raw, text = self._read_validated(path, scan_limit=MAX_FILE)
            if any(secret in text for secret in secrets) or hashlib.sha256(raw).hexdigest() != digest:
                return False
            self._parts(path)  # Reload privacy rules after reading.
            return True
        except (OSError, ValueError, ToolError):
            return False

    def revalidate_resume_reads(self, observations):
        config, owner, revision = self.config, self._conversation_version, self.jobs.revision
        if (config.read_only or not config.tool_enabled("read_file") or not config.tool_enabled("edit_lines")
                or self.jobs.unsettled()):
            return {}
        self._require_tool("read_file")
        seen, verified = set(), {}
        secrets = config.secrets
        for observation in observations:
            self._check_cancelled()
            path, digest, _ = observation
            try:
                parts = self._parts(path)
            except (OSError, ValueError, ToolError):
                continue
            key = tuple(part.casefold() for part in parts) if os.name == "nt" else parts
            if key in seen or redact(path, secrets) != path:
                continue
            seen.add(key)
            if self._matching_read_version(path, digest, secrets):
                verified[path, digest] = parts
            if len(seen) >= MAX_RESUME_READS:
                break
        self._check_cancelled()
        if (self.config is not config or self._conversation_version != owner
                or self.jobs.revision != revision or self.jobs.unsettled()):
            return {}
        return {source: (self.read_versions.remember(parts, source[1]), parts)
                for source, parts in verified.items()}

    def _read_bytes(self, path, parts, cursor):
        if not isinstance(cursor, str) or len(cursor) > 80:
            raise ToolError("cursor must be 'start' or a returned next_cursor")
        offset, expected = 0, None
        if cursor != "start":
            version, separator, position = cursor.rpartition(":")
            if (not separator or len(version) != 65 or not version.startswith("b")
                    or any(char not in "0123456789abcdef" for char in version[1:])
                    or not position.isascii() or not position.isdecimal()
                    or len(position) > 7 or not 1 <= int(position) <= MAX_READ):
                raise ToolError("cursor must be 'start' or a returned next_cursor")
            expected = version[1:]
            offset = int(position)
        raw, text = self._read_validated(path)
        # Arbitrary byte boundaries could otherwise split one configured key
        # across separately redacted pages. Never expose such pages or hashes.
        if any(secret in text for secret in self.config.secrets):
            raise ToolError("file contains configured credentials or protected values; byte paging refused")
        digest = hashlib.sha256(raw).hexdigest()
        # Cursors are read-only content references, separate from edit read_ids.
        # Revalidate bytes and privacy even after a new run or restored session.
        version = hashlib.sha256("\0".join((*parts, digest)).encode("utf-8")).hexdigest()
        if expected is not None and version != expected:
            raise ToolError("file changed or cursor belongs to another path; restart with cursor='start'")
        if offset > len(raw) or (offset < len(raw) and (raw[offset] & 0xc0) == 0x80):
            raise ToolError("cursor must point to a UTF-8 character boundary within the file")
        import codecs
        end = min(len(raw), offset + self.config.max_output)
        content = codecs.getincrementaldecoder("utf-8")().decode(raw[offset:end], final=end == len(raw))
        end = offset + len(content.encode("utf-8"))
        self._check_cancelled()
        return {"path": path, "content": content, "byte_offset": offset, "end_byte_offset": end,
                "total_bytes": len(raw), "truncated": end < len(raw), "sha256": digest,
                "next_cursor": f"b{version}:{end}" if end < len(raw) else None}

    def _read_page(self, path, offset, limit, *, line_numbers=False):
        raw, text = self._read_validated(path)
        result = self._line_page(path, raw, text, offset, limit, line_numbers=line_numbers)
        requested_end = min(result["total_lines"], offset + limit - 1)
        result["range_complete"] = not result["line_truncated"] and result["end_line"] >= requested_end
        return result

    def _read_ranges(self, path, parts, ranges):
        if not isinstance(ranges, list) or not 1 <= len(ranges) <= MAX_READ_RANGES:
            raise ToolError("ranges requires one to eight [start, end] pairs")
        previous_end, line_count = 0, 0
        for pair in ranges:
            if (not isinstance(pair, list) or len(pair) != 2 or any(type(line) is not int for line in pair)
                    or not previous_end < pair[0] <= pair[1] <= 1000000):
                raise ToolError("ranges must be ordered, non-overlapping, inclusive lines from 1 to 1000000")
            start, previous_end = pair
            line_count += previous_end - start + 1
        if line_count > MAX_RANGE_LINES:
            raise ToolError("ranges selects more than 10000 lines; request fewer lines")

        raw, text = self._read_validated(path)
        return self._render_ranges(path, parts, ranges, raw, text)

    def _render_ranges(self, path, parts, ranges, raw, text, *, output_cap=None, **metadata):
        output_cap = self.config.max_output if output_cap is None else output_cap
        selected, content_bytes, range_index = [], 0, 0
        output_error = "ranges exceeds max-output; select fewer lines or matches, or use byte pages"
        if ranges:
            for number, line in enumerate(io.StringIO(text, newline=""), 1):
                if number % 256 == 0:
                    self._check_cancelled()
                start, end = ranges[range_index]
                if number < start:
                    continue
                displayed = f"{number}: {line}"
                content_bytes += len(displayed.encode("utf-8"))
                if content_bytes > output_cap:
                    raise _RangeOutputLimit(output_error)
                selected.append(displayed)
                if number == end:
                    range_index += 1
                    if range_index == len(ranges):
                        break
        if range_index != len(ranges):
            raise ToolError("a requested range extends beyond the end of the file")

        self._check_cancelled()
        self._parts(path)
        result = {"path": path, "content": "".join(selected), "ranges": ranges, "line_numbers": True,
                  "range_complete": True, **metadata}
        secrets = self.config.secrets

        def check_output(value):
            if max(encoded_size(value), encoded_size(redact(value, secrets))) > output_cap:
                raise _RangeOutputLimit(output_error)

        if not ranges or any(secret in text for secret in secrets):
            result.pop("next_cursor", None)
            check_output(result)
        else:
            result["sha256"] = hashlib.sha256(raw).hexdigest()
            result["read_id"] = self.read_versions.remember(
                parts, result["sha256"], validate=lambda identity: check_output({**result, "read_id": identity}))
        return result

    def _read_symbol(self, path, symbol, *, line_numbers):
        language = language_for(path)
        if language is None:
            raise ToolError("symbol reads require a supported source language; use ordinary line reads")
        raw, text = self._read_validated(path, scan_limit=MAX_FILE)
        try:
            records = self.index.symbols(text, language, self._check_cancelled, spans=True)
        except IndexUnavailable:
            raise ToolError("symbol parsing reached a resource limit; use ordinary line reads") from None
        if records is None:
            raise ToolError("file could not be parsed for symbols; use ordinary line reads")
        matches = [item for item in records if item["name"] == symbol]
        if not matches:
            return {"error": "symbol not found; use an exact project_map name or ordinary line reads", "symbol_match_count": 0}
        if len(matches) != 1:
            result = {"error": "symbol is ambiguous; use ordinary offset/limit reads", "symbol_match_count": len(matches),
                      "symbol_locations": [], "symbol_locations_truncated": True}
            for item in matches[:8]:
                location = {key: item[key] for key in ("start_line", "end_line")}
                candidate = {**result, "symbol_locations": [*result["symbol_locations"], location],
                             "symbol_locations_truncated": False}
                if max(encoded_size(candidate), encoded_size(redact(candidate, self.config.secrets))) > self.config.max_output:
                    break
                result = candidate
            result["symbol_locations_truncated"] = len(result["symbol_locations"]) < len(matches)
            return result
        selected = matches[0]
        result = self._line_page(path, raw, text, selected["start_line"],
                                 min(10000, selected["end_line"] - selected["start_line"] + 1), line_numbers=line_numbers)
        result.update(symbol=selected["name"], symbol_start_line=selected["start_line"], symbol_end_line=selected["end_line"],
                      symbol_complete=not result["line_truncated"] and result["end_line"] >= selected["end_line"])
        return result

    def _line_page(self, path, raw, text, offset, limit, *, line_numbers=False):
        selected, size, partial, capped, total = [], 0, False, False, 0
        # Iterate rather than allocating one list entry per line for a file
        # containing millions of blank lines. Keep CR/LF endings unchanged.
        for line in io.StringIO(text, newline=""):
            total += 1
            if total % 256 == 0:
                self._check_cancelled()
            if total < offset or total >= offset + limit or capped:
                continue
            displayed = f"{total}: " + line if line_numbers else line
            encoded = displayed.encode("utf-8")
            if size + len(encoded) > self.config.max_output:
                if not selected:
                    import codecs
                    selected.append(codecs.getincrementaldecoder("utf-8")().decode(
                        encoded[:self.config.max_output], final=False))
                    partial = True
                capped = True
                continue
            selected.append(displayed)
            size += len(encoded)
        self._check_cancelled()
        if offset > total and not (offset == 1 and not total):
            raise ToolError(f"offset {offset} is beyond the end of the file ({total} lines)")
        end = offset - 1 + len(selected)
        truncated = partial or end < total
        result = {"path": path, "content": "".join(selected), "offset": offset, "end_line": end,
                "total_lines": total, "truncated": truncated, "line_truncated": partial,
                "next_offset": end + 1 if truncated and not partial else None}
        if line_numbers:
            result["line_numbers"] = True
        # Never expose a fingerprint of configured credentials that were masked
        # from content, including a secret outside the requested line range.
        if not any(secret in text for secret in self.config.credential_secrets):
            result["sha256"] = hashlib.sha256(raw).hexdigest()
        return result

    def grep(self, pattern, path=".", *, glob=_MISSING, ignore_case=False, literal=False, context=0, limit=100,
             output_mode="content", cursor=_MISSING, matches_per_file=_MISSING):
        self._require_tool("grep")
        if type(literal) is not bool:
            raise ToolError("literal must be a boolean; false uses RE2 regex, true uses exact text")
        result = self.search(pattern, path, glob=glob, ignore_case=ignore_case, regex=not literal,
                             context=context, limit=limit, output_mode=output_mode, cursor=cursor,
                             matches_per_file=matches_per_file)
        # Legacy search's regex=true suggestion does not apply to this API.
        result.pop("search_guidance", None)
        return result

    def _search_cursor(self, parts, raw, query, ignore_case, context, regex, line):
        binding = (str(self.root.path), parts, hashlib.sha256(raw).hexdigest(), query,
                   ignore_case, context, regex, line)
        payload = json.dumps(binding, ensure_ascii=True, separators=(",", ":")).encode()
        return f"s{hashlib.sha256(payload).hexdigest()}:{line}"

    def _search_read(self, query, path, ignore_case, context, limit, matcher, cursor):
        self._require_tool("read_file")
        start_line = 1
        resume = cursor is not _MISSING and cursor != "start"
        if resume:
            invalid = "read cursor must be 'start' or a returned next_cursor"
            if not isinstance(cursor, str) or len(cursor) > 72:
                raise ToolError(invalid)
            signature, separator, position = cursor.rpartition(":")
            if (not separator or len(signature) != 65 or not signature.startswith("s")
                    or any(char not in "0123456789abcdef" for char in signature[1:])
                    or not position.isascii() or not position.isdecimal() or not 1 <= int(position) <= MAX_FILE):
                raise ToolError(invalid)
            start_line = int(position)
        parts = self._parts(path)
        if self.config.project_instructions:
            self.instructions.register(parts[:-1])
        raw, text = self._read_validated(path, scan_limit=MAX_FILE)
        def page_cursor(line):
            return self._search_cursor(parts, raw, query, ignore_case, context, matcher is not None, line)

        if resume and (cursor != page_cursor(start_line) or any(secret in text for secret in self.config.secrets)):
            raise ToolError("file or search settings changed; restart with cursor='start'")
        matches = matching_lines(text, query, ignore_case, context, self._check_cancelled, matcher,
                                 start_line=start_line)
        try:
            prefixes, following = match_window_prefixes(matches, limit)
        except RegexBudgetExceeded:
            return {"error": "read mode reached the regex work budget; no source window returned",
                    "truncated": True, "timed_out": True}
        return self._render_search_page(path, parts, raw, text, prefixes, following, page_cursor)

    def _render_search_page(self, path, parts, raw, text, prefixes, following, cursor, *, window_cap=None):
        count = len(prefixes)
        while True:
            ranges = prefixes[count - 1][1] if count else []
            next_line = prefixes[count][0] if count < len(prefixes) else following
            metadata = {"matched_lines": count, "truncated": next_line is not None}
            if window_cap is None:
                metadata.update(output_mode="read", timed_out=False)
            if next_line is not None:
                metadata["next_cursor"] = cursor(next_line)
            try:
                return self._render_ranges(path, parts, ranges, raw, text, output_cap=window_cap, **metadata)
            except _RangeOutputLimit:
                if count <= 1:
                    raise
                # Retry rendering smaller complete prefixes without repeating
                # matching or creating a read_id for rejected output.
                count = count - 1 if window_cap is not None else count // 2

    def search(self, query, path=".", *, glob=_MISSING, ignore_case=False, regex=False, context=0, limit=100,
               output_mode="content", cursor=_MISSING, matches_per_file=_MISSING):
        self._require_tool("search")
        if not isinstance(query, str) or not query or len(query) > 1000:
            raise ToolError("query must be a nonempty string (at most 1000 characters)")
        if not isinstance(ignore_case, bool):
            raise ToolError("ignore_case must be a boolean")
        if not isinstance(regex, bool):
            raise ToolError("regex must be a boolean")
        if isinstance(context, bool) or not isinstance(context, int) or not 0 <= context <= 10:
            raise ToolError("context must be an integer between 0 and 10; use bounded search to find lines, "
                            "then read_file offset/limit or symbol for more surrounding code")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ToolError("limit must be an integer between 1 and 1000")
        if not isinstance(output_mode, str) or output_mode not in ("content", "files", "count", "read", "windows"):
            raise ToolError("output_mode must be content, files, count, read or windows")
        if output_mode in ("files", "count") and context:
            raise ToolError("files/count output requires context=0; use content or read for surrounding lines")
        if cursor is not _MISSING and output_mode != "read":
            raise ToolError("cursor requires output_mode='read'")
        if matches_per_file is not _MISSING and (output_mode != "windows"
                or type(matches_per_file) is not int or not 1 <= matches_per_file <= MAX_WINDOW_MATCHES):
            raise ToolError("matches_per_file requires output_mode='windows' and an integer from 1 to 8")
        if output_mode == "read":
            if glob is not _MISSING:
                raise ToolError("read mode requires a single explicit file path; omit glob")
            matcher = RegexMatcher(query, ignore_case, self._check_cancelled) if regex else None
            return self._search_read(query, path, ignore_case, context, limit, matcher, cursor)
        if output_mode == "windows":
            self._require_tool("read_file")
            limit = min(limit, MAX_SEARCH_WINDOW_FILES)
            if matches_per_file is _MISSING:
                matches_per_file = 1
        file_glob = FileGlob(glob) if glob is not _MISSING else None
        matcher = RegexMatcher(query, ignore_case, self._check_cancelled) if regex else None
        parts = self._parts(path)
        field = "matches" if output_mode == "content" else "files"
        result = {field: [], "truncated": False, "skipped_files": 0,
                  "files_read": 0, "bytes_read": 0, "entries_scanned": 0}
        if output_mode != "content":
            result["output_mode"] = output_mode
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

        def add_record(record):
            nonlocal remaining, stopped
            size = measure(record) + 2
            if len(result[field]) >= limit or size > remaining:
                result["truncated"] = stopped = True
                return False
            result[field].append(record)
            remaining -= size
            return True

        def add_window(path, file_parts, raw, text, matches):
            nonlocal stopped
            prefixes, following = match_window_prefixes(matches, matches_per_file)
            if not prefixes:
                return
            if len(result[field]) >= limit:
                result["truncated"] = stopped = True
                return
            def page_cursor(line):
                return self._search_cursor(file_parts, raw, query, ignore_case, context, regex, line)
            if self.config.project_instructions:
                self.instructions.register(file_parts[:-1])
            try:
                record = self._render_search_page(path, file_parts, raw, text, prefixes, following, page_cursor,
                                                  window_cap=min(MAX_SEARCH_WINDOW_BYTES, remaining - 2))
            except _RangeOutputLimit:
                # Keep a location even when one complete window cannot fit.
                result["truncated"] = True
                self._parts(path)
                record = {"path": path, "line": prefixes[0][0], "window_omitted": True}
            add_record(record)

        def scan_file(directory, name, prefix):
            nonlocal budget, stopped
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
                matched = matching_lines(text, query, ignore_case, context, self._check_cancelled, matcher)
                path = "/".join((*prefix, name))
                if output_mode == "windows":
                    add_window(path, (*prefix, name), raw, text, matched)
                elif output_mode == "content":
                    for hit in matched:
                        self._check_cancelled()
                        if not add_record({"path": path, **hit}):
                            return
                else:
                    count = 0
                    for _ in matched:
                        self._check_cancelled()
                        count += 1
                        if output_mode == "files":
                            break
                    # Counts describe a complete file scan. A timeout below
                    # must not publish a partial count as if it were exact.
                    if count:
                        add_record({"path": path, **({"count": count} if output_mode == "count" else {})})
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
        with self._scope_parent(parts) as (parent, info):
            if info is not None and stat.S_ISREG(info.st_mode):
                scan_file(parent, parts[-1], parts[:-1])
            else:
                with self._directory(parts) as fd:
                    walk(fd, parts, 0)
        self._check_cancelled()
        if output_mode == "windows" and result[field]:
            self.policy.reload()
            visible = []
            for record in result[field]:
                file_parts = PurePosixPath(record["path"]).parts
                if self.policy.excluded(file_parts):
                    self.read_versions.discard(file_parts)
                    result["truncated"] = True
                    result["skipped_files"] += 1
                else:
                    visible.append(record)
            result[field] = visible
        if not result[field] and not regex and any(char in query for char in "|^$[]().*+?{}\\"):
            guidance = "Literal query; set regex=true for patterns. Not retried."
            if measure({**result, "search_guidance": guidance}) <= self.config.max_output:
                result["search_guidance"] = guidance
        return result

    def project_map(self, path=".", query="", include_calls=False):
        self._require_tool("project_map")
        self._check_cancelled()
        if type(include_calls) is not bool:
            raise ToolError("include_calls must be a boolean")
        if not isinstance(query, str) or len(query.encode("utf-8")) > 256 or any(ord(char) < 32 or ord(char) == 127 for char in query):
            raise ToolError("query must be a literal string of at most 256 UTF-8 bytes without control characters")
        if query and not query.strip():
            raise ToolError("query must contain a non-whitespace character")
        query = query.strip().casefold()
        if include_calls and not query.replace("$", "_").isidentifier():
            raise ToolError("include_calls requires query to be one simple identifier, without a path or qualified name")
        def measure(record):
            size = encoded_size(record)
            if self.config.secrets:
                size = max(size, encoded_size(redact(record, self.config.secrets)))
            return size

        selection = MapSelection(self.config.max_output, measure, include_calls=include_calls) if query else None
        parts = self._parts(path)
        files, scanned, budget, truncated = [], 0, 8 * 1024 * 1024, False
        # Reserve the complete envelope; redaction can expand short credentials.
        size = measure({"files": [], "truncated": False})

        def scan_file(directory, name, prefix):
            nonlocal budget, size, truncated
            record = {"path": "/".join((*prefix, name))}
            language = language_for(name)
            if language:
                raw = self._read(directory, name, min(MAX_FILE, budget))
                self._check_cancelled()
                budget -= len(raw)
                if len(raw) > MAX_FILE or budget < 0:
                    truncated = True
                    record["symbols_unavailable"] = True
                else:
                    try:
                        text = self._decode(raw)
                        if include_calls and any(secret in text for secret in self.config.secrets):
                            raise ToolError("configured credential blocks call-site indexing")
                        symbols = self.index.symbols(text, language, self._check_cancelled)
                        self._check_cancelled()
                        if symbols is None:
                            record["symbols_unavailable"] = True
                        else:
                            record["symbols"] = symbols
                            if include_calls:
                                sites = self.index.call_sites(text, language, self._check_cancelled)
                                if sites is None:
                                    record["symbols_unavailable"] = True
                                    truncated = True
                                else:
                                    record["symbols"] = sorted([*symbols, *(item for item in sites["calls"]
                                        if item["name"].casefold() == query)],
                                        key=lambda item: (item["line"], item.get("column", 0)))
                                    truncated |= sites["unresolved"]
                    except IndexUnavailable:
                        truncated = True
                        record["symbols_unavailable"] = True
                    except (ValueError, ToolError):
                        record["symbols_unavailable"] = True
            if selection is not None:
                selection.add(record, query, self._check_cancelled)
                return True
            record_size = measure(record) + (2 if files else 0)
            if size + record_size > self.config.max_output:
                # Keep a large module discoverable even when its symbol list is too big.
                record = {"path": record["path"], "symbols_unavailable": True}
                record_size = measure(record) + (2 if files else 0)
                truncated = True
            if size + record_size > self.config.max_output:
                return False
            files.append(record)
            size += record_size
            return True

        def walk(directory, prefix, depth):
            nonlocal scanned, truncated
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
                        if not scan_file(directory, entry.name, prefix):
                            return
                    except (OSError, ToolError):
                        truncated = True

        with self._scope_parent(parts) as (parent, info):
            if info is not None and stat.S_ISREG(info.st_mode):
                # A file lookup is not a complete sweep: retain unrelated cache entries.
                scan_file(parent, parts[-1], parts[:-1])
                self._parts(path)  # Recheck privacy rules before publishing this explicit file.
            else:
                with self._directory(parts) as directory, self.index.scan(
                        lambda: not truncated, modes=(b"\0", b"\0calls\0") if include_calls else (b"\0",)):
                    walk(directory, parts, 0)
        self._check_cancelled()
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

    def _replace(self, fd, name, data, mode, before_stat, *, private=False, before_replace=None):
        if data is None:
            if before_replace is not None:
                before_replace()
            fd.unlink(name)
        else:
            temp = f".sprig-{uuid.uuid4().hex}.tmp"
            try:
                fd.write_new(temp, data, mode, private=private, permissions_from=name if before_stat else None)
                if before_replace is not None:
                    before_replace()
                fd.replace(temp, name)
            finally:
                try:
                    fd.unlink(temp)
                except FileNotFoundError:
                    pass
        fd.sync()

    def review_changes(self, identity=None, offset=0, snapshot=None, *, scopes=None):
        """User-local read-only inspection; not a model tool or workspace action."""
        from .change_review import catalog, page
        try:
            if identity is None and (type(offset) is not int or offset != 0 or snapshot is not None):
                raise ValueError("catalog inspection does not accept a page offset or snapshot")
            if identity is not None and scopes is not None:
                raise ValueError("select either a retained edit or a requirement's scope")
            return catalog(self, scopes=scopes) if identity is None else page(self, identity, offset, snapshot)
        except (OSError, ToolError, ChangeError, ValueError, TypeError, RuntimeError, RecursionError) as error:
            result = {"error": terminal_text(redact(str(error), self.config.secrets)), "network_contacted": False}
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > min(self.config.max_output, 16384):
                return {"error": "change review exceeds the output budget"}
            return result

    def change_action(self, action, *, target=None):
        """Local user controls only; deliberately absent from model tool schemas."""
        try:
            self._check_cancelled()
            if action == "changes":
                result = self.history.summary()
            elif action == "discard-history":
                description = "DISCARD exact edit backups (workspace files unchanged). Undo/redo will no longer be available.\n" + json.dumps(self.history.summary(), ensure_ascii=False)
                if not self.request_approval(description):
                    result = {"approved": False}
                else:
                    self._check_cancelled()
                    self.history.discard()
                    result = {"approved": True, "discarded": True}
            elif action in {"undo-batch", "redo-batch", "undo-to", "redo-to"}:
                from .batch_restore import restore
                return restore(self, action, target=target)
            else:
                result = self._restore_change(action)
            return redact(result, self.config.secrets)
        except (OSError, ToolError, ChangeError, ValueError) as exc:
            return {"error": redact(str(exc), self.config.secrets)}

    def _restore_change(self, action, *, before_write=None, description_prefix=""):
        self._require_tool("edit_file")
        if getattr(self, "_restoring_batch", False) and before_write is None:
            raise ToolError("batch recovery is in progress")
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
            if os.name == "nt" and info is None and target is not None:
                description += "\nRe-created Windows file uses a private current-user ACL; previous shared access is not restored."
            if not self.request_approval(description_prefix + description):
                return {"approved": False, "changed": False}
            if before_write is not None:
                before_write()
            self._parts(path)  # File privacy rules may have changed during review.
            self._unchanged(fd, name, current, info)
            self._check_cancelled()
            try:
                self.read_versions.discard(self._parts(path))
                self.history.begin_restore(action, path)
                self._replace(fd, name, target, record["mode"], info,
                              private=os.name == "nt" and info is None and target is not None)
                self.history.complete(self._revision(fd.stat(name)) if target is not None else None)
            except (OSError, ChangeError) as error:
                return {"approved": True, "changed": False, "outcome": "uncertain", "path": path, "action": action,
                        "error": "Restore outcome is uncertain; inspect current files and the pending backup before continuing: " + str(error)}
        return {"approved": True, "changed": True, "path": path, "action": action,
                "file_removed": target is None,
                "note": "Workspace changed; previous model conclusions and acceptance results need rechecking."}

    def edit_lines(self, path, read_id=_MISSING, edits=_MISSING, patch=_MISSING):
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
        if (edits is _MISSING) == (patch is _MISSING):
            raise ToolError("edit_lines requires exactly one of edits or patch")
        result = self.edit_file(path, sha256=digest, line_edits=edits, line_patch=patch)
        if isinstance(result, dict) and "edit_failure" in result:
            result["edit_failure"] = {**result["edit_failure"], "source": "read_id"}
        return result

    def _edit_syntax(self, path, before, after, data):
        language = language_for(path)
        if language is None:
            return None

        def inspect(text):
            try:
                return self.index.syntax.diagnostics(text, language, self._check_cancelled)
            except (IndexUnavailable, RecursionError, MemoryError):
                return {"status": "unavailable", "reason": "parse_limit"}
            except (ImportError, ValueError, RuntimeError):
                return {"status": "unavailable", "reason": "parser_unavailable"}

        report = {"language": language, "parser": "python_ast" if language == "python" else "tree_sitter",
                  "before": {"status": "absent"} if before is None else inspect(before), "after": inspect(after),
                  "sha256": hashlib.sha256(data).hexdigest(), "not_acceptance": True}
        if language == "python":
            report["python_version"] = ".".join(map(str, sys.version_info[:3]))
        return report

    def edit_file(self, path, old=_MISSING, new=_MISSING, *, edits=_MISSING, sha256=_MISSING, line_edits=_MISSING, line_patch=_MISSING):
        self._require_tool("edit_file")
        if self.config.read_only:
            return {"approved": False, "changed": False, "error": "read-only mode blocks file writes"}
        multiple = edits is not _MISSING
        by_line = line_edits is not _MISSING or line_patch is not _MISSING
        if by_line:
            if multiple or old is not _MISSING or new is not _MISSING:
                raise ToolError("choose old/new, edits, or sha256 plus line_edits")
            if sha256 is _MISSING:
                return {"error": "line_edits require full sha256 from read_file; prefer edit_lines with read_id", "changed": False,
                        "edit_failure": {"reason": "missing_revision", "source": "sha256"}}
            if not isinstance(sha256, str) or len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
                return {"error": "sha256 must be 64 lowercase hex characters from read_file; prefer edit_lines with read_id", "changed": False,
                        "edit_failure": {"reason": "invalid_revision", "source": "sha256"}}
            if line_patch is not _MISSING:
                if line_edits is not _MISSING:
                    raise ToolError("choose line_edits or line_patch, not both")
            else:
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
            if self._edit_batch is not None:
                self._edit_batch.check_source(self, parts, before_raw, before_stat)
            if by_line:
                if before_stat is None:
                    raise ToolError("line_edits require an existing file")
                if hashlib.sha256(before_raw).hexdigest() != sha256:
                    return {"error": "file revision changed; read the file again before proposing line_edits", "changed": False,
                            "edit_failure": {"reason": "stale_revision"}}
                after = (apply_line_patch(before, line_patch, MAX_FILE, self._check_cancelled) if line_patch is not _MISSING
                         else replace_lines(before, line_edits, self._check_cancelled))
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
            if self._edit_preview:
                return EditPreview(path, parts, self._revision(before_stat), digest(before_raw), digest(data), rules)
            if self._edit_batch is not None:
                self._edit_batch.check_proposal(parts, data)
            if before_stat is not None and before == after:
                return {"changed": False}
            diff = self._diff(path, before if before_stat else None, after)
            config, version = self.config, self._conversation_version
            if not self.request_approval(f"WRITE {json.dumps(path)}\n{diff or '(create empty file)'}"):
                return {"approved": False, "changed": False}
            # Parse only the already approved proposal, before side effects.
            # Cancellation and fresh identity checks still precede the write.
            syntax = self._edit_syntax(path, before if before_stat else None, after, data)
            # Recheck after approval: an editor may have changed the file while the user read the diff.
            self._parts(path)
            self.instructions.recheck_action(parts[:-1], rules)
            self._unchanged(fd, name, before_raw, before_stat)
            if self._edit_batch is not None:
                self._edit_batch.check_pending(self)
            self._check_cancelled()
            self.read_versions.discard(parts)
            mode = stat.S_IMODE(before_stat.st_mode) & 0o777 if before_stat else 0o600
            self.history.begin_edit(path, before_raw, data, mode, self._revision(before_stat),
                                    batch=self._edit_batch.backup_label() if self._edit_batch is not None else None)
            self._replace(fd, name, data, mode, before_stat)
            self.history.complete(self._revision(fd.stat(name)))
        result = {"approved": True, "changed": True, "path": path}
        written_digest = digest(data)
        if by_line:
            result["sha256"] = written_digest
        if syntax is not None:
            result["syntax"] = syntax
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > self.config.max_output:
                result["syntax"] = {"status": "omitted", "reason": "output_limit", "not_acceptance": True}
                if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > self.config.max_output:
                    del result["syntax"]
        if (self.config is config and self._conversation_version == version
                and config.tool_enabled("read_file") and config.tool_enabled("edit_lines")
                and not any(secret in after for secret in config.secrets)):
            def fit(identity):
                candidate = {**result, "read_id": identity}
                if max(encoded_size(candidate), encoded_size(redact(candidate, config.secrets))) > config.max_output:
                    raise ToolError("edit result has no space for a read identifier")

            try:
                new_id = self.read_versions.remember(parts, written_digest, validate=fit)
            except ToolError:
                pass
            else:
                result["read_id"] = new_id
        return result

    def mcp(self, action="status", **arguments):
        self._require_tool("mcp")
        try:
            return self.mcp_manager.control(action, **arguments)
        finally:
            if action not in {"status", "result"}:
                self.read_versions.clear()  # Includes uncertain/cancelled external delivery.

    def skill(self, name, path="SKILL.md", offset=0, page_sha256=None):
        self._require_tool("skill")
        return self.skills.load(name, path, offset, page_sha256=page_sha256,
                                model_action=self.instructions.model_action_active)

    def question(self, questions):
        self._require_tool("question")
        questions = validate_questions(questions, self.config.secrets)
        if not callable(self.ask):
            return {"answered": False, "error": "interactive questions are unavailable; wait for explicit user input instead of assuming a choice",
                    "not_authorization": True}
        self._check_cancelled()
        answers = self.ask(questions)
        self.root.ensure_open()
        self._check_cancelled()
        if answers is None:
            return {"answered": False, "dismissed": True, "not_authorization": True}
        return answer_result(questions, answers, self.config.max_output, self.config.secrets)

    def read_log(self, log_id, offset=0, *, query=None):
        self._require_tool("read_log")
        return self.logs.read(log_id, offset, query=query)

    def log_control(self, action="list", **arguments):
        self.root.ensure_open()
        self._check_cancelled()
        if action == "list":
            return self.logs.list()
        if action == "read":
            return self.logs.read(**arguments)
        if action == "clear":
            return self.logs.clear(self.request_approval)
        raise ToolError("unknown command log operation")

    def _retain_log(self, preview, capture, result):
        if capture is None:
            return result
        self._check_cancelled()
        text, limited = capture.finish()
        if "unavailable" in preview or type(result.get("returncode")) is not int:
            result["log"] = {"saved": False, "reason": "log storage unavailable or command did not produce an exit status"}
            return result
        try:
            result["log"] = self.logs.save(preview, text, limited, result)
        except ValueError as error:
            result["log"] = {"saved": False, "reason": str(error)}
        except (OSError, ChangeError):
            # Command outcome remains authoritative even if optional logging
            # fails, including an uncertain replace/sync outcome.
            result["log"] = {"saved": False, "outcome": "unknown", "reason": "command log save failed; inspect /logs before relying on retention"}
        return result

    def _finish_foreground(self, result, stream, preview, capture, memory_generation):
        summary = stream.finish()
        if summary is not None:
            if memory_generation is not None and summary["output_truncated"]:
                result["log"] = self.logs.retain_memory(memory_generation, result)
            result.update(summary)
        if self.emit_output:
            result["streamed"] = True
        if preview is not None and capture is None:
            result["log"] = {"saved": False, "reason": "command log storage unavailable before approval"}
        return self._retain_log(preview, capture, result)

    def run(self, argv, cwd=".", timeout=None, background=False, stdin="closed", *, backup=_MISSING):
        self._require_tool("run")
        if self.config.read_only:
            return {"approved": False, "error": "read-only mode blocks command execution"}
        if type(background) is not bool:
            raise ToolError("background must be a boolean")
        if not isinstance(stdin, str) or stdin not in {"closed", "pipe", "pty"} or stdin != "closed" and not background:
            raise ToolError("stdin must be closed or an explicitly opted-in background pipe or PTY")
        if backup is not _MISSING and background:
            raise ToolError("backup is available only for foreground commands")
        job_epoch = None
        if background:
            self.jobs.available()
            if stdin == 'pty' and not self.jobs.terminal_enabled():
                raise ToolError('owned terminal sessions are unavailable on this host')
            job_epoch = self.jobs.epoch
        if (not isinstance(argv, list) or not argv or len(argv) > 256
                or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]):
            raise ToolError("argv must be a nonempty array of strings (at most 256)")
        if sum(len(arg) for arg in argv) > 65536:
            raise ToolError("argv is too large")
        argv = list(argv)  # Approval callbacks cannot mutate the proposed argv.
        if any(secret in arg for secret in self.config.credential_secrets for arg in argv):
            raise ToolError("configured API key must not appear in command arguments")
        maximum = self.config.background_timeout if background else self.config.timeout
        seconds = maximum if timeout is None else timeout
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or not 0 < seconds <= maximum):
            raise ToolError(f"command timeout must be positive and at most {maximum:g} seconds; omit timeout to use that configured limit")
        parts = self._parts(cwd)
        with ExitStack() as directories:
            pinned, cwd_chain = directories.enter_context(self._command_directory(parts))
            observation = None
            if backup is not _MISSING:
                from .command_changes import CommandChanges
                observation = CommandChanges(self, backup)
            return self._run_in_directory(argv, parts, pinned, cwd_chain, seconds, stdin, job_epoch, directories, observation)

    def _run_in_directory(self, argv, parts, pinned, cwd_chain, seconds, stdin, job_epoch, directories, observation):
        config, version = self.config, self._conversation_version
        background = job_epoch is not None
        sandbox = None
        if config.command_sandbox == "bubblewrap":
            sandbox = Sandbox(config, self.root, pinned, self.policy, self._check_cancelled)
        directory = pinned.path
        rules = self.instructions.prepare_action(parts)
        output_limit = self.config.command_output_limit or self.config.max_output
        protection = sandbox.description() if sandbox else "RUN (NOT SANDBOXED; may access outside files/network)"
        description = (f"{protection}\n"
                       f"cwd: {json.dumps(str(directory))}\nargv: {json.dumps(argv)}\n"
                       f"timeout: {seconds}s; output cap: {output_limit} bytes")
        if output_limit > self.config.max_output:
            description += f"; displayed output cap: {self.config.max_output} bytes, head/tail with omission notice"
        if sandbox is None and (self.config.env_allowlist or self.config.command_env or self.config.inherit_env):
            description += ("\nenvironment: " + ("expanded inheritance" if self.config.inherit_env else "minimal inheritance")
                            + "; additional inherited names: " + json.dumps(self.config.env_allowlist)
                            + "; override names: " + json.dumps([name for name, _ in self.config.command_env]))
        if observation is not None:
            description += observation.description()
        preview = memory_generation = None
        if background:
            modes = {
                'closed': 'Non-interactive stdin is closed. ',
                'pipe': 'STDIN PIPE stays open. Text and EOF each require fresh approval. ',
                'pty': 'PTY starts at 80 columns × 24 rows with merged output. Input and resize each require fresh approval. ',
            }
            description += ("\nBACKGROUND: returns an owned job handle. "
                            + modes[stdin]
                            + ('' if stdin == 'closed' else 'Input is limited to 4 KiB per approval and 64 KiB per job. ')
                            + "At most four active / eight retained records, <=1 MiB redacted output per job in memory. "
                            "Completion only notifies locally, without automatic model requests or acceptance. "
                            "Cancellation/reset/exit stops the process tree. Not resumed or replayed after restore.")
        if self.config.command_log_dir is not None:
            try:
                preview = self.logs.preview(background=True) if background else self.logs.preview()
            except (OSError, ValueError):
                preview = {"unavailable": True}
            description += ("\nRETAIN redacted command output in private storage: " + str(self.config.command_log_dir)
                            + "; up to 8 MiB per log / 16 records / 16 MiB journal; readback expires in 7 days, "
                            "payload remains until approved save/clear; temporary payload up to 16 MiB. "
                            + ("BACKGROUND: append only after exit; no eviction or stale pending cleanup. Up to 8 MiB extra capture memory per active job. "
                               "Concurrent appends allowed; removed/changed approved records, quota, cancellation or owner changes refuse retention. "
                               "A crash before completion loses the uncommitted output. " if background else
                               "Expired/old records may be removed as needed, plus the fixed stale pending file. ")
                            + "No command is replayed. Retention candidates (IDs only): "
                            + json.dumps([] if background else preview.get("retention_candidates", [])))
            if "unavailable" in preview:
                description += "\nLogging is unavailable; this command will run without a retained log."
        elif not background and output_limit > self.config.max_output:
            memory_generation, previous = self.logs.memory_preview()
            description += ("\nIf the output preview omits text, keep up to 1 MiB of redacted output in this conversation's memory. "
                            "Pages read by the model may be saved in session history.")
            if previous is not None:
                description += " This may replace in-memory log " + previous + "."
        if not self.request_approval(description):
            return {"approved": False}
        self._require_tool("run")
        if self.config is not config or self._conversation_version != version:
            raise ToolError("command owner or policy changed; request fresh approval")
        self._parts("/".join(parts) or ".")
        if any(secret in arg for secret in self.config.credential_secrets for arg in argv):
            raise ToolError("configured credentials cannot appear in command argv")
        env = {} if sandbox else command_environment(self.config)
        self.instructions.recheck_action(parts, rules)
        current, _ = directories.enter_context(self._command_directory(parts, cwd_chain))
        if observation is not None:
            observation.check_before()
        launch = sandbox.launch(argv) if sandbox else self._command_launch(current, argv, env)
        result = self._execute_command(argv, directory, cwd_chain, launch, seconds, output_limit, stdin, job_epoch,
                                       preview, memory_generation, directories)
        if observation is not None:
            result["command_changes"] = (observation.finish() if type(result.get("returncode")) is int
                                         else {"recorded": 0, "status": "unavailable"})
        return result

    def _execute_command(self, argv, directory, cwd_chain, launch, seconds, output_limit, stdin, job_epoch,
                         preview, memory_generation, directories):
        background = job_epoch is not None
        if background:
            self.read_versions.clear()
            try:
                log = BackgroundLog(self.logs, preview) if preview is not None and "unavailable" not in preview else None
                status = ({"saved": False, "pending": True} if log is not None else
                          {"saved": False, "reason": "command log storage unavailable before approval"} if preview is not None else None)
                with launch as command:
                    return self.jobs.start(argv, directory, cwd_chain, command, seconds, output_limit,
                                           epoch=job_epoch, stdin=stdin, log=log, log_status=status,
                                           directory_guard=directories if os.name == "nt" else None)
            finally:
                self.read_versions.clear()
        sampled = output_limit > self.config.max_output
        stream = (SampledCommandOutput(self.emit_output, self.config.secrets, self.config.max_output) if sampled
                  else CommandOutput(self.emit_output, self.config.secrets))
        capture = LogCapture(self.config.secrets) if preview is not None and "unavailable" not in preview else None
        def emit_chunk(data):
            stream.write(data)
            if capture is not None:
                capture.write(data)
        self.read_versions.clear()
        if os.name == "nt":
            from .windows import run_command
            try:
                with launch as command:
                    result = run_command(command.argv, command.cwd, command.env, seconds, output_limit,
                                         self.cancel_event, emit_chunk)
            finally:
                self.read_versions.clear()
            return self._finish_foreground(result, stream, preview, capture, memory_generation)
        with launch as command:
            process = subprocess.Popen(command.argv, cwd=command.cwd, env=command.env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True, shell=False, pass_fds=command.pass_fds)
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
                        chunk = os.read(key.fileobj.fileno(), min(65536, output_limit - len(output) + 1))
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            space = output_limit - len(output)
                            output.extend(chunk[:space])
                            emit_chunk(chunk[:space])
                            if len(chunk) > space:
                                reason = "output_limit"
                                break
                    if reason:
                        break
                if not reason:
                    while poll_unreaped(process) is None:
                        self._check_cancelled()
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            reason = "timeout"
                            break
                        time.sleep(min(0.01, remaining))
        finally:
            self.read_versions.clear()
            try:
                finish_session(process)
            finally:
                process.stdout.close()
                self.read_versions.clear()
        result = {"approved": True, "returncode": process.returncode, "streamed": bool(self.emit_output),
                "output": output.decode("utf-8", errors="replace"),
                "timed_out": reason == "timeout", "output_limited": reason == "output_limit"}
        return self._finish_foreground(result, stream, preview, capture, memory_generation)
