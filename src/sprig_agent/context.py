"""Request byte budgets and explicit, deterministic tool-output elision."""
import json
from .config import redact


def request_payload(config, messages, tools):
    payload = {"model": config.model, "messages": redact(messages, config.api_key), "tools": tools, "tool_choice": "auto"}
    if config.stream:
        payload["stream"] = True
    return payload


def request_body(config, messages, tools):
    return json.dumps(request_payload(config, messages, tools)).encode("utf-8")


def prepare_context(config, messages, tools, compact=False):
    """Keep the saved transcript intact; never discard user instructions or refusals."""
    selected = redact(messages, config.api_key)
    before = size = len(request_body(config, selected, tools))
    changes = []
    if compact:
        for index, message in enumerate(selected[:-6]):
            if message.get("role") != "tool":
                continue
            try:
                result = json.loads(message["content"])
            except (ValueError, TypeError, RecursionError):
                continue
            if (not isinstance(result, dict) or "error" in result or result.get("approved") is False
                    or result.get("timed_out") or result.get("output_limited")
                    or result.get("returncode", 0) != 0):
                continue
            fields = [name for name in ("content", "output", "files", "entries", "matches") if name in result]
            if not fields:
                continue
            reduced = {key: value for key, value in result.items() if key not in fields}
            reduced.update(context_elided=True, note="Older successful tool output omitted from the request. Inspect current files again if needed; the saved transcript retains the full result.")
            content = json.dumps(reduced, ensure_ascii=False)
            old_bytes = len(json.dumps(message["content"]).encode("utf-8"))
            new_bytes = len(json.dumps(content).encode("utf-8"))
            if old_bytes - new_bytes < 128:
                continue
            message["content"] = content
            size -= old_bytes - new_bytes
            changes.append({"message": index, "tool_call_id": message["tool_call_id"], "fields": fields,
                            "before_bytes": old_bytes, "after_bytes": new_bytes})
    return selected, {"before_bytes": before, "after_bytes": size, "budget_bytes": config.max_context_bytes,
                      "within_budget": size <= config.max_context_bytes, "changes": changes}
