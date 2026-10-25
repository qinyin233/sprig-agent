"""Validated runtime configuration and shared safety helpers."""

from dataclasses import dataclass, field, fields
import json
import math
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from .credentials import RuntimeCredentials

TOOL_NAMES = ("list_files", "read_file", "search", "grep", "project_map", "edit_file", "run", "update_plan", "edit_lines", "file_action", "read_history", "read_log", "question", "skill", "mcp", "web_fetch", "job", "lsp")
SHORTCUT_ACTIONS = ("cancel", "quit", "focus_input", "toggle_sidebar", "help", "new_conversation")
GENERATION_FIELDS = ("max_tokens", "thinking", "temperature", "top_p", "reasoning_effort", "seed", "token_limit_field", "extra_body")
MODEL_CONFIG_FIELDS = frozenset((*GENERATION_FIELDS, "model", "base_url", "api_key", "provider_name", "headers",
    "request_timeout", "request_deadline", "retry_attempts", "proxy", "api_key_header", "api_key_scheme", "preserve_reasoning",
    "check_history_format", "wire_api", "max_context_bytes", "secret_values", "credential_env_vars", "setting_sources"))


def validate_model_change(current, selected):
    if (not isinstance(selected, Config) or selected._runtime_credentials is not current._runtime_credentials
            or any(getattr(selected, row.name) != getattr(current, row.name) for row in fields(Config)
                   if row.name not in MODEL_CONFIG_FIELDS and row.name != "_runtime_credentials")
            or not set(current.credential_secrets).issubset(selected.credential_secrets)):
        raise ValueError("model selection cannot change workspace/tool policy or discard credential protection")
RESERVED_BODY_FIELDS = {"model", "messages", "tools", "tool_choice", "stream", "stream_options", "n",
                        "max_tokens", "max_completion_tokens", "temperature", "top_p", "reasoning_effort", "seed",
                        "prompt", "system", "input", "instructions", "api_key", "headers", "base_url",
                        "store", "previous_response_id", "conversation", "background", "include", "max_output_tokens"}
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
DEFAULT_COMMAND_OUTPUT_LIMIT = 1024 * 1024
HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}")
FORBIDDEN_HEADERS = {"host", "content-length", "content-type", "transfer-encoding", "connection", "upgrade",
                     "cookie", "set-cookie", "proxy-authorization", "authorization", "accept", "user-agent"}


def validate_exclusions(patterns):
    if (not isinstance(patterns, (list, tuple)) or len(patterns) > 256
            or any(not isinstance(p, str) or not p or len(p) > 256 or p.startswith(("!", "/"))
                   or "\\" in p or ".." in p.split("/") or any(ord(c) < 32 for c in p) for p in patterns)):
        raise ValueError("exclusions accept at most 256 relative patterns; negation is not supported")
    return tuple(patterns)


def environment_api_key(base_url, environ=None):
    """Explicit key wins; the Agnes alias is confined to its HTTPS origin."""
    values = os.environ if environ is None else environ
    if values.get("SPRIG_API_KEY"):
        return values["SPRIG_API_KEY"]
    try:
        parsed = urlsplit(base_url)
        agnes = parsed.scheme == "https" and parsed.netloc.lower() in {
            "apihub.agnes-ai.com", "apihub.agnes-ai.com:443"}
    except (ValueError, TypeError):
        agnes = False
    if agnes and values.get("AGNES_API_KEY"):
        return values["AGNES_API_KEY"]
    return values.get("OPENAI_API_KEY", "")


def redact(value, secret):
    """Mask the configured key and long trailing prefixes, including truncation."""
    if isinstance(secret, (list, tuple)):
        value = redact(value, "")  # Keep request compaction detached even with no secrets.
        for item in secret:
            value = redact(value, item)
        return value
    if isinstance(value, str):
        if not secret:
            return value
        # A later provider/header secret must not repeatedly expand masks from
        # earlier passes. Still match full credentials that contain the marker.
        if "[REDACTED]" in secret:
            value = value.replace(secret, "[REDACTED]")
        value = "[REDACTED]".join(part.replace(secret, "[REDACTED]") for part in value.split("[REDACTED]"))
        # A tool cap or completed stream can cut the key in the middle. Long
        # trailing prefixes are withheld too; short ordinary words stay intact.
        for length in range(min(len(secret) - 1, len(value)), 7, -1):
            if value.endswith("[REDACTED]") and "[REDACTED]".endswith(secret[:length]):
                continue
            if value.endswith(secret[:length]):
                return value[:-length] + "[REDACTED]"
        return value
    if isinstance(value, list):
        return [redact(item, secret) for item in value]
    if isinstance(value, dict):
        return {redact(key, secret): redact(item, secret) for key, item in value.items()}
    return value


def terminal_text(text):
    """Make untrusted terminal controls visible, including bidi overrides."""
    return "".join(
        ch if ch == "\n" or (not unicodedata.category(ch).startswith("C"))
        else f"\\u{ord(ch):04x}" for ch in str(text)
    )


def endpoint(base_url):
    if not isinstance(base_url, str) or any(ord(ch) <= 32 for ch in base_url):
        raise ValueError("base URL contains whitespace or control characters")
    parsed = urlsplit(base_url)
    if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain credentials, query, or fragment")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ValueError("use HTTPS (HTTP is allowed only for localhost/loopback)")
    parsed.port  # Validate invalid/non-numeric ports before making a request.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))


@dataclass(frozen=True)
class Config:
    workdir: Path
    model: str
    base_url: str = "https://api.openai.com/v1"
    api_key: str = field(default="", repr=False)
    max_steps: int = 20
    timeout: float = 30
    max_output: int = 16000
    stream: bool = False
    checks: tuple = ()
    read_only: bool = False
    inherit_env: bool = False
    max_context_bytes: int = 131072
    compact_context: bool = False
    request_usage: bool = False
    project_instructions: bool = True
    max_tokens: int | None = None
    thinking: bool = False
    config_path: Path | None = field(default=None, repr=False, compare=False)
    credential_env_vars: tuple = field(default=(), repr=False, compare=False)
    provider_name: str | None = field(default=None, compare=False)
    profile_name: str | None = field(default=None, compare=False)
    temperature: float | None = None
    top_p: float | None = None
    reasoning_effort: str | None = None
    seed: int | None = None
    token_limit_field: str = "max_tokens"
    extra_body: dict | str = field(default="{}", repr=False)
    request_timeout: float | None = None
    request_deadline: float = 300
    retry_attempts: int = 0
    proxy: str = "system"
    api_key_header: str = "auto"
    api_key_scheme: str = "auto"
    headers: tuple = field(default=(), repr=False)
    secret_values: tuple = field(default=(), repr=False, compare=False)
    disabled_tools: tuple = ()
    excluded_paths: tuple = ()
    config_paths: tuple = field(default=(), repr=False, compare=False)
    env_allowlist: tuple = ()
    command_env: tuple = field(default=(), repr=False)
    session_dir: Path | None = None
    change_history_dir: Path | None = None
    command_log_dir: Path | None = None
    auto_save_session: bool = False
    session_max_age_days: int | None = None
    session_max_count: int | None = None
    context_trigger_ratio: float = 0.8
    context_keep_recent_messages: int = 6
    context_keep_read_bytes: int = 8192
    tui_theme: str | None = None
    tui_sidebar: bool = True
    tui_editor_height: int = 5
    steering_mode: str = "all"
    show_metrics: bool = False
    model_catalog: tuple = field(default=(), repr=False, compare=False)
    check_before_task: bool = False
    check_after_edit: bool = False
    instruction_files: tuple = ()
    response_language: str | None = None
    tui_keybindings: tuple = ()
    setting_sources: tuple = field(default=(), repr=False, compare=False)
    command_output_limit: int | None = DEFAULT_COMMAND_OUTPUT_LIMIT
    reference_files: tuple = ()
    preserve_reasoning: bool = False
    check_history_format: str = "tool_calls"
    wire_api: str = "chat_completions"
    skill_dirs: tuple = ()
    mcp_servers: tuple = field(default=(), repr=False)
    lsp_servers: tuple = field(default=(), repr=False)
    web_enabled: bool = False
    background_enabled: bool = False
    background_timeout: float = 300
    prompt_templates: tuple = field(default=(), repr=False)
    _runtime_credentials: RuntimeCredentials = field(default_factory=RuntimeCredentials, init=False, repr=False, compare=False)

    def __post_init__(self):
        from .prompt_templates import validate_templates
        validate_templates(self.prompt_templates)
        if type(self.background_enabled) is not bool:
            raise ValueError("execution.background must be a boolean")
        if (type(self.background_timeout) not in (int, float) or not math.isfinite(self.background_timeout)
                or not 0 < self.background_timeout <= 3600):
            raise ValueError("execution.background_timeout must be positive and at most 3600 seconds")
        if type(self.web_enabled) is not bool:
            raise ValueError("web.enabled must be a boolean")
        if self.wire_api not in ("chat_completions", "messages", "responses"):
            raise ValueError("wire-api must be chat_completions, messages or responses")
        if self.api_key_header == "auto":
            object.__setattr__(self, "api_key_header", "x-api-key" if self.wire_api == "messages" else "Authorization")
        if self.api_key_scheme == "auto":
            object.__setattr__(self, "api_key_scheme", "raw" if self.wire_api == "messages" else "bearer")
        root = Path(self.workdir).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workdir must be a directory")
        object.__setattr__(self, "workdir", root)
        object.__setattr__(self, "base_url", endpoint(self.base_url))
        from .mcp_config import McpServer
        if (not isinstance(self.mcp_servers, tuple) or len(self.mcp_servers) > 8
                or any(not isinstance(server, McpServer) for server in self.mcp_servers)
                or len({server.name for server in self.mcp_servers}) != len(self.mcp_servers)):
            raise ValueError("MCP requires at most eight unique validated servers")
        from .lsp_config import LspServer
        if (not isinstance(self.lsp_servers, tuple) or len(self.lsp_servers) > 8
                or any(not isinstance(server, LspServer) for server in self.lsp_servers)
                or len({server.name for server in self.lsp_servers}) != len(self.lsp_servers)):
            raise ValueError("LSP requires at most eight unique validated servers")
        if (not isinstance(self.skill_dirs, tuple) or len(self.skill_dirs) > 8
                or any(not isinstance(p, (str, Path)) or not str(p) or len(str(p)) > 4096
                       or any(ord(c) < 32 for c in str(p)) for p in self.skill_dirs)):
            raise ValueError("skill-dirs accepts at most eight explicit directory paths")
        skill_dirs = tuple(Path(os.path.abspath(root / Path(p).expanduser())) for p in self.skill_dirs)
        object.__setattr__(self, "skill_dirs", tuple(dict.fromkeys(skill_dirs)))
        if not isinstance(self.model, str) or not self.model or len(self.model) > 200 or any(ord(c) < 32 for c in self.model):
            raise ValueError("a valid model is required (--model, SPRIG_MODEL or config.toml); use --init-config for a template")
        if not isinstance(self.max_steps, int) or isinstance(self.max_steps, bool) or not 1 <= self.max_steps <= 100:
            raise ValueError("max-steps must be between 1 and 100")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 120:
            raise ValueError("timeout must be greater than 0 and at most 120 seconds")
        if not isinstance(self.max_output, int) or isinstance(self.max_output, bool) or not 256 <= self.max_output <= 1000000:
            raise ValueError("max-output must be between 256 and 1000000 bytes")
        if type(self.command_output_limit) is int and self.command_output_limit == 0:
            object.__setattr__(self, "command_output_limit", None)
        if self.command_output_limit is not None and (type(self.command_output_limit) is not int
                or not 256 <= self.command_output_limit <= 8 * 1024 * 1024):
            raise ValueError("command-output-limit must be 0 (stop at max-output) or between 256 and 8388608 bytes")
        if not isinstance(self.api_key, str) or any(ord(c) < 32 for c in self.api_key):
            raise ValueError("API key contains control characters")
        if not isinstance(self.stream, bool):
            raise ValueError("stream must be a boolean")
        if not isinstance(self.request_usage, bool) or (self.request_usage and not self.stream):
            raise ValueError("request-usage requires streaming and must be a boolean")
        if not isinstance(self.read_only, bool) or not isinstance(self.inherit_env, bool):
            raise ValueError("read-only and inherit-env must be booleans")
        if self.read_only and self.checks:
            raise ValueError("read-only mode cannot execute acceptance commands")
        if (not isinstance(self.max_context_bytes, int) or isinstance(self.max_context_bytes, bool)
                or not 4096 <= self.max_context_bytes <= 2 * 1024 * 1024):
            raise ValueError("max-context-bytes must be between 4096 and 2097152")
        if not isinstance(self.compact_context, bool):
            raise ValueError("compact-context must be a boolean")
        if (type(self.context_trigger_ratio) not in (int, float) or not math.isfinite(self.context_trigger_ratio)
                or not 0 <= self.context_trigger_ratio <= 1):
            raise ValueError("context-trigger-ratio must be between 0 and 1")
        if type(self.context_keep_recent_messages) is not int or not 6 <= self.context_keep_recent_messages <= 1000:
            raise ValueError("context-keep-recent-messages must be between 6 and 1000")
        if type(self.context_keep_read_bytes) is not int or not 0 <= self.context_keep_read_bytes <= 262144:
            raise ValueError("context-keep-read-bytes must be between 0 and 262144")
        for name, high in (("session_max_age_days", 36500), ("session_max_count", 256)):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or not 1 <= value <= high):
                raise ValueError(name + " must be a positive bounded integer")
        if not isinstance(self.project_instructions, bool):
            raise ValueError("project-instructions must be a boolean")
        if (not isinstance(self.instruction_files, tuple) or len(self.instruction_files) > 16
                or any(not isinstance(path, str) or not path or len(path) > 256
                       or path.startswith("/") or any(c in path for c in "\\:*?[]")
                       or any(part in {"", ".", ".."} for part in path.split("/"))
                       or any(ord(c) < 32 for c in path) for path in self.instruction_files)):
            raise ValueError("instruction files accept at most 16 workspace-relative POSIX file paths; no globs")
        object.__setattr__(self, "instruction_files", tuple(dict.fromkeys(self.instruction_files)))
        if self.response_language is not None and (not isinstance(self.response_language, str)
                or not self.response_language.strip() or len(self.response_language) > 80
                or any(unicodedata.category(c).startswith("C") for c in self.response_language)):
            raise ValueError("response-language must be a nonempty language name of at most 80 characters")
        if not isinstance(self.tui_keybindings, tuple) or len(self.tui_keybindings) > len(SHORTCUT_ACTIONS):
            raise ValueError("invalid TUI keybindings")
        assigned, actions, normalized = set(), set(), []
        for entry in self.tui_keybindings:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("invalid TUI keybindings")
            action, keys = entry
            if (not isinstance(action, str) or action not in SHORTCUT_ACTIONS or action in actions
                    or not isinstance(keys, tuple) or len(keys) > 4):
                raise ValueError("unknown or duplicate TUI action; at most four shortcut keys per action")
            actions.add(action)
            canonical = []
            for key in keys:
                if not isinstance(key, str) or not re.fullmatch(r"(?:(?:ctrl|alt|shift)\+)*(?:f(?:[1-9]|1[0-2])|[a-z0-9]|home|end|pageup|pagedown|up|down|left|right)", key):
                    raise ValueError("invalid TUI alias; use a function key or modified letter, digit or navigation key")
                parts = key.split("+")
                modifiers = parts[:-1]
                if len(set(modifiers)) != len(modifiers):
                    raise ValueError("duplicate TUI key modifier")
                value = "+".join([name for name in ("ctrl", "alt", "shift") if name in modifiers] + [parts[-1]])
                if ((not re.fullmatch(r"f(?:[1-9]|1[0-2])", parts[-1]) and not ({"ctrl", "alt"} & set(modifiers)))
                        or value in {"ctrl+c", "ctrl+q", "ctrl+l", "ctrl+j", "ctrl+a", "ctrl+v", "ctrl+x", "ctrl+z"}):
                    raise ValueError("TUI aliases cannot replace fixed safety, input, selection or clipboard keys")
                if value in assigned:
                    raise ValueError("conflicting TUI shortcut keys")
                assigned.add(value)
                canonical.append(value)
            normalized.append((action, tuple(canonical)))
        object.__setattr__(self, "tui_keybindings", tuple(normalized))
        if (self.max_tokens is not None and
                (type(self.max_tokens) is not int or not 1 <= self.max_tokens <= 65536)):
            raise ValueError("max-tokens must be between 1 and 65536")
        if not isinstance(self.thinking, bool):
            raise ValueError("thinking must be a boolean")
        if self.wire_api != "chat_completions" and (self.seed is not None or self.token_limit_field != "max_tokens"):
            raise ValueError("seed and token-limit-field are Chat Completions settings")
        if type(self.preserve_reasoning) is not bool:
            raise ValueError("preserve-reasoning must be a boolean")
        if self.check_history_format not in ("tool_calls", "observations"):
            raise ValueError("check-history-format must be tool_calls or observations")
        if not isinstance(self.checks, tuple) or len(self.checks) > 16:
            raise ValueError("at most 16 acceptance checks are supported")
        if type(self.check_before_task) is not bool or (self.check_before_task and not self.checks):
            raise ValueError("check-before-task must be a boolean and requires fixed acceptance checks")
        if type(self.check_after_edit) is not bool or (self.check_after_edit and not self.checks):
            raise ValueError("check-after-edit must be a boolean and requires fixed acceptance checks")
        for argv in self.checks:
            if (not isinstance(argv, tuple) or not argv or len(argv) > 256
                    or any(not isinstance(arg, str) or "\0" in arg for arg in argv) or not argv[0]
                    or sum(len(arg) for arg in argv) > 65536):
                raise ValueError("each acceptance check must be a nonempty argv array")
            if self.api_key and any(self.api_key in arg for arg in argv):
                raise ValueError("configured API key must not appear in acceptance arguments")
        for name, low, high in (("temperature", 0, 2), ("top_p", 0, 1)):
            value = getattr(self, name)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high):
                raise ValueError(name + " is outside the supported finite range")
        if self.reasoning_effort is not None and (not isinstance(self.reasoning_effort, str) or self.reasoning_effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}):
            raise ValueError("invalid reasoning-effort")
        if self.seed is not None and (type(self.seed) is not int or not 0 <= self.seed <= 2147483647):
            raise ValueError("seed must be an integer between 0 and 2147483647")
        if not isinstance(self.token_limit_field, str) or self.token_limit_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError("token-limit-field must be max_tokens or max_completion_tokens")
        try:
            extra = json.loads(self.extra_body) if isinstance(self.extra_body, str) else self.extra_body
            if not isinstance(extra, dict) or set(extra) & RESERVED_BODY_FIELDS or len(extra) > 32:
                raise ValueError
            if self.thinking and "chat_template_kwargs" in extra:
                raise ValueError
            if self.thinking and ((self.wire_api == "messages" and "thinking" in extra)
                                  or (self.wire_api == "responses" and "reasoning" in extra)):
                raise ValueError
            if self.reasoning_effort is not None and ((self.wire_api == "messages" and "output_config" in extra)
                                                      or (self.wire_api == "responses" and "reasoning" in extra)):
                raise ValueError
            pending = [(extra, 0)]
            while pending:
                value, depth = pending.pop()
                if depth > 12:
                    raise ValueError
                if isinstance(value, (dict, list)):
                    if isinstance(value, dict) and any(not isinstance(key, str) for key in value):
                        raise ValueError
                    pending.extend((item, depth + 1) for item in (value.values() if isinstance(value, dict) else value))
            encoded = json.dumps(extra, sort_keys=True, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > 16384:
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise ValueError("extra-body must be a bounded JSON object without reserved request fields") from None
        object.__setattr__(self, "extra_body", encoded)
        if self.request_timeout is not None and (type(self.request_timeout) not in (int, float)
                or not math.isfinite(self.request_timeout) or not 0 < self.request_timeout <= 120):
            raise ValueError("request-timeout must be greater than 0 and at most 120 seconds")
        if (type(self.request_deadline) not in (int, float) or not math.isfinite(self.request_deadline)
                or not 0 < self.request_deadline <= 3600):
            raise ValueError("request-deadline must be greater than 0 and at most 3600 seconds")
        if type(self.retry_attempts) is not int or not 0 <= self.retry_attempts <= 5:
            raise ValueError("retry-attempts must be an integer from 0 to 5")
        if not isinstance(self.proxy, str):
            raise ValueError("proxy must be a string")
        if self.proxy not in {"system", "direct"}:
            try:
                parsed = urlsplit(self.proxy)
                if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None
                        or parsed.path not in {"", "/"} or parsed.query or parsed.fragment or any(ord(c) <= 32 for c in self.proxy)):
                    raise ValueError
                parsed.port
            except (ValueError, TypeError):
                raise ValueError("proxy must be system, direct, or an HTTP(S) proxy URL without credentials") from None
        if not isinstance(self.api_key_header, str) or not HEADER_NAME.fullmatch(self.api_key_header) or self.api_key_header.lower() in FORBIDDEN_HEADERS - {"authorization"}:
            raise ValueError("invalid API key header")
        if not isinstance(self.api_key_scheme, str) or self.api_key_scheme not in {"bearer", "raw"}:
            raise ValueError("API key scheme must be bearer or raw")
        seen = {self.api_key_header.lower()}
        if not isinstance(self.headers, tuple) or len(self.headers) > 16:
            raise ValueError("at most 16 custom headers are supported")
        for pair in self.headers:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("headers must contain name/value pairs")
            name, value = pair
            if (not isinstance(name, str) or not HEADER_NAME.fullmatch(name) or name.lower() in FORBIDDEN_HEADERS | seen
                    or not isinstance(value, str) or not value or len(value) > 4096
                    or any(ord(c) < 32 or ord(c) > 255 for c in value)):
                raise ValueError("invalid, reserved or duplicated HTTP header")
            seen.add(name.lower())
        if (not isinstance(self.disabled_tools, tuple) or any(name not in TOOL_NAMES for name in self.disabled_tools)
                or len(set(self.disabled_tools)) != len(self.disabled_tools)):
            raise ValueError("disabled-tools contains unknown or duplicate tool names")
        if "run" in self.disabled_tools and self.checks:
            raise ValueError("disabled run tool cannot execute acceptance checks")
        object.__setattr__(self, "excluded_paths", validate_exclusions(self.excluded_paths))
        for values in (self.env_allowlist, self.credential_env_vars):
            if not isinstance(values, tuple) or len(values) > 128 or any(not isinstance(name, str) or not ENV_NAME.fullmatch(name) for name in values):
                raise ValueError("environment variable names must be bounded identifiers")
        if not isinstance(self.secret_values, tuple) or len(self.secret_values) > 128 or any(not isinstance(value, str) or len(value) > 4096 for value in self.secret_values):
            raise ValueError("invalid credential redaction values")
        if not isinstance(self.command_env, tuple) or len(self.command_env) > 32:
            raise ValueError("at most 32 command environment overrides are supported")
        object.__setattr__(self, "credential_env_vars", tuple(dict.fromkeys((*self.credential_env_vars,
                           *(variable for server in self.mcp_servers for _, variable in (*server.headers_env, *server.env_from)),
                           *(variable for server in self.lsp_servers for _, variable in server.env_from)))))
        protected = {"SPRIG_API_KEY", "OPENAI_API_KEY", "AGNES_API_KEY", *(name.upper() for name in self.credential_env_vars)}
        for pair in self.command_env:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValueError("command environment must contain name/value pairs")
            name, value = pair
            if (not isinstance(name, str) or not ENV_NAME.fullmatch(name) or name.upper() in protected
                    or not isinstance(value, str) or "\0" in value or len(value) > 8192
                    or any(secret and secret in value for secret in self.credential_secrets)):
                raise ValueError("invalid command environment or attempted model credential inheritance")
        if any(secret in arg for argv in self.checks for arg in argv for secret in self.credential_secrets):
            raise ValueError("configured credentials must not appear in acceptance arguments")
        if self.tui_theme is not None and (not isinstance(self.tui_theme, str) or self.tui_theme not in {"textual-dark", "textual-light", "nord", "gruvbox", "dracula", "monokai", "tokyo-night", "solarized-dark", "solarized-light"}):
            raise ValueError("unsupported TUI theme")
        if type(self.tui_editor_height) is not int or not 3 <= self.tui_editor_height <= 12:
            raise ValueError("TUI editor height must be between 3 and 12")
        if not isinstance(self.steering_mode, str) or self.steering_mode not in {"all", "one-at-a-time"}:
            raise ValueError("steering-mode must be all or one-at-a-time")
        if any(type(value) is not bool for value in (self.tui_sidebar, self.auto_save_session, self.show_metrics)):
            raise ValueError("sidebar, auto-save-session and metrics must be booleans")
        for name in ("session_dir", "change_history_dir", "command_log_dir"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, (str, os.PathLike)) or len(str(value)) > 4096 or "\0" in str(value):
                    raise ValueError("storage directories must be paths")
                if name == "command_log_dir" and any(secret in str(value) for secret in self.credential_secrets):
                    raise ValueError("configured credentials cannot appear in command log storage paths")
                object.__setattr__(self, name, Path(value).expanduser().absolute())
        from .references import reference_paths
        selected = reference_paths(self.reference_files)
        if any(secret in path for path in selected for secret in self.secrets):
            raise ValueError("configured credentials cannot appear in file reference paths")
        object.__setattr__(self, "reference_files", selected)

    @property
    def credential_env_names(self):
        return tuple(dict.fromkeys((*self.credential_env_vars, *self._runtime_credentials.environment_names())))

    @property
    def credential_secrets(self):
        return tuple(dict.fromkeys(value for value in (self.api_key, *self.secret_values, *(value for _, value in self.headers),
                    *(value for server in self.mcp_servers for value in server.credential_values()),
                    *(value for server in self.lsp_servers for value in server.credential_values()),
                    *self._runtime_credentials.snapshot()) if value))

    @property
    def secrets(self):
        return (*self.credential_secrets, *(value for _, value in self.command_env if len(value) >= 8))

    def generation_options(self):
        """Only explicitly selected options; defaults leave the wire unchanged."""
        options = {} if self.extra_body == "{}" else json.loads(self.extra_body)
        if self.max_tokens is not None:
            options["max_output_tokens" if self.wire_api == "responses" else self.token_limit_field] = self.max_tokens
        elif self.wire_api == "messages":
            options["max_tokens"] = 4096  # Required by Messages; an output cap, not a billing cap.
        if self.thinking:
            if self.wire_api == "messages":
                if "thinking" in options:
                    raise ValueError("thinking and extra-body.thinking cannot both be configured")
                options["thinking"] = {"type": "adaptive"}
            elif self.wire_api == "responses":
                if "reasoning" in options:
                    raise ValueError("thinking and extra-body.reasoning cannot both be configured")
                options["reasoning"] = {"effort": self.reasoning_effort or "medium"}
            else:
                options["chat_template_kwargs"] = {"enable_thinking": True}
        for name in ("temperature", "top_p", "reasoning_effort", "seed"):
            if getattr(self, name) is not None:
                if name == "reasoning_effort" and self.wire_api == "responses":
                    options.setdefault("reasoning", {})["effort"] = self.reasoning_effort
                elif name == "reasoning_effort" and self.wire_api == "messages":
                    options.setdefault("output_config", {})["effort"] = self.reasoning_effort
                else:
                    options[name] = getattr(self, name)
        return options

    def tool_enabled(self, name):
        return (name in TOOL_NAMES and name not in self.disabled_tools
                and (name not in {"search", "grep"} or not {"search", "grep"}.intersection(self.disabled_tools))
                and (name not in {"edit_lines", "file_action"} or "edit_file" not in self.disabled_tools))

    def binding(self):
        binding = {"base_url": self.base_url, "model": self.model, "workdir": str(self.workdir)}
        if self.retry_attempts:
            binding["retry_attempts"] = self.retry_attempts
        if self.web_enabled:
            binding["web_enabled"] = True
        if self.background_enabled:
            binding["background"] = {"enabled": True, "timeout": self.background_timeout}
        if self.wire_api != "chat_completions":
            binding["wire_api"] = self.wire_api
        if self.command_output_limit is not None:
            binding["command_output_limit"] = self.command_output_limit
        if self.command_log_dir is not None:
            binding["command_log_dir"] = str(self.command_log_dir)
        if self.checks:
            binding["checks"] = [list(argv) for argv in self.checks]
        if self.check_before_task:
            binding["check_before_task"] = True
        if self.check_after_edit:
            binding["check_after_edit"] = True
        if self.read_only:
            binding["read_only"] = True
        if self.inherit_env:
            binding["inherit_env"] = True
        if not self.project_instructions:
            binding["project_instructions"] = False
        if self.instruction_files:
            binding["instruction_files"] = list(self.instruction_files)
        if self.skill_dirs:
            binding["skill_dirs"] = [str(path) for path in self.skill_dirs]
        if self.mcp_servers:
            binding["mcp_servers"] = [{k: v for k, v in server.public().items() if k != "missing_environment"} for server in self.mcp_servers]
        if self.lsp_servers:
            binding["lsp_servers"] = [{k: v for k, v in server.public().items() if k != "missing_environment"} for server in self.lsp_servers]
        if self.response_language is not None:
            binding["response_language"] = self.response_language
        if self.max_tokens is not None:
            binding["max_tokens"] = self.max_tokens
        if self.thinking:
            binding["thinking"] = True
        if self.preserve_reasoning:
            binding["preserve_reasoning"] = True
        if self.check_history_format != "tool_calls":
            binding["check_history_format"] = self.check_history_format
        for name in ("temperature", "top_p", "reasoning_effort", "seed"):
            if getattr(self, name) is not None:
                binding[name] = getattr(self, name)
        if self.extra_body != "{}":
            binding["extra_body"] = json.loads(self.extra_body)
        if self.token_limit_field != "max_tokens":
            binding["token_limit_field"] = self.token_limit_field
        for name in ("disabled_tools", "excluded_paths", "env_allowlist"):
            if getattr(self, name):
                binding[name] = list(getattr(self, name))
        if self.command_env:
            binding["command_env_names"] = sorted(name for name, _ in self.command_env)
        if self.headers:
            binding["header_names"] = sorted(name.lower() for name, _ in self.headers)
        if self.api_key_header != "Authorization" or self.api_key_scheme != "bearer":
            binding["authentication"] = {"header": self.api_key_header, "scheme": self.api_key_scheme}
        return binding
