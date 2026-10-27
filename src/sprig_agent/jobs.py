"""Conversation-owned background commands; no restart, detached PID or saved authority."""
from collections import OrderedDict
import codecs
from dataclasses import dataclass
import hashlib
import json
import math
import os
import threading
import time
import uuid

from .config import redact, terminal_text
from .mcp_process import Process
from .streaming import TextOutput

MAX_ACTIVE, MAX_RECORDS, MAX_RETAINED = 4, 8, 1024 * 1024
MAX_INPUT, MAX_JOB_INPUT, INPUT_TIMEOUT = 4096, 65536, 2
INPUT_FAILURE = "background stdin delivery failed; effects may be incomplete; no input was replayed"
JOB_SCHEMA = {"type": "function", "function": {
    "name": "job", "description": "Owned background commands: list/status/output/wait (<=30s); freshly approve cancel, input (exact UTF-8 <=4KiB), or eof. Input requires run(background=true,stdin='pipe'); no PTY/replay. Output offsets are UTF-8 bytes; keep end for a stable prefix. Exit is not fixed acceptance.",
    "parameters": {"type": "object", "properties": {
        "action": {"enum": ["list", "status", "output", "wait", "cancel", "input", "eof"]},
        "job_id": {"type": "string", "maxLength": 40},
        "text": {"type": "string", "minLength": 1, "maxLength": MAX_INPUT},
        "offset": {"type": "integer", "minimum": 0, "maximum": MAX_RETAINED},
        "end": {"type": "integer", "minimum": 0, "maximum": MAX_RETAINED},
        "seconds": {"type": "number", "minimum": 0, "maximum": 30}},
        "required": ["action"], "additionalProperties": False}}}


@dataclass(frozen=True, repr=False)
class InputProposal:
    row: object
    epoch: int
    version: int
    config: object
    data: object


class Output:
    def __init__(self, secrets):
        self.decoders = {name: codecs.getincrementaldecoder("utf-8")(errors="replace") for name in ("stdout", "stderr")}
        self.data = bytearray()
        self.limited, self.suppressed = False, False
        self.streams = {name: TextOutput(self._accept, secrets) for name in self.decoders}

    def _accept(self, text):
        if self.suppressed:
            return
        raw = text.encode("utf-8")
        remaining = MAX_RETAINED - len(self.data)
        self.data.extend(raw[:remaining])
        self.limited |= len(raw) > remaining

    def write(self, channel, raw):
        self.streams[channel].write(self.decoders[channel].decode(raw, final=False))

    def finish(self):
        for name, stream in self.streams.items():
            stream.write(self.decoders[name].decode(b"", final=True))
            stream.finish()

    def text(self, secrets):
        text = self.data.decode("utf-8", errors="ignore")
        if redact(text, secrets) != text:
            self.data.clear()
            self.suppressed = True
            return ""
        return text


class Job:
    def __init__(self, identity, process, label, truncated, seconds, cap, secrets, directory, identity_cwd, stdin):
        self.identity, self.process, self.label = identity, process, label
        self.seconds, self.cap = seconds, cap
        self.command_truncated = truncated
        self.started = time.monotonic()
        self.cancel, self.done = threading.Event(), threading.Event()
        self.state, self.code, self.bytes = "running", None, 0
        self.output = Output(secrets)
        self.thread = None
        self.error = None
        self.cwd, self.directory_identity = directory, identity_cwd
        self.stdin_pipe, self.stdin_open = stdin == "pipe", stdin == "pipe"
        self.stdin_bytes, self.input_version = 0, 0
        self.input_failure = False
        self.input_stop = False
        self.input_lock = threading.Lock()


class Jobs:
    def __init__(self, config, check):
        self.config, self.check = config, check
        self.records = OrderedDict()
        self.lock = threading.RLock()
        self._reset_lock = threading.Lock()
        self.resetting, self.closed, self.epoch = False, False, 0
        self.revision = 0

    def enabled(self):
        return (self.config.background_enabled and not self.config.read_only
                and self.config.tool_enabled("run") and self.config.tool_enabled("job"))

    def available(self):
        self.check()
        with self.lock:
            if self.closed or self.resetting:
                raise ValueError("background owner is closed or resetting; no new command may start")
            if not self.enabled():
                raise ValueError("background commands require execution.background, run/job tools and edit mode")
            if self.config.max_output < 1024:
                raise ValueError("background commands require max_output >= 1024")
            if sum(row.state == "running" for row in self.records.values()) >= MAX_ACTIVE:
                raise ValueError("at most four background commands may run; wait or cancel an owned task")
            if any(row.state == "cleanup_failed" for row in self.records.values()):
                raise ValueError("background cleanup is uncertain; inspect and close this conversation before starting commands")

    def start(self, argv, directory, env, seconds, cap, *, epoch, stdin="closed"):
        if not isinstance(stdin, str) or stdin not in {"closed", "pipe"}:
            raise ValueError("invalid background stdin mode")
        self.available()
        with self.lock:
            self.available()
            if epoch != self.epoch:
                raise ValueError("background proposal expired after conversation reset; request fresh approval")
            while len(self.records) >= MAX_RECORDS:
                old = next((key for key, row in self.records.items() if row.done.is_set()), None)
                if old is None:
                    raise ValueError("background record capacity reached")
                del self.records[old]
            info = directory.stat()
            identity_cwd = (info.st_dev, info.st_ino)
            process = Process(argv, directory, env)
            try:
                if stdin == "closed":
                    process.close_input()
                self.check()
                label = terminal_text(redact(json.dumps([os.path.basename(argv[0]), *argv[1:]], ensure_ascii=False), self.config.secrets)).encode("utf-8")
                row = Job("job-" + uuid.uuid4().hex, process, label[:160].decode("utf-8", errors="ignore"), len(label) > 160,
                          seconds, cap, self.config.secrets, directory, identity_cwd, stdin)
                row.thread = threading.Thread(target=self._run, args=(row,), name="sprig-" + row.identity, daemon=False)
                self.records[row.identity] = row
                self.revision += 1
                row.thread.start()
            except BaseException:
                if 'row' in locals():
                    self.records.pop(row.identity, None)
                process.close()
                raise
            return {**self._info(row), "approved": True, "started": True, "task_completed": False}

    def _input_ready(self, proposal, reserved=False):
        self.check()
        row = proposal.row
        if (self.closed or self.resetting or not self.enabled() or self.config is not proposal.config
                or self.epoch != proposal.epoch or self.records.get(row.identity) is not row
                or row.input_version != proposal.version + int(reserved)):
            raise ValueError("stdin proposal expired after owner, policy or input changed; request fresh approval")
        if (row.state != "running" or row.done.is_set() or row.cancel.is_set() or not row.stdin_open
                or time.monotonic() >= row.started + row.seconds):
            raise ValueError("stdin is unavailable or the original job deadline expired; no input was replayed")
        if not reserved and proposal.data is not None and row.stdin_bytes + len(proposal.data) > MAX_JOB_INPUT:
            raise ValueError("background stdin exceeds its 64 KiB lifetime input budget")

    def prepare_input(self, action, job_id, text):
        data = None
        if action == "input":
            if not isinstance(text, str) or not text:
                raise ValueError("stdin input must be nonempty UTF-8 text without NUL")
            if len(text) > MAX_INPUT:
                raise ValueError("stdin input exceeds 4 KiB; send separately approved chunks")
            if "\0" in text:
                raise ValueError("stdin input must not contain NUL")
            try:
                data = text.encode("utf-8")
            except UnicodeError:
                raise ValueError("stdin input must be valid UTF-8 text") from None
            if len(data) > MAX_INPUT:
                raise ValueError("stdin input exceeds 4 KiB; send separately approved chunks")
            if any(secret in text for secret in self.config.credential_secrets):
                raise ValueError("configured credentials must not appear in stdin input")
        elif action != "eof" or text is not None:
            raise ValueError("EOF accepts no text")
        with self.lock:
            row = self._find(job_id)
            proposal = InputProposal(row, self.epoch, row.input_version, self.config, data)
            self._input_ready(proposal)
            if row.input_lock.locked():
                raise ValueError("another stdin operation is in progress; no input was queued")
            return proposal

    def deliver_input(self, proposal, recheck):
        row, offset, reserved = proposal.row, 0, False
        if not row.input_lock.acquire(blocking=False):
            raise ValueError("another stdin operation is in progress; request fresh approval")
        def result(complete):
            with self.lock:
                return {"job_id": row.identity, "state": row.state, "approved": True,
                        "stdin": "failed" if row.input_failure else "open" if row.stdin_open else "closed",
                        "input_bytes": len(proposal.data) if proposal.data is not None else 0,
                        "written_bytes": offset, "stdin_bytes": row.stdin_bytes, "input_complete": complete,
                        "task_completed": False, "not_authorization": True}
        try:
            recheck()
            with self.lock:
                self._input_ready(proposal)
                row.input_version += 1
                self.revision += 1
                reserved = True
            until = min(time.monotonic() + INPUT_TIMEOUT, row.started + row.seconds)
            while True:
                recheck()
                with self.lock:
                    self._input_ready(proposal, reserved=True)
                if time.monotonic() >= until:
                    raise TimeoutError("stdin write deadline expired")
                if proposal.data is None:
                    if row.process.close_input(nonblocking=True):
                        with self.lock:
                            row.stdin_open = False
                        return result(True)
                else:
                    count = row.process.write(proposal.data[offset:], nonblocking=True)
                    if type(count) is not int or not 0 <= count <= len(proposal.data) - offset:
                        raise ValueError("invalid process pipe write count")
                    with self.lock:
                        offset += count
                        row.stdin_bytes += count
                    if offset == len(proposal.data):
                        return result(True)
                row.cancel.wait(.02)
        except BaseException as error:
            if not reserved:
                raise
            with self.lock:
                row.input_failure = not isinstance(error, KeyboardInterrupt) or bool(offset)
                row.input_stop = row.input_failure and not isinstance(error, KeyboardInterrupt) and not row.cancel.is_set()
                row.stdin_open = False
                row.cancel.set()
                if row.done.is_set() and row.input_failure and row.state == "exited":
                    row.state, row.error = "failed", INPUT_FAILURE
                self.revision += 1
            if not isinstance(error, (OSError, ValueError)):
                raise
            return {**result(False), "outcome": "uncertain",
                    "error": "stdin delivery failed or expired; job stopping, effects may be partial. Inspect state; input was not replayed."}
        finally:
            row.input_lock.release()

    def _run(self, row):
        state, code = "exited", None
        channels = {"stdout", "stderr"}
        try:
            while True:
                if row.cancel.is_set():
                    state = "failed" if row.input_failure and row.input_stop else "cancelled"
                    break
                if time.monotonic() - row.started >= row.seconds:
                    state = "timed_out"
                    break
                received = False
                for channel in tuple(channels):
                    for _ in range(16):
                        if row.cancel.is_set() or time.monotonic() - row.started >= row.seconds:
                            break
                        data = row.process.read(channel)
                        if data is None:
                            break
                        if not data:
                            channels.remove(channel)
                            break
                        received = True
                        with self.lock:
                            remaining = row.cap - row.bytes
                            row.bytes += len(data)
                            row.output.write(channel, data[:max(remaining, 0)])
                            if len(data) > remaining:
                                state = "output_limited"
                                break
                    if state == "output_limited":
                        break
                if state == "output_limited":
                    break
                code = row.process.poll()
                if code is not None and not received:
                    break  # Clean descendants even when they still hold a pipe.
                row.cancel.wait(.02)
        except BaseException:
            state = "failed"
        finally:
            try:
                row.process.close()
            except BaseException:
                state = "cleanup_failed"
            with self.lock:
                try:
                    row.output.finish()
                except BaseException:
                    state = "failed" if state != "cleanup_failed" else state
                if row.input_failure and state == "exited":
                    state = "failed"
                row.state, row.code = state, code
                row.stdin_open = False
                if state in {"failed", "cleanup_failed"}:
                    row.error = INPUT_FAILURE if row.input_failure and state == "failed" else "background execution or cleanup failed; effects may be incomplete; no command was retried"
                self.revision += 1
                row.done.set()

    def _info(self, row):
        result = {"job_id": row.identity, "state": row.state, "command": redact(row.label, self.config.secrets),
                  "command_preview": True, "command_truncated": row.command_truncated,
                  "timeout": row.seconds, "output_bytes": row.bytes,
                  "retained_bytes": len(row.output.data.decode("utf-8", errors="ignore").encode("utf-8")),
                  "retention_limited": row.output.limited, "output_suppressed": row.output.suppressed,
                  "historical": row.state != "running", "not_authorization": True}
        if row.state != "running":
            result.update(returncode=row.code, timed_out=row.state == "timed_out", cancelled=row.state == "cancelled",
                          output_limited=row.state == "output_limited")
        if row.error:
            result.update(error=row.error, outcome="uncertain")
        if row.stdin_pipe:
            result.update(stdin="failed" if row.input_failure else "open" if row.stdin_open else "closed", stdin_bytes=row.stdin_bytes)
            if row.input_failure:
                result.update(stdin_failed=True, outcome="uncertain")
        return result

    def _find(self, identity):
        if not isinstance(identity, str) or identity not in self.records:
            raise ValueError("background job is unavailable in this conversation; never resume by PID or replay an old command")
        return self.records[identity]

    def unsettled(self):
        with self.lock:
            return any(row.state in {"running", "cleanup_failed"} for row in self.records.values())

    def observations(self):
        with self.lock:
            return [{"job_id": row.identity, "state": row.state, "returncode": row.code} for row in self.records.values()]

    def request_cancel_all(self):
        with self.lock:
            for row in self.records.values():
                if not row.done.is_set():
                    row.cancel.set()

    def reset(self, *, closing=False):
        with self._reset_lock:
            with self.lock:
                self.closed |= closing
                self.resetting = True
                self.epoch += 1
                rows = list(self.records.values())
                for row in rows:
                    row.cancel.set()
            until = time.monotonic() + 7
            for row in rows:
                if row.thread is not None:
                    row.thread.join(max(0, until - time.monotonic()))
            with self.lock:
                if any(row.thread is not None and row.thread.is_alive() for row in rows):
                    raise TimeoutError("background cleanup is pending; keep process ownership and try closing again")
                if any(row.state == "cleanup_failed" for row in rows):
                    raise ValueError("background cleanup failed; state retained for inspection")
                self.records.clear()
                self.revision += 1
                self.resetting = False

    def close(self):
        self.reset(closing=True)

    def control(self, action="list", job_id=None, offset=0, end=None, seconds=0):
        self.check()
        if not self.enabled():
            raise ValueError("background job controls are disabled")
        if (action not in {"list", "status", "output", "wait", "cancel"} or type(offset) is not int or offset < 0
                or end is not None and (type(end) is not int or end < 0)
                or type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 <= seconds <= 30):
            raise ValueError("invalid background action, offset, end or wait duration")
        if action != "output" and end is not None or action not in {"list", "output"} and offset or action != "wait" and seconds:
            raise ValueError("end/offset/wait duration do not apply to this background action")
        if action == "list":
            if job_id is not None:
                raise ValueError("job list accepts only an optional offset")
            with self.lock:
                rows = list(self.records.values())
                if offset > len(rows):
                    raise ValueError("job list offset exceeds retained records")
                result = {"jobs": [], "offset": offset, "next_offset": None, "persistent": False}
                for row in rows[offset:]:
                    candidate = {**result, "jobs": [*result["jobs"], self._info(row)]}
                    candidate["next_offset"] = offset + len(candidate["jobs"]) if offset + len(candidate["jobs"]) < len(rows) else None
                    if self._size(candidate) > self.config.max_output:
                        break
                    result = candidate
                if offset < len(rows) and not result["jobs"]:
                    raise ValueError("background status exceeds max_output")
                return result
        with self.lock:
            row = self._find(job_id)
        if action == "cancel":
            row.cancel.set()
            seconds = 6
        if action in {"wait", "cancel"}:
            until = time.monotonic() + seconds
            while not row.done.is_set() and time.monotonic() < until:
                self.check()
                row.done.wait(min(.05, max(0, until - time.monotonic())))
        self.check()
        with self.lock:
            result = self._info(row)
            if action != "output":
                if self._size(result) > self.config.max_output:
                    raise ValueError("background status exceeds max_output")
                return result
            text = row.output.text(self.config.secrets)
            raw = text.encode("utf-8")
            end = len(raw) if end is None else end
            if row.output.suppressed:
                raise ValueError("background output withheld after credential policy changed")
            if not offset <= end <= len(raw):
                raise ValueError("background output range exceeds the retained prefix")
            try:
                text = raw[offset:end].decode("utf-8")
            except UnicodeError:
                raise ValueError("background output offsets must be UTF-8 character boundaries") from None
            result.update(offset=offset, end=end, content="", next_offset=None, complete=False,
                          sha256=hashlib.sha256(raw[:end]).hexdigest(), untrusted=True)
            low, high = 0, len(text)
            while low < high:
                self.check()
                count = (low + high + 1) // 2
                next_offset = offset + len(text[:count].encode("utf-8"))
                result.update(content=text[:count], next_offset=next_offset if next_offset < end else None, complete=next_offset == end)
                if self._size(result) <= min(self.config.max_output, 16384):
                    low = count
                else:
                    high = count - 1
            next_offset = offset + len(text[:low].encode("utf-8"))
            result.update(content=text[:low], next_offset=next_offset if next_offset < end else None, complete=next_offset == end)
            if text and not low or self._size(result) > min(self.config.max_output, 16384):
                raise ValueError("background output metadata exceeds max_output")
            return result

    @staticmethod
    def _size(value):
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def local_arguments(text):
    command, _, remainder = text.partition(" ")
    if command == "start":
        value = json.loads(remainder)
        if not isinstance(value, dict) or set(value) - {"argv", "cwd", "timeout", "stdin"} or "argv" not in value:
            raise ValueError('Use /job start {"argv":["program","arg"],"cwd":".","timeout":300}')
        return "run", {**value, "background": True}
    if command == "input":
        identity, separator, payload = remainder.partition(" ")
        if not separator or len(payload) > MAX_INPUT * 6 + 2 or not payload.lstrip().startswith('"'):
            raise ValueError('Use /job input ID "exact text\\n"; at most 4 KiB UTF-8')
        value = json.loads(payload)
        if not isinstance(value, str):
            raise ValueError("job input requires a JSON string")
        return "job", {"action": "input", "job_id": identity, "text": value}
    parts = text.split()
    if not parts or parts == ["list"]:
        return "job", {"action": "list"}
    if len(parts) == 2 and parts[0] == "list":
        return "job", {"action": "list", "offset": int(parts[1])}
    if len(parts) == 2 and parts[0] in {"status", "cancel", "eof"}:
        return "job", {"action": parts[0], "job_id": parts[1]}
    if len(parts) in {2, 3} and parts[0] == "wait":
        return "job", {"action": "wait", "job_id": parts[1], "seconds": float(parts[2]) if len(parts) == 3 else 1}
    if 2 <= len(parts) <= 4 and parts[0] == "output":
        return "job", {"action": "output", "job_id": parts[1], **({"offset": int(parts[2])} if len(parts) >= 3 else {}),
                       **({"end": int(parts[3])} if len(parts) == 4 else {})}
    raise ValueError('Use /jobs or /job start|list|status|output|wait|cancel|input|eof; input uses ID "JSON string"')
