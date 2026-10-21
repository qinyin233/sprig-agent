"""Bounded newline JSON-RPC over owned nonblocking pipes; no reconnect/replay."""
import time
import uuid

from .mcp_http import HttpClient, McpError, decode, MAX_REPLY
from .mcp_process import Process
from .mcp_schema import bounded

EMPTY = object()
MAX_STDOUT = 64 * 1024 * 1024
MAX_STDERR = 8 * 1024 * 1024


class StdioClient(HttpClient):
    def __init__(self, server, check, launch):
        self.server, self.check = server, check
        self.launch = launch
        self.session, self.process = None, None
        self.ready, self.capabilities = False, {}
        self.catalog_changed, self.catalog_version = False, 0
        self.buffer = bytearray()
        self.stdout_bytes = self.stderr_bytes = self.notifications = 0
        self.pending, self.result = None, EMPTY
        self.closed = False
        self.expected_catalog = None
        self.request_sent = False

    def status(self):
        return {"process_running": bool(self.process and self.process.poll() is None),
                "stdout_bytes": self.stdout_bytes, "stderr_discarded_bytes": self.stderr_bytes}

    def close(self):
        self.closed, self.ready = True, False
        if self.process:
            self.process.close()
        self.buffer.clear()
        self.result = EMPTY

    def poll_events(self):
        if self.closed or self.process is None:
            return 0
        try:
            count = 0
            while True:
                self.check()
                received = False
                for channel in ("stdout", "stderr"):
                    chunk = self.process.read(channel)
                    if chunk is None:
                        continue
                    if not chunk:
                        if channel == "stdout":
                            raise McpError("MCP stdout closed; delivery may be unknown; explicitly reconnect")
                        continue
                    received = True
                    count += len(chunk)
                    if channel == "stderr":
                        self.stderr_bytes += len(chunk)
                        if self.stderr_bytes > MAX_STDERR:
                            raise McpError("MCP stderr exceeded the 8 MiB connection allowance")
                        continue  # Never return raw stderr, including credential-bearing logs.
                    self.stdout_bytes += len(chunk)
                    if self.stdout_bytes > MAX_STDOUT:
                        raise McpError("MCP stdout exceeded the 64 MiB connection allowance")
                    self.buffer.extend(chunk)
                    while b"\n" in self.buffer:
                        line, _, tail = self.buffer.partition(b"\n")
                        self.buffer = bytearray(tail)
                        if len(line) > MAX_REPLY:
                            raise McpError("MCP stdio frame exceeds 1 MiB")
                        value = decode(line)
                        if isinstance(value, dict) and "method" in value:
                            self.notifications += 1
                            if self.notifications > 256:
                                raise McpError("too many MCP notifications without a fresh RPC")
                            self._response(value, self.pending)
                        else:
                            if self.pending is None or self.result is not EMPTY:
                                raise McpError("unsolicited or duplicate MCP response")
                            self.result = self._response(value, self.pending)
                    if len(self.buffer) > MAX_REPLY:
                        raise McpError("MCP stdio frame exceeds 1 MiB without newline")
                if not received:
                    break
                if count >= 65536:
                    break  # Yield to deadline/cancellation checks, without growing a queue.
            return count
        except Exception:
            self.close()
            raise

    def _exchange(self, method, params, identity, mirrored=(), *, cleanup=False, delete=False):
        self.check() if not cleanup else None
        deadline = time.monotonic() + (0.1 if cleanup else self.server.deadline)
        drain_until = min(deadline, time.monotonic() + self.server.timeout)
        while self.poll_events() or self.buffer:
            if time.monotonic() >= drain_until:
                raise McpError("MCP pending stdout could not settle before a new request")
            time.sleep(.001)
        if self.expected_catalog is not None and self.expected_catalog != self.catalog_version:
            raise McpError("MCP catalog changed before sending; discover tools and approve a fresh proposal")
        raw = bounded(self._message(method, params, identity), 65536) + b"\n"
        idle = time.monotonic() + (0.1 if cleanup else self.server.timeout)
        self.pending, self.result, self.notifications = identity, EMPTY, 0
        offset = 0
        self.request_sent = False
        try:
            while True:
                if not cleanup:
                    self.check()
                now = time.monotonic()
                if now >= min(deadline, idle):
                    raise McpError("MCP stdio request timed out; delivery may be unknown; no automatic retry")
                if offset < len(raw):
                    sent = self.process.write(raw[offset:])
                    offset += sent
                    self.request_sent = offset == len(raw)
                before = self.stdout_bytes
                self.poll_events()
                if self.stdout_bytes != before:
                    idle = time.monotonic() + self.server.timeout
                if offset == len(raw) and (identity is None or self.result is not EMPTY):
                    return {} if identity is None else self.result
                time.sleep(.01)
        finally:
            self.pending = None

    def rpc(self, method, params, mirrored=()):
        identity = uuid.uuid4().hex
        self.request_sent = False
        try:
            return self._exchange(method, params, identity, mirrored)
        except KeyboardInterrupt:
            # A best-effort cancellation is not a retry of the original request.
            raw = bounded(self._message("notifications/cancelled", {"requestId": identity}, None), 65536) + b"\n"
            try:
                until, offset = time.monotonic() + .1, 0
                while self.request_sent and offset < len(raw) and time.monotonic() < until:
                    offset += self.process.write(raw[offset:])
                    time.sleep(.005)
            except (OSError, ValueError):
                pass
            finally:
                self.close()
            raise
        except BaseException:
            self.close()
            raise
        finally:
            self.expected_catalog = None
            self.result = EMPTY

    def connect(self):
        self.check()
        if self.server.missing_env or any(name not in dict(self.server.env) for name, _ in self.server.env_from):
            raise McpError("MCP process environment reference is missing")
        directory, env, recheck = self.launch
        recheck()
        self.process = Process(list(self.server.command), directory, env)
        try:
            return super().connect()
        except BaseException:
            self.close()
            raise

    def terminate(self):
        self.close()
        return {"process_closed": True}
