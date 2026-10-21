"""Single-request MCP Streamable HTTP; no automatic resend or background streams."""
import base64
import json
import urllib.error
import urllib.request
import uuid

from .mcp_config import MODERN
from .mcp_schema import bounded
from .provider import ProviderError
from .streaming import events
from .transport import HTTPHandler, HTTPSHandler, IOBudget

MAX_REPLY = 1024 * 1024
CLIENT_INFO = {"name": "sprig-agent", "version": "0.1.0"}


class McpError(ValueError):
    def __init__(self, message, *, status=None, rpc_code=None):
        super().__init__(message)
        self.status, self.rpc_code = status, rpc_code


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise McpError("MCP redirects are disabled; configure the final trusted endpoint")


def header_value(value):
    if type(value) is bool:
        value = "true" if value else "false"
    else:
        value = str(value)
    if (value != value.strip() or any(not (32 <= ord(c) < 127 or c == "\t") for c in value)
            or value.startswith("=?base64?") and value.endswith("?=")):
        return "=?base64?" + base64.b64encode(value.encode("utf-8")).decode("ascii") + "?="
    return value


def decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise McpError("duplicate MCP JSON key")
            value[key] = item
        return value
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8").removeprefix("\ufeff")
        result = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        bounded(result, MAX_REPLY, nodes=32768)
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise McpError("invalid or oversized MCP JSON response") from None


class LimitedResponse:
    def __init__(self, response):
        self.response, self.total = response, 0

    def read1(self, size):
        data = self.response.read1(min(size, MAX_REPLY - self.total + 1))
        self.total += len(data)
        if self.total > MAX_REPLY:
            raise McpError("MCP response exceeds 1 MiB")
        return data


class HttpClient:
    def __init__(self, server, check):
        self.server, self.check = server, check
        self.session = None
        self.ready = False
        self.capabilities = {}
        self.catalog_changed = False
        self.catalog_version = 0
        handlers = [NoRedirect(), HTTPHandler(), HTTPSHandler()]
        if server.proxy == "direct":
            handlers.append(urllib.request.ProxyHandler({}))
        elif server.proxy != "system":
            handlers.append(urllib.request.ProxyHandler({"http": server.proxy, "https": server.proxy}))
        self.opener = urllib.request.build_opener(*handlers)

    def close(self):
        self.ready = False
        self.opener.close()

    @property
    def modern(self):
        return self.server.protocol_version == MODERN

    def _message(self, method, params, identity):
        params = dict(params)
        if self.modern:
            params["_meta"] = {"io.modelcontextprotocol/protocolVersion": MODERN,
                               "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
                               "io.modelcontextprotocol/clientCapabilities": {}}
        value = {"jsonrpc": "2.0", "method": method, "params": params}
        if identity is not None:
            value["id"] = identity
        return value

    def _headers(self, method, params, mirrored):
        headers = {**dict(self.server.headers), "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": self.server.protocol_version, "User-Agent": "sprig-agent/0.1.0"}
        if self.modern:
            headers["Mcp-Method"] = method
            if method in ("tools/call", "resources/read", "prompts/get"):
                headers["Mcp-Name"] = header_value(params.get("name", params.get("uri")))
            headers.update(mirrored)
        elif self.session:
            headers["MCP-Session-Id"] = self.session
        if sum(len(k) + len(v) for k, v in headers.items()) > 65536:
            raise McpError("MCP request headers exceed bounds")
        return headers

    def _response(self, value, identity):
        if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
            raise McpError("invalid MCP JSON-RPC envelope")
        if "method" in value:
            if "id" in value or not isinstance(value["method"], str) or not value["method"].startswith("notifications/"):
                raise McpError("server-initiated MCP requests are not enabled")
            if value["method"] == "notifications/tools/list_changed":
                self.catalog_changed = True
                self.catalog_version += 1
            return None
        if value.get("id") != identity or ("result" in value) == ("error" in value):
            raise McpError("MCP response ID or result/error envelope mismatch")
        if "error" in value:
            error = value["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(error.get("message"), str):
                raise McpError("invalid MCP error response")
            raise McpError("MCP RPC failed; inspect server configuration before a fresh proposal", rpc_code=error["code"])
        result = value["result"]
        if not isinstance(result, dict) or result.get("resultType", "complete") not in ("complete", "input_required"):
            raise McpError("unsupported MCP result shape or resultType")
        return result

    def _exchange(self, method, params, identity, mirrored=(), *, cleanup=False, delete=False):
        self.check() if not cleanup else None
        payload = None if delete else bounded(self._message(method, params, identity), 65536)
        headers = self._headers(method, params, dict(mirrored))
        request = urllib.request.Request(self.server.url, payload, headers, method="DELETE" if delete else "POST")
        idle, total = (1, 1) if cleanup else (self.server.timeout, self.server.deadline)
        budget = request._sprig_io_budget = IOBudget(idle, total, (lambda: None) if cleanup else self.check)
        try:
            with self.opener.open(request, timeout=idle) as response:
                budget.check()
                if delete:
                    return {}
                if identity is None:
                    if response.status != 202 or response.read(1):
                        raise McpError("MCP notification requires empty HTTP 202")
                    return {}
                if response.status != 200:
                    raise McpError("unexpected MCP HTTP status", status=response.status)
                if method == "initialize":
                    sessions = response.headers.get_all("MCP-Session-Id", [])
                    if len(sessions) > 1 or sessions and (not 16 <= len(sessions[0]) <= 1024 or any(not 33 <= ord(c) <= 126 for c in sessions[0])):
                        raise McpError("invalid MCP session header")
                    self.session = sessions[0] if sessions else None
                content_type = response.headers.get_content_type()
                if content_type == "application/json":
                    raw = response.read(MAX_REPLY + 1)
                    budget.check()
                    if len(raw) > MAX_REPLY:
                        raise McpError("MCP response exceeds 1 MiB")
                    result = self._response(decode(raw), identity)
                    if result is None:
                        raise McpError("MCP JSON response did not complete the request")
                    return result
                if content_type != "text/event-stream":
                    raise McpError("MCP requires JSON or SSE response content type")
                for count, data in enumerate(events(LimitedResponse(response), completion_marker="matching MCP response")):
                    budget.check()
                    if count >= 256:
                        raise McpError("too many MCP stream notifications")
                    if not data.strip():
                        continue
                    result = self._response(decode(data), identity)
                    if result is not None:
                        return result
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if delete and status == 405:
                return {"termination_unsupported": True}
            raise McpError("MCP HTTP request failed; no automatic retry", status=status) from None
        except (OSError, urllib.error.URLError, ProviderError) as exc:
            raise McpError("MCP network, timeout or incomplete response; delivery may be unknown; no automatic retry") from None
        raise McpError("MCP response ended without a matching result")

    def rpc(self, method, params, mirrored=()):
        identity = uuid.uuid4().hex
        try:
            return self._exchange(method, params, identity, mirrored)
        except KeyboardInterrupt:
            if not self.modern and method != "initialize":
                try:
                    self._exchange("notifications/cancelled", {"requestId": identity, "reason": "client cancelled"}, None, cleanup=True)
                except (ValueError, OSError):
                    pass  # Best effort; never resend the original operation.
            raise

    def connect(self):
        if self.server.missing_env or any(name not in dict(self.server.headers) for name, _ in self.server.headers_env):
            raise McpError("MCP credential environment variable is missing")
        if self.modern:
            result = self.rpc("server/discover", {})
            versions = result.get("supportedVersions")
            if not isinstance(versions, list) or self.server.protocol_version not in versions:
                raise McpError("MCP server does not advertise the configured protocol version")
        else:
            result = self.rpc("initialize", {"protocolVersion": self.server.protocol_version, "clientInfo": CLIENT_INFO, "capabilities": {}})
            if result.get("protocolVersion") != self.server.protocol_version:
                raise McpError("MCP server chose another version; configure it explicitly before reconnecting")
        if not isinstance(result.get("capabilities"), dict) or result.get("resultType", "complete") != "complete":
            raise McpError("invalid MCP discovery or initialization capabilities")
        meta = result.get("_meta")
        if meta is not None and not isinstance(meta, dict):
            raise McpError("invalid MCP metadata")
        info = result.get("serverInfo", (meta or {}).get("io.modelcontextprotocol/serverInfo"))
        if ((not self.modern or info is not None) and (not isinstance(info, dict) or not isinstance(info.get("name"), str) or not isinstance(info.get("version"), str))
                or not isinstance(result.get("instructions", ""), str)):
            raise McpError("invalid MCP server information")
        if not self.modern:
            self._exchange("notifications/initialized", {}, None)
        self.capabilities, self.ready = result["capabilities"], True
        return {"capabilities": self.capabilities, "server_info": info,
                "instructions": result.get("instructions", ""), "not_authorization": True}

    def terminate(self):
        try:
            return self._exchange("disconnect", {}, None, delete=True) if self.session else {}
        finally:
            self.close()
