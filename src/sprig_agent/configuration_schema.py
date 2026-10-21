"""Editor assistance for the TOML configuration; runtime validation is authoritative."""

from .config import RESERVED_BODY_FIELDS, TOOL_NAMES, SHORTCUT_ACTIONS


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
    agent = obj({
        "max_steps": integer(1, 100), "timeout": number(0, 120, True), "max_output": integer(256, 1000000),
        "max_context_bytes": integer(4096, 2097152), "max_tokens": integer(1, 65536), "checks": checks,
        **{name: boolean for name in ("stream", "thinking", "read_only", "inherit_env", "compact_context", "request_usage", "project_instructions", "check_before_task", "check_after_edit")},
    })
    groups = {
        "agent": agent, "generation": generation,
        "tools": obj({name: boolean for name in TOOL_NAMES}),
        "files": obj({"exclude": {"type": "array", "maxItems": 256, "items": {**text, "maxLength": 256},
                                  "description": "Relative POSIX glob patterns; no !, .. components, backslashes or absolute paths. Adds to .sprigignore."}}),
        "execution": obj({"inherit_env": boolean, "timeout": number(0, 120, True), "max_output": integer(256, 1000000),
                          "output_limit": {**integer(256, 8388608), "description": "Raw command output kill limit; defaults to max_output. Higher values use bounded redacted head/tail display."},
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
