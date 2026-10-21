"""Explicit trusted MCP endpoints, separate from model routing and credentials."""
from dataclasses import dataclass, field, replace
import math
import re
from urllib.parse import urlsplit
import base64
import hashlib
import json
from pathlib import PurePosixPath

from .config import ENV_NAME, HEADER_NAME, FORBIDDEN_HEADERS, endpoint

VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18")
MODERN = VERSIONS[0]
SECRET_ENV = re.compile(r"(?:^|_)(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL|PAT)(?:_|$)", re.I)


def environment_credentials(values, references=()):
    declared = {name.upper() for name, _ in references}
    return tuple(value for name, value in values if name.upper() in declared or SECRET_ENV.search(name))


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
    url: str = ""
    protocol_version: str = MODERN
    enabled: bool = True
    timeout: float = 30
    deadline: float = 120
    proxy: str = "system"
    headers: tuple = field(default=(), repr=False)
    headers_env: tuple = ()
    missing_env: tuple = ()
    transport: str = "streamable_http"
    command: tuple = ()
    cwd: str = "."
    env: tuple = field(default=(), repr=False)
    env_from: tuple = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise ValueError("invalid MCP server name")
        if self.transport not in ("streamable_http", "stdio"):
            raise ValueError("MCP transport must be streamable_http or stdio")
        if self.transport == "streamable_http":
            endpoint(self.url)  # Preserve the exact endpoint path/trailing slash.
            if self.command or self.env or self.env_from or self.cwd != ".":
                raise ValueError("HTTP MCP servers cannot configure process fields")
        else:
            if self.url or self.headers or self.headers_env or self.proxy != "system":
                raise ValueError("stdio MCP servers cannot configure HTTP fields")
            if (not isinstance(self.command, tuple) or not 1 <= len(self.command) <= 256
                    or not self.command[0] or any(not isinstance(arg, str) or "\0" in arg for arg in self.command)
                    or sum(len(arg) for arg in self.command) > 65536):
                raise ValueError("MCP stdio command must be a bounded argv array without NUL")
            if (not isinstance(self.cwd, str) or not self.cwd or len(self.cwd) > 1024 or "\0" in self.cwd
                    or "\\" in self.cwd or ":" in self.cwd or PurePosixPath(self.cwd).is_absolute()
                    or ".." in PurePosixPath(self.cwd).parts):
                raise ValueError("MCP stdio cwd must be a workspace-relative POSIX path")
            seen_env = set()
            for pairs, reference in ((self.env, False), (self.env_from, True)):
                if not isinstance(pairs, tuple) or len(pairs) > 32:
                    raise ValueError("MCP process environment accepts at most 32 pairs per table")
                for pair in pairs:
                    if not isinstance(pair, tuple) or len(pair) != 2:
                        raise ValueError("invalid MCP process environment pair")
                    key, value = pair
                    if (not isinstance(key, str) or not ENV_NAME.fullmatch(key) or key.upper() in seen_env
                            or not isinstance(value, str) or "\0" in value or len(value) > 4096
                            or reference and not ENV_NAME.fullmatch(value)):
                        raise ValueError("invalid or duplicated MCP process environment name/value")
                    seen_env.add(key.upper())
            if sum(len(k) + len(v) for k, v in (*self.env, *self.env_from)) > 16384:
                raise ValueError("MCP process environment exceeds 16 KiB")
            if len(seen_env) > 32:
                raise ValueError("MCP process environment accepts at most 32 distinct names total")
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
        mappings = ("headers", "headers_env", "env", "env_from")
        common = {"transport", "protocol_version", "enabled", "timeout", "deadline"}
        if isinstance(value, dict):
            allowed = common | ({"command", "cwd", "env", "env_from"} if value.get("transport") == "stdio" else {"url", "proxy", "headers", "headers_env"})
            if set(value) - allowed:
                raise ValueError("MCP fields must match the selected HTTP or stdio transport")
        if (not isinstance(value, dict) or set(value) - {"transport", "url", "protocol_version", "enabled", "timeout", "deadline", "proxy", "headers", "headers_env", "command", "cwd", "env", "env_from"}
                or any(not isinstance(value.get(key, {}), dict) for key in mappings)
                or not isinstance(value.get("command", []), list)):
            raise ValueError("MCP requires explicit validated HTTP or stdio server tables")
        return cls(name, value.get("url", ""), **{key: item for key, item in value.items() if key not in {"url", "command", *mappings}},
                   command=tuple(value.get("command", [])), **{key: tuple(value.get(key, {}).items()) for key in mappings})

    def resolve(self, env):
        if self.transport == "stdio":
            values, missing = dict(self.env), []
            for name, variable in self.env_from:
                if env.get(variable):
                    values[name] = env[variable]
                else:
                    missing.append(variable)
            result = replace(self, env=tuple(values.items()), env_from=())
            object.__setattr__(result, "env_from", self.env_from)
            object.__setattr__(result, "missing_env", tuple(missing))
            return result
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
        value = {"name": self.name, "transport": self.transport, "url": self.url, "protocol_version": self.protocol_version,
                "enabled": self.enabled, "timeout": self.timeout, "deadline": self.deadline, "proxy": self.proxy,
                "header_names": sorted({k.lower() for k, _ in (*self.headers, *self.headers_env)}),
                "credential_environment": sorted(v for _, v in self.headers_env), "missing_environment": list(self.missing_env)}
        if self.transport == "stdio":
            credential_names = {name.upper() for name, _ in self.env_from}
            ordinary = sorted((key, value) for key, value in self.env if key.upper() not in credential_names and not SECRET_ENV.search(key))
            value.update(command_sha256=hashlib.sha256(json.dumps(self.command).encode()).hexdigest(),
                         environment_sha256=hashlib.sha256(json.dumps(ordinary, sort_keys=True).encode()).hexdigest(),
                         argc=len(self.command), cwd=self.cwd, environment_names=sorted({k for k, _ in (*self.env, *self.env_from)}),
                         credential_environment=sorted(v for _, v in self.env_from))
        return value

    def credential_values(self):
        values = [value for _, value in self.headers]
        values.extend(environment_credentials(self.env, self.env_from))
        return tuple(part for value in values for part in credential_parts(value))


def parse_servers(value):
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("MCP accepts at most eight named servers")
    return tuple(McpServer.parse(name, fields) for name, fields in value.items())
