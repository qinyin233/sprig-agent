"""Request byte budgets and explicit, non-destructive history elision."""
import json
from .config import redact


def request_payload(config, messages, tools):
    payload = {"model": config.model, "messages": redact(messages, config.api_key), "tools": tools, "tool_choice": "auto"}
    if config.stream:
        payload["stream"] = True
        if config.request_usage:
            payload["stream_options"] = {"include_usage": True}
    return payload


def request_body(config, messages, tools):
    return json.dumps(request_payload(config, messages, tools)).encode("utf-8")


def _result(content):
    try:
        value = json.loads(content)
    except (ValueError, TypeError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _successful(result):
    return (result is not None and "error" not in result and result.get("approved") is not False
            and not any(result.get(flag) for flag in (
                "timed_out", "output_limited", "skipped", "cancelled", "interrupted", "outcome"))
            and result.get("returncode", 0) == 0)


def _edit_path(arguments):
    value = _result(arguments)
    if value is None or not isinstance(value.get("path"), str) or not value["path"]:
        return None
    if set(value) == {"path", "old", "new"}:
        if not all(isinstance(value[key], str) for key in ("old", "new")):
            return None
    elif set(value) == {"path", "edits"}:
        edits = value["edits"]
        if (not isinstance(edits, list) or not 1 <= len(edits) <= 32
                or any(not isinstance(edit, dict) or set(edit) != {"old", "new"}
                       or not all(isinstance(edit[key], str) for key in ("old", "new")) for edit in edits)):
            return None
    else:
        return None
    return value["path"]


def prepare_context(config, messages, tools, compact=False):
    """Keep the saved transcript intact; never discard user instructions or refusals."""
    selected = redact(messages, config.api_key)
    before = size = len(request_body(config, selected, tools))
    changes = []
    def wire_size(value):
        return len(json.dumps(redact(value, config.api_key)).encode("utf-8"))
    def replace_string(container, field, value, index, identity, fields):
        old_bytes, new_bytes = wire_size(container[field]), wire_size(value)
        if old_bytes - new_bytes < 128:
            return
        container[field] = value
        changes.append({"message": index, "tool_call_id": identity, "fields": fields,
                        "before_bytes": old_bytes, "after_bytes": new_bytes})
    if compact:
        cutoff = max(0, len(selected) - 6)
        calls, results, ambiguous = {}, {}, set()
        # Correlate each historical intent with one completed result. Duplicate
        # IDs and a result in the protected tail must never permit elision.
        for index, message in enumerate(selected):
            if message.get("role") == "assistant":
                for call in message.get("tool_calls", []):
                    identity = call.get("id")
                    if identity in calls:
                        ambiguous.add(identity)
                    calls[identity] = (index, call)
            elif message.get("role") == "tool":
                identity = message.get("tool_call_id")
                if identity in results:
                    ambiguous.add(identity)
                results[identity] = (index, message.get("content"))
        for identity, (index, call) in calls.items():
            result_index, content = results.get(identity, (len(selected), None))
            function = call.get("function", {})
            if (identity in ambiguous or not index < result_index < cutoff
                    or function.get("name") != "edit_file"):
                continue
            result = _result(content)
            if (not _successful(result) or result.get("approved") is not True
                    or result.get("changed") is not True):
                continue
            path = _edit_path(function.get("arguments"))
            if path is None or result.get("path") != path:
                continue
            marker = json.dumps({"path": path, "context_elided": True,
                "note": "Completed approved historical edit: replacement text omitted from this request. This is not runnable tool input. Inspect current files before further edits; the saved transcript retains the original arguments."}, ensure_ascii=False)
            replace_string(function, "arguments", marker, index, identity, ["arguments"])
        for index, message in enumerate(selected[:-6]):
            if message.get("role") != "tool":
                continue
            result = _result(message.get("content"))
            if not _successful(result):
                continue
            fields = [name for name in ("content", "output", "files", "entries", "matches") if name in result]
            if not fields:
                continue
            reduced = {key: value for key, value in result.items() if key not in fields}
            reduced.update(context_elided=True, note="Older successful tool output omitted from the request. Inspect current files again if needed; the saved transcript retains the full result.")
            content = json.dumps(reduced, ensure_ascii=False)
            replace_string(message, "content", content, index, message["tool_call_id"], fields)
        # Count the actual serialized request, including redaction of new markers.
        if changes:
            size = len(request_body(config, selected, tools))
    return selected, {"before_bytes": before, "after_bytes": size, "budget_bytes": config.max_context_bytes,
                      "within_budget": size <= config.max_context_bytes, "changes": changes}
