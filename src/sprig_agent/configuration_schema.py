"""Editor assistance for the TOML configuration; runtime validation is authoritative."""

from .config import RESERVED_BODY_FIELDS, TOOL_NAMES, SHORTCUT_ACTIONS
from .local_commands import COMMANDS
from .prompt_templates import NAME_PATTERN


def configuration_schema():
    def obj(properties, **extra):
        return {"type": "object", "properties": properties, "additionalProperties": False, **extra}

    def integer(low, high):
        return {"type": "integer", "minimum": low, "maximum": high}

    def number(low, high, exclusive=False):
        return {"type": "number", "exclusiveMinimum" if exclusive else "minimum": low, "maximum": high}

    text = {"type": "string", "minLength": 1, "maxLength": 4096, "pattern": r"^[^\x00-\x1f]+$"}
    identifier = r"^[A-Za-z0-9_-]{1,64}$"
    environment = {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,127}$"}
    boolean = {"type": "boolean"}
    model_name = {**text, "maxLength": 200}
    checks = {"type": "array", "maxItems": 16, "items": {"type": "array", "minItems": 1, "maxItems": 256, "items": {"type": "string"}}}
    generation = obj({
        "max_tokens": integer(1, 65536), "thinking": boolean, "temperature": number(0, 2), "top_p": number(0, 1),
        "seed": integer(0, 2147483647), "reasoning_effort": {"enum": ["none", "minimal", "low", "medium", "high", "xhigh", "max"]},
        "token_limit_field": {"enum": ["max_tokens", "max_completion_tokens"]},
        "extra_body": {"type": "object", "maxProperties": 32, "propertyNames": {"not": {"enum": sorted(RESERVED_BODY_FIELDS)}},
                       "description": "Provider-specific JSON fields; <=16 KiB, depth <=12. Reserved request/security fields are rejected."},
    })
    mcp_common = {"enabled": boolean, "protocol_version": {"enum": ["2026-07-28", "2025-11-25", "2025-06-18"]},
                  "timeout": number(0, 120, True), "deadline": number(0, 3600, True)}
    mcp_http = obj({**mcp_common, "transport": {"const": "streamable_http"}, "url": text, "proxy": text,
                    "headers": {"type": "object", "maxProperties": 16, "additionalProperties": {**text, "writeOnly": True}},
                    "headers_env": {"type": "object", "maxProperties": 16, "additionalProperties": environment}}, required=["url"])
    mcp_stdio = obj({**mcp_common, "transport": {"const": "stdio"},
                     "command": {"type": "array", "minItems": 1, "maxItems": 256,
                                 "prefixItems": [{"type": "string", "minLength": 1, "maxLength": 65536, "pattern": r"^[^\x00]*$"}],
                                 "items": {"type": "string", "maxLength": 65536, "pattern": r"^[^\x00]*$"}}, "cwd": text,
                     "env": {"type": "object", "maxProperties": 32, "propertyNames": environment,
                             "additionalProperties": {"type": "string", "maxLength": 4096, "pattern": r"^[^\x00]*$", "writeOnly": True}},
                     "env_from": {"type": "object", "maxProperties": 32, "propertyNames": environment, "additionalProperties": environment}},
                    required=["transport", "command"])
    agent = obj({
        "max_steps": integer(1, 100), "timeout": number(0, 120, True), "max_output": integer(256, 1000000),
        "max_context_bytes": integer(4096, 2097152), "max_tokens": integer(1, 65536), "checks": checks,
        **{name: boolean for name in ("stream", "thinking", "read_only", "inherit_env", "compact_context", "request_usage", "project_instructions", "check_before_task", "check_after_edit")},
    })
    groups = {
        "editor": obj({"command": {"type": "array", "maxItems": 32,
                                   "items": {"type": "string", "maxLength": 4096},
                                   "description": "Trusted TUI editor argv; empty disables. Optional standalone {file}, otherwise append private draft path. No shell or environment expansion."},
                       "timeout": number(0, 3600, True)}),
        "agent": agent, "generation": generation,
        "tools": obj({name: boolean for name in TOOL_NAMES}),
        "files": obj({"exclude": {"type": "array", "maxItems": 256, "items": {**text, "maxLength": 256},
                                  "description": "Relative POSIX glob patterns; no !, .. components, backslashes or absolute paths. Adds to .sprigignore."}}),
        "execution": obj({"command_sandbox": {"enum": ["off", "bubblewrap"], "default": "off"},
                          "sandbox_read_dirs": {"type": "array", "maxItems": 8, "items": {**text, "maxLength": 4096}},
                          "inherit_env": boolean, "timeout": number(0, 120, True), "max_output": integer(256, 1000000),
                          "background": boolean, "background_timeout": number(0, 3600, True),
                          "output_limit": {**integer(0, 8388608), "not": {"minimum": 1, "maximum": 255}, "default": 1048576,
                                           "description": "Raw command output kill limit; defaults to 1 MiB. 0 restores stopping at max_output. Higher values use bounded redacted head/tail display."},
                          "env_allowlist": {"type": "array", "maxItems": 128, "items": environment},
                          "env": {"type": "object", "maxProperties": 32, "propertyNames": environment,
                                  "additionalProperties": {"type": "string", "maxLength": 8192}}}),
        "storage": obj({"session_dir": text, "change_history_dir": text, "command_log_dir": text, "auto_save_session": boolean,
                        "session_max_age_days": integer(1, 36500), "session_max_count": integer(1, 256)}),
        "context": obj({"max_bytes": integer(4096, 2097152), "compact": boolean,
                        "trigger_ratio": number(0, 1), "keep_recent_messages": integer(6, 1000),
                        "keep_read_bytes": integer(0, 262144),
                        "files": {"type": "array", "maxItems": 16, "items": text,
                                  "description": "Initial workspace-relative POSIX file references; <=8 KiB total paths. Runtime selections are saved separately. Small complete snapshots refresh before requests; larger files attach path/size only."}}),
        "instructions": obj({"files": {"type": "array", "maxItems": 16, "items": {**text, "maxLength": 256},
                                      "description": "Required workspace-relative UTF-8 files; no globs. Existing privacy and instruction limits apply."},
                             "response_language": {**text, "maxLength": 80}}),
        "skills": obj({"dirs": {"type": "array", "maxItems": 8, "items": text,
                                "description": "Explicit skill collection directories, relative to the declaring user/profile config. Project settings cannot add roots. No automatic home/workspace scan."}}),
        "mcp": obj({"servers": {"type": "object", "maxProperties": 8, "propertyNames": {"pattern": identifier}, "additionalProperties": {"oneOf": [mcp_http, mcp_stdio]}}}),
        "lsp": obj({"servers": {"type": "object", "maxProperties": 8, "propertyNames": {"pattern": identifier}, "additionalProperties": obj({
            "command": {"type": "array", "minItems": 1, "maxItems": 256, "items": {"type": "string"}},
            "languages": {"type": "object", "minProperties": 1, "maxProperties": 32, "propertyNames": {"pattern": r"^\.[A-Za-z0-9_+-]{1,16}$"}, "additionalProperties": {"type": "string", "pattern": r"^[A-Za-z0-9_+-]{1,64}$"}},
            "cwd": text, "enabled": boolean, "timeout": number(0,120,True), "deadline": number(0,3600,True),
            "env": {"type": "object", "maxProperties": 32, "propertyNames": environment, "additionalProperties": {"type": "string", "maxLength":4096}},
            "env_from": {"type": "object", "maxProperties":32, "propertyNames":environment, "additionalProperties":environment},
        }, required=["command", "languages"])}}),
        "web": obj({"enabled": {**boolean, "description": "Opt-in public HTTP(S) documents. Each GET needs approval; no proxies, auth, redirects, retries or persistent cache. Project configuration cannot enable it."}}),
        "prompts": {"type": "object", "maxProperties": 32,
                    "propertyNames": {"pattern": "^" + NAME_PATTERN + "$", "not": {"enum": sorted(COMMANDS)}},
                    "additionalProperties": obj({"template": {"type": "string", "minLength": 1, "maxLength": 16384,
                        "description": "Literal task text, at most 16 KiB UTF-8. $ARGUMENTS or contiguous $1..$9, $$ for a literal dollar. No shell, file or environment interpolation."},
                        "description": {"type": "string", "maxLength": 160}}, required=["template"]),
                    "description": "Trusted user/profile prompt library, at most 48 KiB UTF-8 total; project settings cannot define prompts."},
        "tui": obj({"theme": {"enum": ["textual-dark", "textual-light", "nord", "gruvbox", "dracula", "monokai", "tokyo-night", "solarized-dark", "solarized-light"]},
                    "sidebar": boolean, "editor_height": integer(3, 12), "steering_mode": {"enum": ["all", "one-at-a-time"]}, "metrics": boolean,
                    "keybindings": obj({action: {"type": "array", "maxItems": 4, "items": {"type": "string",
                        "pattern": r"^(?:(?:ctrl|alt|shift)\+)*(?:f(?:[1-9]|1[0-2])|[a-z0-9]|home|end|pageup|pagedown|up|down|left|right)$"}} for action in SHORTCUT_ACTIONS})}),
    }
    interface = {"enum": ["cli", "chat", "tui"]}
    model = obj({"id": model_name, "generation": generation, "max_context_bytes": integer(4096, 2097152)}, required=["id"])
    headers = {"type": "object", "maxProperties": 16, "propertyNames": {"type": "string", "pattern": r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$"}, "additionalProperties": text}
    provider = obj({
        "model": model_name, "base_url": text, "api_key": {**text, "writeOnly": True}, "api_key_env": environment,
        "request_timeout": number(0, 120, True), "request_deadline": number(0, 3600, True),
        "retry_attempts": integer(0, 5),
        "proxy": {**text, "description": "system, direct, or an HTTP(S) proxy URL without credentials."},
        "api_key_header": text, "api_key_scheme": {"enum": ["auto", "bearer", "raw"]}, "headers": headers,
        "wire_api": {"enum": ["chat_completions", "messages", "responses"]},
        "preserve_reasoning": boolean,
        "check_history_format": {"enum": ["tool_calls", "observations"]},
        "headers_env": {**headers, "additionalProperties": environment}, "generation": generation,
        "models": {"type": "object", "maxProperties": 64, "propertyNames": {"pattern": identifier}, "additionalProperties": model},
    }, **{"not": {"required": ["api_key", "api_key_env"]}})
    profile = obj({**groups, "provider": text, "model": model_name, "interface": interface})
    schema = obj({
        **groups, "schema_version": {"const": 1}, "provider": text, "profile": text, "interface": interface,
        "providers": {"type": "object", "maxProperties": 32, "propertyNames": {"pattern": identifier}, "additionalProperties": provider},
        "profiles": {"type": "object", "maxProperties": 32, "propertyNames": {"pattern": identifier}, "additionalProperties": profile},
    })
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", "title": "Sprig user configuration",
            "description": "Private TOML, schema version 1. Cross-field, endpoint, path and permission restrictions are additionally checked at runtime.", **schema}
