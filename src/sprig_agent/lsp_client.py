"""Bounded Content-Length JSON-RPC; no server-driven writes, commands or retries."""
from collections import OrderedDict
import json
import os
import re
import time

from .mcp_process import Process
from .mcp_schema import bounded

MAX_FRAME = 1024 * 1024


class LspError(ValueError):
    pass


def encode(value):
    try:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(body) > MAX_FRAME:
            raise ValueError()
        return b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n\r\n" + body
    except (ValueError, UnicodeError, RecursionError):
        raise LspError("LSP request exceeds the 1 MiB UTF-8 JSON frame limit") from None


def decode(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        bounded(value, MAX_FRAME, depth=32, nodes=65536)
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise LspError("invalid, duplicate, deeply nested or oversized LSP JSON message") from None


class Budget:
    def __init__(self, server, check):
        self.check_callback = check
        self.deadline = time.monotonic() + server.deadline
        self.last = time.monotonic()
        self.idle = server.timeout

    def check(self):
        self.check_callback()
        now = time.monotonic()
        if now >= self.deadline or now - self.last >= self.idle:
            raise LspError("LSP request deadline or idle limit exceeded; no automatic retry")

    def activity(self):
        self.check()
        self.last = time.monotonic()


class Client:
    def __init__(self, server, directory, environment, root_uri, check):
        self.server, self.check, self.root_uri = server, check, root_uri
        self.buffer = bytearray()
        self.expected = None
        self.stdout_bytes = self.stderr_bytes = self.notifications = 0
        self.next_id = 0
        self.capabilities = {}
        self.documents, self.diagnostics = OrderedDict(), {}
        self.discarded_diagnostics = self.denied_requests = 0
        self.closed = False
        self.check()
        self.process = Process(server.command, directory, environment)

    def close(self):
        if not self.closed:
            self.process.close()
            self.closed = True
            self.documents.clear()
            self.diagnostics.clear()
            self.buffer.clear()

    def status(self):
        return {"running": not self.closed and self.process.poll() is None,
                "documents": len(self.documents), "stdout_bytes": self.stdout_bytes, "stderr_bytes": self.stderr_bytes,
                "denied_requests": self.denied_requests, "discarded_diagnostics": self.discarded_diagnostics}

    def _frame(self):
        if self.expected is None:
            marker = self.buffer.find(b"\r\n\r\n")
            if marker < 0:
                if len(self.buffer) > 8192:
                    raise LspError("LSP header exceeds 8 KiB")
                return None
            if marker > 8192:
                raise LspError("LSP header exceeds 8 KiB")
            try:
                headers = {}
                for row in self.buffer[:marker].decode("ascii").split("\r\n"):
                    key, separator, value = row.partition(":")
                    key, value = key.lower(), value.strip()
                    if not separator or not re.fullmatch(r"[a-z-]+", key) or key in headers:
                        raise ValueError()
                    headers[key] = value
                length = headers.get("content-length", "")
                if not re.fullmatch(r"[0-9]{1,7}", length) or not 0 < int(length) <= MAX_FRAME:
                    raise ValueError()
                if "content-type" in headers:
                    media = headers["content-type"].lower().replace(" ", "")
                    if media not in {"application/vscode-jsonrpc", "application/vscode-jsonrpc;charset=utf-8", "application/vscode-jsonrpc;charset=utf8"}:
                        raise ValueError()
                self.expected = int(length)
            except (ValueError, UnicodeError):
                raise LspError("invalid LSP Content-Length or UTF-8 content header") from None
            del self.buffer[:marker + 4]
        if len(self.buffer) < self.expected:
            return None
        raw = bytes(self.buffer[:self.expected])
        del self.buffer[:self.expected]
        self.expected = None
        return decode(raw)

    def _incoming(self, value, budget):
        if "method" not in value:
            return value
        if "result" in value or "error" in value:
            raise LspError("invalid mixed LSP request/response envelope")
        self.notifications += 1
        if self.notifications > 256 or not isinstance(value["method"], str):
            raise LspError("LSP notification/request quota exceeded or invalid method")
        if "id" in value:
            identity = value["id"]
            if type(identity) not in (str, int) or isinstance(identity, str) and len(identity) > 256:
                raise LspError("invalid LSP server request ID")
            method, params = value["method"], value.get("params", {})
            reply = {"jsonrpc": "2.0", "id": identity}
            if method == "workspace/configuration" and isinstance(params, dict) and isinstance(params.get("items"), list) and len(params["items"]) <= 32:
                reply["result"] = [None for _ in params["items"]]
            elif method == "window/workDoneProgress/create":
                reply["result"] = None
            else:
                self.denied_requests += 1
                reply["error"] = {"code": -32601, "message": "Sprig does not allow server-driven commands, edits or dynamic client capabilities"}
            self._send(reply, budget)
            return None
        if value["method"] == "textDocument/publishDiagnostics":
            params = value.get("params")
            if not isinstance(params, dict) or not isinstance(params.get("uri"), str) or not isinstance(params.get("diagnostics"), list):
                raise LspError("invalid LSP diagnostics notification")
            uri = params["uri"]
            doc = self.documents.get(uri)
            version = params.get("version")
            if doc is None or version is not None and (type(version) is not int or version != doc["version"]):
                self.discarded_diagnostics += 1
                return None
            if len(params["diagnostics"]) > 256:
                raise LspError("LSP diagnostics exceed 256 entries")
            bounded(params["diagnostics"], 131072, depth=24, nodes=16384)
            self.diagnostics[uri] = {"version": version, "items": params["diagnostics"]}
        return None

    def _pump(self, budget):
        budget.check()
        for _ in range(16):
            data = self.process.read("stderr")
            if not data:
                break
            self.stderr_bytes += len(data)
            if self.stderr_bytes > 8 * 1024 * 1024:
                raise LspError("LSP stderr exceeds 8 MiB; raw logs are discarded")
            budget.activity()
        for _ in range(16):
            data = self.process.read("stdout")
            if data is None:
                break
            if not data:
                raise LspError("LSP process closed stdout before the operation completed")
            self.stdout_bytes += len(data)
            if self.stdout_bytes > 32 * 1024 * 1024:
                raise LspError("LSP connection stdout exceeds 32 MiB")
            self.buffer.extend(data)
            if len(self.buffer) > MAX_FRAME + 8192:
                raise LspError("LSP pending frame exceeds bounds")
            budget.activity()

    def _send(self, message, budget):
        raw = encode(message)
        offset = 0
        while offset < len(raw):
            budget.check()
            count = self.process.write(raw[offset:offset + 16384])
            if count:
                offset += count
                budget.activity()
            else:
                # Drain server output while our input pipe is backpressured.
                self._pump(budget)
                time.sleep(.005)

    def notify(self, method, params, budget):
        self._send({"jsonrpc": "2.0", "method": method, "params": params}, budget)

    def exchange(self, method, params, budget):
        self.next_id += 1
        identity = self.next_id
        self.notifications = 0
        self._send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}, budget)
        try:
            while True:
                budget.check()
                for _ in range(64):
                    value = self._frame()
                    if value is None:
                        break
                    response = self._incoming(value, budget)
                    if response is None:
                        continue
                    if (response.get("id") != identity or type(response.get("id")) is not int
                            or ("result" in response) == ("error" in response)):
                        raise LspError("unsolicited or mismatched LSP response")
                    if "error" in response:
                        error = response["error"]
                        if not isinstance(error, dict) or type(error.get("code")) is not int:
                            raise LspError("invalid LSP error envelope")
                        raise LspError("language server rejected the request (code " + str(error["code"]) + "); no automatic retry")
                    return response["result"]
                self._pump(budget)
                time.sleep(.005)
        except KeyboardInterrupt:
            try:
                class Cleanup:
                    timeout = .05
                    deadline = .05
                self.notify("$/cancelRequest", {"id": identity}, Budget(Cleanup(), lambda: None))
            except BaseException:
                pass
            raise

    def initialize(self, budget, *, require_sync=True):
        result = self.exchange("initialize", {"processId": os.getpid(), "rootUri": self.root_uri,
            "workspaceFolders": [{"uri": self.root_uri, "name": "workspace"}],
            "clientInfo": {"name": "sprig-agent", "version": "0.1.0"},
            "capabilities": {"general": {"positionEncodings": ["utf-16"]}, "offsetEncoding": ["utf-16"],
                "workspace": {"applyEdit": False, "configuration": False, "didChangeWatchedFiles": {"dynamicRegistration": False},
                    "symbol": {"dynamicRegistration": False, "symbolKind": {"valueSet": list(range(1, 27))}}},
                "window": {"workDoneProgress": False},
                "textDocument": {"synchronization": {"dynamicRegistration": False, "didSave": False},
                    "definition": {"dynamicRegistration": False, "linkSupport": True},
                    "references": {"dynamicRegistration": False}, "implementation": {"dynamicRegistration": False, "linkSupport": True},
                    "hover": {"dynamicRegistration": False, "contentFormat": ["plaintext"]},
                    "documentSymbol": {"dynamicRegistration": False, "hierarchicalDocumentSymbolSupport": True},
                    "diagnostic": {"dynamicRegistration": False, "relatedDocumentSupport": False},
                    "publishDiagnostics": {"versionSupport": True, "relatedInformation": False}}}}, budget)
        if not isinstance(result, dict) or not isinstance(result.get("capabilities"), dict):
            raise LspError("LSP initialize requires server capabilities")
        bounded(result["capabilities"], 65536, depth=24, nodes=8192)
        self.capabilities = result["capabilities"]
        if self.capabilities.get("positionEncoding", "utf-16") != "utf-16" or result.get("offsetEncoding", "utf-16") != "utf-16":
            raise LspError("LSP server did not negotiate UTF-16 positions")
        sync = self.capabilities.get("textDocumentSync")
        self.sync_kind = sync if type(sync) is int else sync.get("change", 0) if isinstance(sync, dict) else 0
        if self.sync_kind not in (1, 2) or isinstance(sync, dict) and sync.get("openClose") is not True:
            self.sync_kind = 0
            if require_sync:
                raise LspError("LSP requires document open/close and full or incremental synchronization")
        self.notify("initialized", {}, budget)

    def sync(self, document, language, budget):
        if self.sync_kind not in (1, 2):
            raise LspError("LSP requires document open/close and full or incremental synchronization")
        uri = document["uri"]
        previous = self.documents.get(uri)
        if previous is None and len(self.documents) >= 4:
            old_uri, _ = self.documents.popitem(last=False)
            self.notify("textDocument/didClose", {"textDocument": {"uri": old_uri}}, budget)
            self.diagnostics.pop(old_uri, None)
        version = previous["version"] if previous else 0
        if previous is None or previous["sha256"] != document["sha256"]:
            version += 1
            self.diagnostics.pop(uri, None)
            self.documents[uri] = {**document, "version": version}
            if previous is None:
                self.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "version": version, "languageId": language, "text": document["text"]}}, budget)
            else:
                change = {"text": document["text"]}
                if self.sync_kind == 2:
                    lines = re.split(r"\r\n|\r|\n", previous["text"])
                    change["range"] = {"start": {"line": 0, "character": 0}, "end": {"line": len(lines) - 1, "character": len(lines[-1].encode("utf-16-le")) // 2}}
                self.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": version}, "contentChanges": [change]}, budget)
        self.documents.move_to_end(uri)
        return version

    def pushed_diagnostics(self, uri, budget):
        until = min(budget.deadline, time.monotonic() + min(5, self.server.timeout))
        while True:
            budget.check()
            for _ in range(64):
                value = self._frame()
                if value is None:
                    break
                if self._incoming(value, budget) is not None:
                    raise LspError("unsolicited LSP response while waiting for diagnostics")
            if uri in self.diagnostics:
                return self.diagnostics[uri]
            if time.monotonic() >= until:
                return None
            self._pump(budget)
            time.sleep(.005)
