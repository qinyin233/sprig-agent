"""User-owned TOML settings. Never discover configuration in a repository."""

from contextlib import contextmanager
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import sys
import tomllib

from .config import Config, ENV_NAME, GENERATION_FIELDS, TOOL_NAMES, endpoint, environment_api_key, redact
from .filesystem import Directory

MAX_CONFIG = 65536
DEFAULT_URL = "https://api.openai.com/v1"
AGENT_DEFAULTS = {
    "max_steps": 20, "timeout": 30, "max_output": 16000, "stream": False,
    "read_only": False, "inherit_env": False, "max_context_bytes": 131072,
    "compact_context": False, "request_usage": False, "project_instructions": True,
    "max_tokens": None, "thinking": False, "checks": (), "check_before_task": False, "check_after_edit": False,
    "recover_output_limit": False,
}
TEMPLATE = '''# Sprig user configuration. No automatic model/provider fallback.
provider = "agnes"
interface = "tui"

[providers.agnes]
model = "agnes-3.0-flash"
base_url = "https://apihub.agnes-ai.com/v1"
api_key_env = "AGNES_API_KEY"
# retry_attempts = 2 # optional extra transient HTTP attempts; default 0, may incur usage
# Instead of api_key_env, api_key = "your-key" is supported in this private file.
# Environment references are recommended; never commit a key.

[agent]
stream = true
timeout = 60
max_steps = 20
max_tokens = 2048
thinking = false # omit Sprig's thinking preset; server-default reasoning is unchanged
# read_only = true
# max_context_bytes = 131072
# compact_context = true
# checks = [["uv", "run", "python", "-m", "unittest", "discover", "-s", "tests", "-q"]]
# check_before_task = true # requires checks; each command still asks approval
# check_after_edit = true # fixed checks after file edit batches; final checks still run

[instructions]
files = [] # e.g. ["CONTRIBUTING.md"], relative to the selected workspace
# response_language = "English"

[context]
files = [] # initial live references; small complete files refresh before requests

[skills]
dirs = [] # explicit collections, e.g. ["~/.agents/skills"]; loading never approves commands

[web]
enabled = false # opt-in public HTTP(S) documents; every fetch needs fresh approval

# Optional additional shortcuts; Enter and approval keys remain fixed.
# [tui.keybindings]
# help = ["f1"]
# toggle_sidebar = ["ctrl+b"]

# Optional reusable task text; /review ARGUMENTS sends an ordinary task.
# [prompts.review]
# description = "Review a change"
# template = "Review the code and suggest checks: $ARGUMENTS"
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
    if approve("CREATE private Sprig configuration: " + str(path) + "\n" + TEMPLATE) is not True:
        return False
    with config_parent(path, create=True) as (directory, name):
        directory.write_new(name, TEMPLATE.encode("utf-8"), private=True)
        directory.sync()
    return True


GROUP_FIELDS = {
    "agent": set(AGENT_DEFAULTS), "generation": set(GENERATION_FIELDS), "tools": set(TOOL_NAMES),
    "files": {"exclude"}, "execution": {"inherit_env", "timeout", "max_output", "output_limit", "env_allowlist", "env",
                                         "background", "background_timeout", "command_sandbox", "sandbox_read_dirs", "explore"},
    "storage": {"session_dir", "change_history_dir", "command_log_dir", "auto_save_session", "session_max_age_days", "session_max_count"},
    "context": {"max_bytes", "compact", "trigger_ratio", "keep_recent_messages", "keep_read_bytes", "files"},
    "tui": {"theme", "sidebar", "editor_height", "steering_mode", "metrics", "keybindings"},
    "instructions": {"files", "response_language"},
    "skills": {"dirs"},
    "mcp": {"servers"},
    "lsp": {"servers"},
    "web": {"enabled"},
    "prompts": set(),
    "editor": {"command", "timeout"},
}
TUI_FIELDS = {"theme": "tui_theme", "sidebar": "tui_sidebar", "editor_height": "tui_editor_height",
              "steering_mode": "steering_mode", "metrics": "show_metrics", "keybindings": "tui_keybindings"}
TRANSPORT_FIELDS = {"request_timeout", "request_deadline", "retry_attempts", "proxy", "api_key_header", "api_key_scheme", "preserve_reasoning", "check_history_format", "wire_api"}
CONTEXT_FIELDS = {"max_bytes": "max_context_bytes", "compact": "compact_context",
                  "trigger_ratio": "context_trigger_ratio", "keep_recent_messages": "context_keep_recent_messages",
                  "keep_read_bytes": "context_keep_read_bytes", "files": "reference_files"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _text(value, *, limit=4096):
    return isinstance(value, str) and bool(value) and len(value) <= limit and not any(ord(ch) < 32 for ch in value)


def _table(value, fields, label):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError("unknown " + label + " setting or invalid table (values withheld)")
    return value


def _generation(value):
    _table(value, GENERATION_FIELDS, "generation")
    return value


def _flatten(layer, directory):
    """Translate only documented keys; never evaluate substitutions or commands."""
    values = {**layer.get("agent", {}), **layer.get("generation", {})}
    values.update({CONTEXT_FIELDS[name]: value for name, value in layer.get("context", {}).items()})
    execution = layer.get("execution", {})
    values.update({name: execution[name] for name in ("inherit_env", "timeout", "max_output", "command_sandbox") if name in execution})
    if "sandbox_read_dirs" in execution:
        values["sandbox_read_dirs"] = tuple(directory / Path(path).expanduser() for path in execution["sandbox_read_dirs"])
    if "output_limit" in execution:
        values["command_output_limit"] = execution["output_limit"]
    if "background" in execution:
        values["background_enabled"] = execution["background"]
    if "background_timeout" in execution:
        values["background_timeout"] = execution["background_timeout"]
    if "explore" in execution:
        values["explore_enabled"] = execution["explore"]
    if "env_allowlist" in execution:
        values["env_allowlist"] = tuple(execution["env_allowlist"])
    if "env" in execution:
        values["command_env"] = tuple(execution["env"].items())
    if "command" in layer.get("editor", {}):
        values["editor_command"] = tuple(layer["editor"]["command"])
    if "timeout" in layer.get("editor", {}):
        values["editor_timeout"] = layer["editor"]["timeout"]
    if "exclude" in layer.get("files", {}):
        values["excluded_paths"] = tuple(layer["files"]["exclude"])
    values.update({TUI_FIELDS[name]: value for name, value in layer.get("tui", {}).items()})
    if "tui_keybindings" in values:
        values["tui_keybindings"] = tuple((name, tuple(keys)) for name, keys in values["tui_keybindings"].items())
    instructions = layer.get("instructions", {})
    if "files" in instructions:
        values["instruction_files"] = tuple(instructions["files"])
    if "response_language" in instructions:
        values["response_language"] = instructions["response_language"]
    if "dirs" in layer.get("skills", {}):
        values["skill_dirs"] = tuple(Path(os.path.abspath(directory / Path(path).expanduser())) for path in layer["skills"]["dirs"])
    if "servers" in layer.get("mcp", {}):
        from .mcp_config import parse_servers
        values["mcp_servers"] = parse_servers(layer["mcp"]["servers"])
    if "servers" in layer.get("lsp", {}):
        from .lsp_config import parse_servers as parse_lsp
        values["lsp_servers"] = parse_lsp(layer["lsp"]["servers"])
    if "enabled" in layer.get("web", {}):
        values["web_enabled"] = layer["web"]["enabled"]
    if "prompts" in layer:
        from .prompt_templates import parse_templates
        values["prompt_templates"] = parse_templates(layer["prompts"])
    for name, value in layer.get("storage", {}).items():
        if name.endswith("_dir"):
            value = Path(value).expanduser()
            value = value if value.is_absolute() else directory / value
        values[name] = value
    if "checks" in values:
        values["checks"] = tuple(tuple(argv) for argv in values["checks"])
    return values


def _validate_layer(layer, directory):
    for name, fields in GROUP_FIELDS.items():
        if name == "prompts":
            from .prompt_templates import parse_templates
            parse_templates(layer.get(name, {}))
            continue
        entry = _table(layer.get(name, {}), fields, name)
        if name == "tools" and any(type(value) is not bool for value in entry.values()):
            raise ValueError("tool settings must be booleans")
    checks = layer.get("agent", {}).get("checks", [])
    for name in ("check_before_task", "check_after_edit"):
        if name in layer.get("agent", {}) and type(layer["agent"][name]) is not bool:
            raise ValueError("agent " + name + " must be a boolean")
    if not isinstance(checks, list) or any(not isinstance(argv, list) for argv in checks):
        raise ValueError("agent checks must be an array of argv arrays")
    execution = layer.get("execution", {})
    if "command" in layer.get("editor", {}) and not isinstance(layer["editor"]["command"], list):
        raise ValueError("editor.command must be an argv array")
    if "env_allowlist" in execution and not isinstance(execution["env_allowlist"], list):
        raise ValueError("execution env_allowlist must be an array")
    if "env" in execution and not isinstance(execution["env"], dict):
        raise ValueError("execution env must be a table")
    if "sandbox_read_dirs" in execution and (not isinstance(execution["sandbox_read_dirs"], list)
            or any(not _text(path) for path in execution["sandbox_read_dirs"])):
        raise ValueError("execution sandbox_read_dirs must be an array of paths")
    if "exclude" in layer.get("files", {}) and not isinstance(layer["files"]["exclude"], list):
        raise ValueError("files exclude must be an array")
    if "files" in layer.get("instructions", {}) and not isinstance(layer["instructions"]["files"], list):
        raise ValueError("instruction files must be an array")
    if "files" in layer.get("context", {}) and not isinstance(layer["context"]["files"], list):
        raise ValueError("context files must be an array of workspace-relative POSIX paths")
    if "dirs" in layer.get("skills", {}) and (not isinstance(layer["skills"]["dirs"], list)
            or any(not _text(path) for path in layer["skills"]["dirs"])):
        raise ValueError("skill directories must be an array of explicit paths")
    shortcuts = layer.get("tui", {}).get("keybindings", {})
    if not isinstance(shortcuts, dict) or any(not isinstance(keys, list) for keys in shortcuts.values()):
        raise ValueError("TUI keybindings must map actions to arrays of keys")
    for name, value in layer.get("storage", {}).items():
        if name.endswith("_dir") and not _text(value):
            raise ValueError("storage directories must be nonempty paths")
    values = {**AGENT_DEFAULTS, **_flatten(layer, directory)}
    values["disabled_tools"] = tuple(name for name, enabled in layer.get("tools", {}).items() if not enabled)
    try:
        # A higher-precedence spelling must not hide an invalid lower one.
        # Validate each group as well as the final combination.
        for name in GROUP_FIELDS:
            if name in layer:
                separate = {**AGENT_DEFAULTS, **_flatten({name: layer[name]}, directory)}
                if name != "agent":
                    separate["stream"] = values["stream"]
                # Checks may come from a trusted profile, lower layer or CLI.
                # The fully resolved Config still requires nonempty checks.
                if not separate["checks"]:
                    separate["check_before_task"] = False
                    separate["check_after_edit"] = False
                Config(directory, "configuration-validation", **separate)
        if not values["checks"]:
            values["check_before_task"] = False
            values["check_after_edit"] = False
        Config(directory, "configuration-validation", **values)
    except (OSError, ValueError, TypeError):
        raise ValueError("invalid agent/generation/runtime setting; check types, bounds and incompatible options (values withheld)") from None


def _validate(data, directory, *, project=False):
    allowed = {*GROUP_FIELDS, "interface", "schema_version"}
    if project:
        allowed |= {"model", "profile"}
    else:
        allowed |= {"provider", "providers", "profiles", "profile"}
    _table(data, allowed, "top-level configuration")
    if "schema_version" in data and (type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise ValueError("unsupported configuration schema_version; expected 1")
    if "interface" in data and data["interface"] not in ("cli", "chat", "tui"):
        raise ValueError("configuration interface must be cli, chat or tui")
    if "model" in data and not _text(data["model"], limit=200):
        raise ValueError("invalid model selection")
    _validate_layer(data, directory)
    if project:
        # An explicit repository file may tune requests and tighten tool policy.
        # It cannot route credentials, inject environment, or persist transcripts.
        if set(data) & {"execution", "storage", "skills", "mcp", "web", "lsp", "prompts", "editor"}:
            raise ValueError("project configuration cannot change execution environment, storage, skill roots, MCP/LSP servers, web opt-in, trusted prompts or editor commands")
        if "keybindings" in data.get("tui", {}):
            raise ValueError("project configuration cannot change trusted keyboard shortcuts")
        if any(data.get("agent", {}).get(name) for name in ("inherit_env", "checks")):
            raise ValueError("project configuration cannot inherit environment or add commands")
        if "recover_output_limit" in data.get("agent", {}):
            raise ValueError("project configuration cannot set agent.recover_output_limit")
        if "profile" in data:
            raise ValueError("select trusted profiles with --profile or the user configuration")
        return
    providers = data.get("providers", {})
    if not isinstance(providers, dict) or len(providers) > 32:
        raise ValueError("providers must be a table with at most 32 entries")
    for name, entry in providers.items():
        if not IDENTIFIER.fullmatch(name):
            raise ValueError("invalid provider name")
        _table(entry, {"model", "base_url", "api_key_env", "api_key", "headers", "headers_env", "models", "generation", *TRANSPORT_FIELDS}, "provider")
        for key in ("model", "base_url", "api_key_env", "api_key"):
            if key in entry and not _text(entry[key], limit=200 if key == "model" else 4096):
                raise ValueError("provider strings must be nonempty and contain no control characters")
        if "api_key" in entry and "api_key_env" in entry:
            raise ValueError("choose either api_key_env or api_key for each provider")
        if "api_key_env" in entry and not ENV_NAME.fullmatch(entry["api_key_env"]):
            raise ValueError("invalid api_key_env variable name")
        try:
            url = endpoint(entry.get("base_url", DEFAULT_URL))
        except (ValueError, TypeError):
            raise ValueError("invalid provider base URL") from None
        headers = entry.get("headers", {})
        header_env = entry.get("headers_env", {})
        if not isinstance(headers, dict) or not isinstance(header_env, dict):
            raise ValueError("headers and headers_env must be tables")
        if any(not isinstance(value, str) or not ENV_NAME.fullmatch(value) for value in header_env.values()):
            raise ValueError("headers_env values must be environment variable names")
        generation = _generation(entry.get("generation", {}))
        try:
            Config(directory, "configuration-validation", base_url=url, **generation,
                   **{key: entry[key] for key in TRANSPORT_FIELDS if key in entry},
                   headers=tuple(headers.items()) + tuple((key, "validation") for key in header_env))
        except (OSError, ValueError, TypeError):
            raise ValueError("invalid provider transport, header or generation setting (values withheld)") from None
        models = entry.get("models", {})
        if not isinstance(models, dict) or len(models) > 64:
            raise ValueError("models must be a table with at most 64 aliases per provider")
        ids = set()
        for alias, model in models.items():
            if not IDENTIFIER.fullmatch(alias):
                raise ValueError("invalid model alias")
            _table(model, {"id", "generation", "max_context_bytes"}, "model")
            if not _text(model.get("id"), limit=200) or model["id"] in ids:
                raise ValueError("each model alias requires a unique nonempty id")
            ids.add(model["id"])
            try:
                Config(directory, model["id"], **_generation(model.get("generation", {})),
                       **({"max_context_bytes": model["max_context_bytes"]} if "max_context_bytes" in model else {}))
            except (OSError, ValueError, TypeError):
                raise ValueError("invalid model generation or context setting (values withheld)") from None
    if "provider" in data and (not isinstance(data["provider"], str) or data["provider"] not in providers):
        raise ValueError("default provider is not defined")
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict) or len(profiles) > 32:
        raise ValueError("profiles must be a table with at most 32 entries")
    for name, profile in profiles.items():
        if not IDENTIFIER.fullmatch(name):
            raise ValueError("invalid profile name")
        _table(profile, {*GROUP_FIELDS, "provider", "model", "interface"}, "profile")
        if "provider" in profile and (not isinstance(profile["provider"], str) or profile["provider"] not in providers):
            raise ValueError("profile provider is not defined")
        if "model" in profile and not _text(profile["model"], limit=200):
            raise ValueError("invalid profile model")
        if "interface" in profile and profile["interface"] not in ("cli", "chat", "tui"):
            raise ValueError("invalid profile interface")
        _validate_layer(profile, directory)
    if "profile" in data and (not isinstance(data["profile"], str) or data["profile"] not in profiles):
        raise ValueError("default profile is not defined")


def credential_snapshot(data, env):
    """Protect credentials in every provider/profile, without selecting a service."""
    names = set()
    secrets = {env[name] for name in ("SPRIG_API_KEY", "OPENAI_API_KEY", "AGNES_API_KEY") if env.get(name)}
    for entry in data.get("providers", {}).values():
        names.update(entry.get("headers_env", {}).values())
        if entry.get("api_key_env"):
            names.add(entry["api_key_env"])
        secrets.update(entry.get("headers", {}).values())
        if entry.get("api_key"):
            secrets.add(entry["api_key"])
    from .mcp_config import credential_parts, environment_credentials
    for layer in (data, *data.get("profiles", {}).values()):
        for entry in layer.get("mcp", {}).get("servers", {}).values():
            names.update(entry.get("headers_env", {}).values())
            names.update(entry.get("env_from", {}).values())
            secrets.update(entry.get("headers", {}).values())
            secrets.update(environment_credentials(entry.get("env", {}).items()))
        for entry in layer.get("lsp", {}).get("servers", {}).values():
            names.update(entry.get("env_from", {}).values())
            secrets.update(environment_credentials(entry.get("env", {}).items()))
    secrets.update(env[name] for name in names if env.get(name))
    secrets = {part for value in secrets for part in credential_parts(value)}
    if len(names) > 128 or len(secrets) > 128 or any(not isinstance(value, str) or len(value) > 4096 for value in secrets):
        raise ValueError("configuration credential protection exceeds its limit; values withheld")
    return names, secrets


@dataclass(frozen=True)
class Settings:
    path: Path | None = None
    data: dict = field(default_factory=dict, repr=False)
    project_path: Path | None = None
    project_data: dict = field(default_factory=dict, repr=False)

    def with_project(self, path):
        project = load_settings(path, required=True, project=True)
        return Settings(self.path, self.data, project.path, project.data)

    def selection(self, options, env):
        name = options.get("profile") or env.get("SPRIG_PROFILE") or self.data.get("profile")
        if name and name not in self.data.get("profiles", {}):
            raise ValueError("selected profile is not defined")
        profile = self.data.get("profiles", {}).get(name, {})
        interface = options.get("interface") or self.project_data.get("interface") or profile.get("interface") or self.data.get("interface")
        return name, profile, interface

    def catalog(self):
        return tuple({"provider": name, "alias": alias, "id": model["id"],
                      "generation": model.get("generation", {}),
                      **({"max_context_bytes": model["max_context_bytes"]} if "max_context_bytes" in model else {})}
                     for name, entry in self.data.get("providers", {}).items()
                     for alias, model in entry.get("models", {}).items())

    def resolve(self, options, environ=None, *, configured_auth_only=False):
        """CLI > project > profile > model > provider > user > built-ins.

        Provider/model/profile environment selectors sit immediately below CLI.
        A project layer can only tighten trusted read-only/tool/file policy.
        """
        env = os.environ if environ is None else environ
        profile_name, profile, interface = self.selection(options, env)
        providers = self.data.get("providers", {})
        provider = options.get("provider") or env.get("SPRIG_PROVIDER") or profile.get("provider") or self.data.get("provider", "")
        if provider and provider not in providers:
            raise ValueError("selected provider is not defined in the configuration")
        selected = providers.get(provider, {})
        profile_model = profile.get("model") if not profile.get("provider") or profile["provider"] == provider else None
        alias = options.get("model") or env.get("SPRIG_MODEL") or self.project_data.get("model") or profile_model or selected.get("model", "")
        models = selected.get("models", {})
        model_settings = models.get(alias, next((entry for entry in models.values() if entry["id"] == alias), {}))
        model = model_settings.get("id", alias)
        managing = options.get("_manage_sessions", False)
        if managing:
            model = "session-manager"
        url = options.get("base_url") or env.get("SPRIG_BASE_URL") or selected.get("base_url", DEFAULT_URL)
        try:
            url = endpoint(url)
        except (ValueError, TypeError):
            raise ValueError("invalid configured base URL; use an HTTPS API root without credentials, query or fragment") from None
        configured_url = endpoint(selected.get("base_url", DEFAULT_URL))
        key_env = selected.get("api_key_env")
        key, source = "", "none"
        if not managing:
            if not configured_auth_only and env.get("SPRIG_API_KEY"):
                key, source = env["SPRIG_API_KEY"], "SPRIG_API_KEY"
            elif key_env or "api_key" in selected:
                if url != configured_url:
                    raise ValueError("provider credentials cannot be used with an overridden base URL; set SPRIG_API_KEY explicitly")
                if key_env:
                    if key_env.upper() == "AGNES_API_KEY" and not environment_api_key(url, {"AGNES_API_KEY": "present"}):
                        raise ValueError("AGNES_API_KEY is restricted to the Agnes HTTPS origin")
                    if not env.get(key_env):
                        raise ValueError("configured api_key_env is unset or empty; set it in the process environment")
                    key, source = env[key_env], "configured environment variable"
                else:
                    key, source = selected["api_key"], "private config file"
            else:
                key = "" if configured_auth_only else environment_api_key(url, env)
                source = "AGNES_API_KEY" if key and environment_api_key(url, {"AGNES_API_KEY": env.get("AGNES_API_KEY", "")}) else ("OPENAI_API_KEY" if key else "none")
        # All configured credentials are protected, including unused providers.
        names, secrets = credential_snapshot(self.data, env)
        headers = dict(selected.get("headers", {}))
        from .mcp_config import credential_parts
        if headers or selected.get("headers_env"):
            if url != configured_url:
                raise ValueError("configured headers cannot follow an overridden base URL")
            for name, variable in selected.get("headers_env", {}).items():
                if variable.upper() == "AGNES_API_KEY" and not environment_api_key(url, {"AGNES_API_KEY": "present"}):
                    raise ValueError("AGNES_API_KEY is restricted to the Agnes HTTPS origin")
                if not managing and not env.get(variable):
                    raise ValueError("configured header environment variable is unset or empty")
                if env.get(variable):
                    headers[name] = env[variable]
        directory = self.path.parent if self.path else Path(options.get("workdir", ".")).resolve()
        values = dict(AGENT_DEFAULTS)
        sources = {name: "default" for name in {*AGENT_DEFAULTS, *GENERATION_FIELDS, *TRANSPORT_FIELDS,
                    *TUI_FIELDS.values(), "instruction_files", "response_language", "excluded_paths",
                    "session_dir", "change_history_dir", "command_log_dir", "auto_save_session", "env_allowlist", "command_env",
                    "command_sandbox", "sandbox_read_dirs", "explore_enabled",
                    "session_max_age_days", "session_max_count", "command_output_limit", "background_enabled", "background_timeout", "prompt_templates", "editor_command", "editor_timeout", *CONTEXT_FIELDS.values()}}
        def apply(entries, label):
            if "prompt_templates" in entries:
                merged_prompts = {row.name: row for row in values.get("prompt_templates", ())}
                merged_prompts.update((row.name, row) for row in entries["prompt_templates"])
                sources.update({"prompts." + row.name: label for row in entries["prompt_templates"]})
                entries = {**entries, "prompt_templates": tuple(merged_prompts.values())}
            if "tui_keybindings" in entries:
                merged = dict(values.get("tui_keybindings", ()))
                merged.update(entries["tui_keybindings"])
                sources.update({"tui.keybindings." + action: label for action, _ in entries["tui_keybindings"]})
                entries = {**entries, "tui_keybindings": tuple(merged.items())}
            values.update(entries)
            sources.update({name: label for name in entries})
            if "tui_keybindings" in entries and entries["tui_keybindings"]:
                sources["tui_keybindings"] = " + ".join(dict.fromkeys(sources["tui.keybindings." + action] for action, _ in entries["tui_keybindings"]))
        apply(_flatten(self.data, directory), "user")
        apply(selected.get("generation", {}), "provider")
        apply(model_settings.get("generation", {}), "model")
        if "max_context_bytes" in model_settings:
            apply({"max_context_bytes": model_settings["max_context_bytes"]}, "model")
        apply(_flatten(profile, directory), "profile")
        # A trusted profile replaces a complete per-server definition by name.
        if "mcp_servers" in values:
            from .mcp_config import parse_servers
            combined = {**self.data.get("mcp", {}).get("servers", {}), **profile.get("mcp", {}).get("servers", {})}
            values["mcp_servers"] = tuple(server.resolve(env) for server in parse_servers(combined))
        if "lsp_servers" in values:
            from .lsp_config import parse_servers as parse_lsp
            combined_lsp = {**self.data.get("lsp", {}).get("servers", {}), **profile.get("lsp", {}).get("servers", {})}
            values["lsp_servers"] = tuple(server.resolve(env) for server in parse_lsp(combined_lsp))
        tool_policy = {**self.data.get("tools", {}), **profile.get("tools", {})}
        if self.project_path:
            project_values = _flatten(self.project_data, self.project_path.parent)
            if values.get("read_only") and project_values.get("read_only") is False:
                raise ValueError("project configuration cannot disable trusted read-only policy")
            if values.get("check_before_task") and project_values.get("check_before_task") is False:
                raise ValueError("project configuration cannot disable trusted baseline checks")
            if values.get("check_after_edit") and project_values.get("check_after_edit") is False:
                raise ValueError("project configuration cannot disable trusted edit checks")
            if any(enabled and (tool_policy.get(name) is False
                                or name in {"search", "grep"} and any(tool_policy.get(alias) is False for alias in ("search", "grep"))
                                or name in {"edit_lines", "file_action"} and tool_policy.get("edit_file") is False)
                   for name, enabled in self.project_data.get("tools", {}).items()):
                raise ValueError("project configuration cannot enable a disabled tool")
            if "excluded_paths" in project_values:
                project_values["excluded_paths"] = tuple(dict.fromkeys((*values.get("excluded_paths", ()), *project_values["excluded_paths"])))
            previous_exclusions = sources["excluded_paths"] if values.get("excluded_paths") else None
            apply(project_values, "project")
            if previous_exclusions and "excluded_paths" in project_values:
                sources["excluded_paths"] = previous_exclusions + " + project"
            tool_policy.update(self.project_data.get("tools", {}))
        apply({name: selected[name] for name in TRANSPORT_FIELDS if name in selected}, "provider")
        configurable = {*AGENT_DEFAULTS, *GENERATION_FIELDS, *TRANSPORT_FIELDS, *CONTEXT_FIELDS.values(),
                        "session_dir", "change_history_dir", "command_log_dir", "show_metrics", "instruction_files", "response_language",
                        "session_max_age_days", "session_max_count", "command_output_limit", "skill_dirs", "web_enabled", "background_enabled", "background_timeout"}
        overrides = {name: value for name, value in options.items() if name in configurable and value is not None}
        if "skill_dirs" in overrides:
            overrides["skill_dirs"] = tuple(overrides["skill_dirs"])
        if "reference_files" in overrides:
            from .references import user_reference_paths
            overrides["reference_files"] = user_reference_paths(Path(options.get("workdir", ".")).expanduser().resolve(strict=True),
                                                                overrides["reference_files"])
        apply(overrides, "CLI")
        if options.get("_tui_stream"):
            sources["stream"] = "TUI"
        for name in options.get("disable_tool") or ():
            tool_policy[name] = False
        values["disabled_tools"] = tuple(name for name in TOOL_NAMES if tool_policy.get(name) is False)
        values["checks"] = tuple(tuple(argv) for argv in values["checks"])
        if "instruction_files" in values:
            values["instruction_files"] = tuple(values["instruction_files"])
        for name in TOOL_NAMES:
            sources["tools." + name] = ("CLI" if name in (options.get("disable_tool") or ()) else
                "project" if name in self.project_data.get("tools", {}) else
                "profile" if name in profile.get("tools", {}) else "user" if name in self.data.get("tools", {}) else "default")
        if tool_policy.get("edit_file") is False:
            sources["tools.edit_lines"] = "tools.edit_file"
            sources["tools.file_action"] = "tools.edit_file"
        for name, alias in (("search", "grep"), ("grep", "search")):
            if tool_policy.get(name) is not False and tool_policy.get(alias) is False:
                sources["tools." + name] = "tools." + alias
        def selection_source(entries):
            return next((label for value, label in entries if value), "default")
        sources["model"] = selection_source(((options.get("model"), "CLI"), (env.get("SPRIG_MODEL"), "environment"),
            (self.project_data.get("model"), "project"), (profile_model, "profile"), (selected.get("model"), "provider")))
        sources["provider"] = selection_source(((options.get("provider"), "CLI"), (env.get("SPRIG_PROVIDER"), "environment"),
            (profile.get("provider"), "profile"), (self.data.get("provider"), "user")))
        sources["base_url"] = selection_source(((options.get("base_url"), "CLI"), (env.get("SPRIG_BASE_URL"), "environment"),
            (selected.get("base_url"), "provider")))
        config = Config(workdir=Path(options.get("workdir", ".")), model=model, base_url=url, api_key=key,
                        config_path=self.path, config_paths=(self.project_path,) if self.project_path else (),
                        provider_name=provider or None, profile_name=profile_name, credential_env_vars=tuple(sorted(names)),
                        headers=tuple(headers.items()), secret_values=tuple(sorted(secrets)), model_catalog=self.catalog(),
                        setting_sources=tuple(sorted(sources.items())), **values)
        return config, {"provider": provider or None, "profile": profile_name, "api_key_source": source,
                        "config_file": str(self.path) if self.path else None,
                        "project_config_file": str(self.project_path) if self.project_path else None, "interface": interface}


def load_settings(path=None, *, required=False, project=False):
    path = Path(os.path.abspath(Path(path or default_config_path()).expanduser()))
    try:
        with config_parent(path) as (directory, name):
            raw = directory.read(name, MAX_CONFIG, private=True)
            path = directory.path / name
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
    except (UnicodeError, ValueError, RecursionError) as error:
        # Python 3.11 puts position in the message; 3.14 exposes attributes.
        position = re.search(r"\(at line (\d+), column (\d+)\)", str(error))
        suffix = " at line " + position[1] + ", column " + position[2] if position else ""
        raise ValueError("invalid UTF-8 TOML configuration" + suffix + "; check syntax locally (values withheld)") from None
    _validate(data, path.parent, project=project)
    return Settings(path, data)


def public_config(config, metadata):
    """Deliberate allowlist; header/environment/credential values stay private."""
    return redact({**metadata, "model": config.model, "base_url": config.base_url,
            "api_key_present": bool(config.api_key), "workdir": str(config.workdir),
            "agent": {name: getattr(config, name) for name in AGENT_DEFAULTS},
            "generation": config.generation_options(),
            "context": {name: getattr(config, field) for name, field in CONTEXT_FIELDS.items()},
            "transport": {"request_timeout": config.request_timeout, "request_deadline": config.request_deadline, "proxy": config.proxy,
                          "retry_attempts": config.retry_attempts,
                          "preserve_reasoning": config.preserve_reasoning,
                          "wire_api": config.wire_api,
                          "check_history_format": config.check_history_format,
                          "api_key_header": config.api_key_header, "api_key_scheme": config.api_key_scheme,
                          "header_names": [name for name, _ in config.headers]},
            "tools": {name: config.tool_enabled(name) for name in TOOL_NAMES},
            "files": {"exclude": config.excluded_paths},
            "instructions": {"files": config.instruction_files, "response_language": config.response_language},
            "skills": {"dirs": [str(path) for path in config.skill_dirs]},
            "prompts": [{"name": row.name, "description": row.description, "template_bytes": len(row.template.encode("utf-8"))}
                        for row in config.prompt_templates],
            "web": {"enabled": config.web_enabled, "direct_only": True, "persistent_cache": False},
            "background": {"enabled": config.background_enabled, "timeout": config.background_timeout, "persistent": False},
            "explore": {"enabled": config.explore_enabled, "foreground": True},
            "editor": {"command": config.editor_command, "timeout": config.editor_timeout},
            "mcp": {"servers": [server.public() for server in config.mcp_servers]},
            "lsp": {"servers": [server.public() for server in config.lsp_servers]},
            "execution": {"env_allowlist": config.env_allowlist, "env_names": [name for name, _ in config.command_env],
                          "command_sandbox": config.command_sandbox, "sandbox_read_dirs": [str(path) for path in config.sandbox_read_dirs],
                          "max_output": config.max_output, "output_limit": config.command_output_limit or config.max_output},
            "storage": {"session_dir": str(config.session_dir) if config.session_dir else None,
                        "change_history_dir": str(config.change_history_dir) if config.change_history_dir else None,
                        "command_log_dir": str(config.command_log_dir) if config.command_log_dir else None,
                        "auto_save_session": config.auto_save_session,
                        "session_max_age_days": config.session_max_age_days, "session_max_count": config.session_max_count},
            "tui": {name: (dict(config.tui_keybindings) if name == "keybindings" else getattr(config, field)) for name, field in TUI_FIELDS.items()},
            "setting_sources": dict(config.setting_sources),
            "approvals": "each write and command requires fresh approval", "command_sandbox": config.command_sandbox != "off"}, config.secrets)
