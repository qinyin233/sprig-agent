"""Conversation-owned background commands; no restart, detached PID or saved authority."""
from collections import OrderedDict
import codecs
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
JOB_SCHEMA = {"type": "function", "function": {
    "name": "job", "description": "Observe owned background commands: list/status/output/wait (<=30s), or freshly approve cancel. Start via run(background=true). Memory-only; no restart/replay. Output offsets are UTF-8 bytes; keep end for a stable prefix. Exit is not fixed acceptance.",
    "parameters": {"type": "object", "properties": {
        "action": {"enum": ["list", "status", "output", "wait", "cancel"]},
        "job_id": {"type": "string", "maxLength": 40},
        "offset": {"type": "integer", "minimum": 0, "maximum": MAX_RETAINED},
        "end": {"type": "integer", "minimum": 0, "maximum": MAX_RETAINED},
        "seconds": {"type": "number", "minimum": 0, "maximum": 30}},
        "required": ["action"], "additionalProperties": False}}}


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
    def __init__(self, identity, process, label, truncated, seconds, cap, secrets):
        self.identity, self.process, self.label = identity, process, label
        self.seconds, self.cap = seconds, cap
        self.command_truncated = truncated
        self.started = time.monotonic()
        self.cancel, self.done = threading.Event(), threading.Event()
        self.state, self.code, self.bytes = "running", None, 0
        self.output = Output(secrets)
        self.thread = None
        self.error = None


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

    def start(self, argv, directory, env, seconds, cap, *, epoch):
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
            process = Process(argv, directory, env)
            try:
                process.close_input()
                self.check()
                label = terminal_text(redact(json.dumps([os.path.basename(argv[0]), *argv[1:]], ensure_ascii=False), self.config.secrets)).encode("utf-8")
                row = Job("job-" + uuid.uuid4().hex, process, label[:160].decode("utf-8", errors="ignore"), len(label) > 160, seconds, cap, self.config.secrets)
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

    def _run(self, row):
        state, code = "exited", None
        channels = {"stdout", "stderr"}
        try:
            while True:
                if row.cancel.is_set():
                    state = "cancelled"
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
                row.state, row.code = state, code
                if state in {"failed", "cleanup_failed"}:
                    row.error = "background execution or cleanup failed; effects may be incomplete; no command was retried"
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
        if not isinstance(value, dict) or set(value) - {"argv", "cwd", "timeout"} or "argv" not in value:
            raise ValueError('Use /job start {"argv":["program","arg"],"cwd":".","timeout":300}')
        return "run", {**value, "background": True}
    parts = text.split()
    if not parts or parts == ["list"]:
        return "job", {"action": "list"}
    if len(parts) == 2 and parts[0] == "list":
        return "job", {"action": "list", "offset": int(parts[1])}
    if len(parts) == 2 and parts[0] in {"status", "cancel"}:
        return "job", {"action": parts[0], "job_id": parts[1]}
    if len(parts) in {2, 3} and parts[0] == "wait":
        return "job", {"action": "wait", "job_id": parts[1], "seconds": float(parts[2]) if len(parts) == 3 else 1}
    if 2 <= len(parts) <= 4 and parts[0] == "output":
        return "job", {"action": "output", "job_id": parts[1], **({"offset": int(parts[2])} if len(parts) >= 3 else {}),
                       **({"end": int(parts[3])} if len(parts) == 4 else {})}
    raise ValueError("Use /jobs or /job start|list|status|output|wait|cancel; output uses ID [OFFSET [END]]")
