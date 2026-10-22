"""Trusted language-server argv and suffix mapping; no discovery or installers."""
from dataclasses import dataclass, field, replace
import re

from .mcp_config import McpServer


@dataclass(frozen=True)
class LspServer:
    name: str
    command: tuple
    languages: tuple
    enabled: bool = True
    cwd: str = "."
    env: tuple = field(default=(), repr=False)
    env_from: tuple = ()
    timeout: float = 15
    deadline: float = 60
    missing_env: tuple = ()
    _resolved: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self):
        self.process_config()
        if (not isinstance(self.languages, tuple) or not 1 <= len(self.languages) <= 32
                or any(not isinstance(row, tuple) or len(row) != 2 for row in self.languages)):
            raise ValueError("LSP languages must map 1..32 suffixes to language IDs")
        seen = set()
        for suffix, language in self.languages:
            if (not isinstance(suffix, str) or not re.fullmatch(r"\.[A-Za-z0-9_+-]{1,16}", suffix)
                    or suffix.lower() in seen or not isinstance(language, str)
                    or not re.fullmatch(r"[A-Za-z0-9_+-]{1,64}", language)):
                raise ValueError("invalid or duplicated LSP suffix/language ID")
            seen.add(suffix.lower())

    def process_config(self):
        value = McpServer(self.name, transport="stdio", command=self.command, cwd=self.cwd, env=self.env,
                          env_from=() if self._resolved else self.env_from, timeout=self.timeout, deadline=self.deadline, enabled=self.enabled)
        if self._resolved:
            object.__setattr__(value, "env_from", self.env_from)
        return value

    @classmethod
    def parse(cls, name, value):
        if (not isinstance(value, dict) or set(value) - {"command", "languages", "enabled", "cwd", "env", "env_from", "timeout", "deadline"}
                or not isinstance(value.get("command"), list) or not isinstance(value.get("languages"), dict)
                or any(not isinstance(value.get(key, {}), dict) for key in ("env", "env_from"))):
            raise ValueError("LSP requires explicit argv, languages and optional bounded process settings")
        return cls(name, tuple(value["command"]), tuple(value["languages"].items()),
                   **{key: item for key, item in value.items() if key not in {"command", "languages", "env", "env_from"}},
                   env=tuple(value.get("env", {}).items()), env_from=tuple(value.get("env_from", {}).items()))

    def resolve(self, environment):
        process = self.process_config().resolve(environment)
        result = replace(self, env=process.env, env_from=())
        object.__setattr__(result, "env_from", self.env_from)
        object.__setattr__(result, "missing_env", process.missing_env)
        object.__setattr__(result, "_resolved", True)
        return result

    def public(self):
        value = self.process_config().public()
        return {key: value[key] for key in ("name", "enabled", "timeout", "deadline", "command_sha256", "environment_sha256",
                "argc", "cwd", "environment_names", "credential_environment")} | {
                    "languages": dict(self.languages), "missing_environment": list(self.missing_env)}

    def credential_values(self):
        return self.process_config().credential_values()


def parse_servers(value):
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("LSP accepts at most eight explicitly configured servers")
    return tuple(LspServer.parse(name, entry) for name, entry in value.items())
