"""User-owned TOML settings. Never discover configuration in a repository."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import sys
import tomllib

from .config import Config, endpoint, environment_api_key, redact
from .filesystem import Directory

MAX_CONFIG = 65536
DEFAULT_URL = "https://api.openai.com/v1"
AGENT_DEFAULTS = {
    "max_steps": 20, "timeout": 30, "max_output": 16000, "stream": False,
    "read_only": False, "inherit_env": False, "max_context_bytes": 131072,
    "compact_context": False, "request_usage": False, "project_instructions": True,
    "max_tokens": None, "thinking": False, "checks": (),
}
TEMPLATE = '''# Sprig user configuration. No automatic model/provider fallback.
provider = "agnes"
interface = "tui"

[providers.agnes]
model = "agnes-3.0-flash"
base_url = "https://apihub.agnes-ai.com/v1"
api_key_env = "AGNES_API_KEY"
# Instead of api_key_env, api_key = "your-key" is supported in this private file.
# Environment references are recommended; never commit a key.

[agent]
stream = true
timeout = 60
max_steps = 20
max_tokens = 2048
thinking = false
# read_only = true
# max_context_bytes = 131072
# compact_context = true
# checks = [["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests", "-q"]]
'''


def default_config_path():
    return Path.home() / ".sprig" / "config.toml"


@contextmanager
def config_parent(path, *, create=False):
    """Traverse every component without following user symlinks/reparse points."""
    path = Path(os.path.abspath(Path(path).expanduser()))
    if sys.platform == "darwin":
        # Match the documented macOS system aliases used by SessionStore.
        for name in ("var", "tmp", "etc"):
            alias, target = Path("/") / name, Path("/private") / name
            if path.parts[:2] == alias.parts and alias.is_symlink() and alias.resolve() == target:
                path = target.joinpath(*path.parts[2:])
                break
    handle = Directory(path.anchor)
    try:
        for part in path.parts[1:-1]:
            try:
                child = handle.child(part)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    handle.mkdir(part)
                except FileExistsError:
                    pass
                child = handle.child(part)
            handle.close()
            handle = child
        yield handle, path.name
    finally:
        handle.close()


def initialize_config(path, approve):
    """Create only after exact confirmation, with exclusive/private creation."""
    path = Path(os.path.abspath(Path(path).expanduser()))
    try:
        with config_parent(path) as (directory, name):
            directory.stat(name)
    except FileNotFoundError:
        pass
    else:
        raise ValueError("configuration already exists; edit it locally or choose another --config path")
    if not approve("CREATE private Sprig configuration: " + str(path) + "\n" + TEMPLATE):
        return False
    with config_parent(path, create=True) as (directory, name):
        directory.write_new(name, TEMPLATE.encode("utf-8"), private=True)
        directory.sync()
    return True


def _text(value, *, empty=False, limit=4096):
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= limit
            and not any(ord(ch) < 32 for ch in value))


@dataclass(frozen=True)
class Settings:
    path: Path | None = None
    data: dict = field(default_factory=dict, repr=False)

    def resolve(self, options, environ=None):
        """CLI > environment > selected provider / agent defaults > built-ins."""
        env = os.environ if environ is None else environ
        provider = options.get("provider") or env.get("SPRIG_PROVIDER") or self.data.get("provider", "")
        providers = self.data.get("providers", {})
        if provider and provider not in providers:
            raise ValueError("selected provider is not defined in the configuration")
        selected = providers.get(provider, {})
        model = options.get("model") or env.get("SPRIG_MODEL") or selected.get("model", "")
        url = options.get("base_url") or env.get("SPRIG_BASE_URL") or selected.get("base_url", DEFAULT_URL)
        try:
            url = endpoint(url)
        except (ValueError, TypeError):
            raise ValueError("invalid configured base URL; use an HTTPS API root without credentials, query or fragment") from None
        key_env = selected.get("api_key_env")
        # A configured key is bound to its configured endpoint. An explicit URL
        # override must bring its own key; never forward a provider secret elsewhere.
        configured_url = endpoint(selected.get("base_url", DEFAULT_URL))
        key, source = "", "none"
        if env.get("SPRIG_API_KEY"):
            key, source = env["SPRIG_API_KEY"], "SPRIG_API_KEY"
        elif key_env or "api_key" in selected:
            if url != configured_url:
                raise ValueError("provider credentials cannot be used with an overridden base URL; set SPRIG_API_KEY explicitly")
            if key_env:
                if key_env == "AGNES_API_KEY" and not environment_api_key(url, {"AGNES_API_KEY": "present"}):
                    raise ValueError("AGNES_API_KEY is restricted to the Agnes HTTPS origin")
                if not env.get(key_env):
                    raise ValueError("configured api_key_env is unset or empty; set it in the process environment")
                key, source = env[key_env], "configured environment variable"
            else:
                key, source = selected["api_key"], "private config file"
        else:
            key = environment_api_key(url, env)
            source = "AGNES_API_KEY" if key and environment_api_key(url, {"AGNES_API_KEY": env.get("AGNES_API_KEY", "")}) else ("OPENAI_API_KEY" if key else "none")
        values = {**AGENT_DEFAULTS, **self.data.get("agent", {})}
        values.update({name: value for name, value in options.items()
                       if name in AGENT_DEFAULTS and value is not None})
        values["checks"] = tuple(tuple(argv) for argv in values["checks"])
        config = Config(workdir=Path(options.get("workdir", ".")), model=model,
                        base_url=url, api_key=key, config_path=self.path, provider_name=provider or None,
                        credential_env_vars=tuple(p["api_key_env"] for p in providers.values() if "api_key_env" in p),
                        **values)
        return config, {"provider": provider or None, "api_key_source": source,
                        "config_file": str(self.path) if self.path else None,
                        "interface": self.data.get("interface")}


def load_settings(path=None, *, required=False):
    path = Path(os.path.abspath(Path(path or default_config_path()).expanduser()))
    try:
        with config_parent(path) as (directory, name):
            raw = directory.read(name, MAX_CONFIG, private=True)
    except FileNotFoundError:
        if required:
            raise ValueError("configuration file does not exist; use --init-config to create it") from None
        return Settings()
    except (OSError, ValueError):
        raise ValueError("cannot safely read configuration; require an owner-private regular file (POSIX 0600 / Windows private ACL), without symlinks") from None
    if len(raw) > MAX_CONFIG:
        raise ValueError("configuration exceeds 64 KiB")
    try:
        data = tomllib.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError):
        # TOMLDecodeError can include the actual secret: never display it.
        raise ValueError("invalid UTF-8 TOML configuration; check syntax locally (values withheld)") from None
    if set(data) - {"provider", "providers", "agent", "interface"}:
        raise ValueError("unknown top-level configuration setting")
    if "interface" in data and data["interface"] not in ("cli", "chat", "tui"):
        raise ValueError("configuration interface must be cli, chat or tui")
    providers = data.get("providers", {})
    if not isinstance(providers, dict) or len(providers) > 32:
        raise ValueError("providers must be a table with at most 32 entries")
    for name, entry in providers.items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or not isinstance(entry, dict):
            raise ValueError("invalid provider name or table")
        if set(entry) - {"model", "base_url", "api_key_env", "api_key"}:
            raise ValueError("unknown provider setting")
        if any(not _text(value) for value in entry.values()):
            raise ValueError("provider settings must be nonempty strings without control characters")
        if "api_key" in entry and "api_key_env" in entry:
            raise ValueError("choose either api_key_env or api_key for each provider")
        if "api_key_env" in entry and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", entry["api_key_env"]):
            raise ValueError("invalid api_key_env variable name")
        try:
            endpoint(entry.get("base_url", DEFAULT_URL))
        except (ValueError, TypeError):
            raise ValueError("invalid provider base URL") from None
    if "provider" in data and (not isinstance(data["provider"], str) or data["provider"] not in providers):
        raise ValueError("default provider is not defined")
    agent = data.get("agent", {})
    if not isinstance(agent, dict) or set(agent) - set(AGENT_DEFAULTS):
        raise ValueError("unknown agent setting or invalid agent table")
    checks = agent.get("checks", [])
    if not isinstance(checks, list) or any(not isinstance(argv, list) for argv in checks):
        raise ValueError("agent checks must be an array of argv arrays")
    # Validate every configured agent field even if a flag overrides it later.
    try:
        validated = {**AGENT_DEFAULTS, **agent, "checks": tuple(tuple(argv) for argv in checks)}
        Config(path.parent, "configuration-validation", **validated)
    except (OSError, ValueError, TypeError):
        raise ValueError("invalid agent setting; check types, bounds and incompatible options") from None
    return Settings(path, data)


def public_config(config, metadata):
    """Deliberate allowlist: no raw provider table, key value or environment dump."""
    return redact({**metadata, "model": config.model, "base_url": config.base_url,
            "api_key_present": bool(config.api_key), "workdir": str(config.workdir),
            "agent": {name: getattr(config, name) for name in AGENT_DEFAULTS},
            "approvals": "each write and command requires fresh approval", "command_sandbox": False}, config.api_key)
