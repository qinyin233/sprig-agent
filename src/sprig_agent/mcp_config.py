"""Explicit trusted MCP endpoints, separate from model routing and credentials."""
from dataclasses import dataclass, field, replace
import math
import re
from urllib.parse import urlsplit
import base64

from .config import ENV_NAME, HEADER_NAME, FORBIDDEN_HEADERS, endpoint

VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18")
MODERN = VERSIONS[0]


def credential_parts(value):
    result = [value]
    scheme, _, token = value.partition(" ")
    if scheme.lower() in ("bearer", "basic") and token.strip():
        result.append(token.strip())
        if scheme.lower() == "basic":
            try:
                decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
                result.append(decoded)
                if ":" in decoded:
                    result.append(decoded.partition(":")[2])
            except (ValueError, UnicodeError):
                pass
    return tuple(part for part in result if part)


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str
    protocol_version: str = MODERN
    enabled: bool = True
    timeout: float = 30
    deadline: float = 120
    proxy: str = "system"
    headers: tuple = field(default=(), repr=False)
    headers_env: tuple = ()
    missing_env: tuple = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError("invalid MCP server name")
        endpoint(self.url)  # Validate, preserving the exact endpoint path/trailing slash.
        if self.protocol_version not in VERSIONS or type(self.enabled) is not bool:
            raise ValueError("invalid MCP protocol_version or enabled flag")
        for value, cap in ((self.timeout, 120), (self.deadline, 3600)):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= cap:
                raise ValueError("MCP timeout/deadline is outside its bounds")
        if not isinstance(self.proxy, str):
            raise ValueError("invalid MCP proxy")
        if self.proxy not in ("system", "direct"):
            parsed = urlsplit(self.proxy)
            parsed.port
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or parsed.path not in ("", "/") or any(ord(c) <= 32 for c in self.proxy)):
                raise ValueError("MCP proxy must be system, direct or a credential-free HTTP(S) proxy origin")
        if (not isinstance(self.headers, tuple) or not isinstance(self.headers_env, tuple)
                or len(self.headers) + len(self.headers_env) > 16):
            raise ValueError("MCP headers must be bounded name/value pairs")
        seen = set()
        for env, pairs in ((False, self.headers), (True, self.headers_env)):
            for pair in pairs:
                if not isinstance(pair, tuple) or len(pair) != 2:
                    raise ValueError("invalid MCP header pair")
                name, value = pair
                if (not isinstance(name, str) or not HEADER_NAME.fullmatch(name) or name.lower() in seen
                        or name.lower() in FORBIDDEN_HEADERS - {"authorization"} or name.lower().startswith("mcp-")
                        or not isinstance(value, str) or not value or len(value) > 4096
                        or any(ord(c) < 32 or ord(c) > 255 for c in value)
                        or env and not ENV_NAME.fullmatch(value)):
                    raise ValueError("invalid, duplicated or reserved MCP header")
                seen.add(name.lower())
        if sum(len(name) + len(value) for name, value in (*self.headers, *self.headers_env)) > 16384:
            raise ValueError("MCP configured header bytes exceed 16 KiB")

    @classmethod
    def parse(cls, name, value):
        if (not isinstance(value, dict) or set(value) - {"transport", "url", "protocol_version", "enabled", "timeout", "deadline", "proxy", "headers", "headers_env"}
                or value.get("transport", "streamable_http") != "streamable_http"
                or not isinstance(value.get("headers", {}), dict) or not isinstance(value.get("headers_env", {}), dict)):
            raise ValueError("MCP supports explicit streamable_http server tables")
        return cls(name, value.get("url", ""), **{key: item for key, item in value.items() if key not in {"transport", "url", "headers", "headers_env"}},
                   headers=tuple(value.get("headers", {}).items()), headers_env=tuple(value.get("headers_env", {}).items()))

    def resolve(self, env):
        values = dict(self.headers)
        missing = []
        for name, variable in self.headers_env:
            if env.get(variable):
                values[name] = env[variable]
            else:
                missing.append(variable)
        # Keep reference names separately; resolved values must not count as duplicates.
        result = replace(self, headers=tuple(values.items()), headers_env=())
        object.__setattr__(result, "headers_env", self.headers_env)
        object.__setattr__(result, "missing_env", tuple(missing))
        return result

    def public(self):
        return {"name": self.name, "transport": "streamable_http", "url": self.url, "protocol_version": self.protocol_version,
                "enabled": self.enabled, "timeout": self.timeout, "deadline": self.deadline, "proxy": self.proxy,
                "header_names": sorted({k.lower() for k, _ in (*self.headers, *self.headers_env)}),
                "credential_environment": sorted(v for _, v in self.headers_env), "missing_environment": list(self.missing_env)}

    def credential_values(self):
        return tuple(part for _, value in self.headers for part in credential_parts(value))


def parse_servers(value):
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("MCP accepts at most eight named servers")
    return tuple(McpServer.parse(name, fields) for name, fields in value.items())
