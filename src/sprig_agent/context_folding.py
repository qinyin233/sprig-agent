"""Request-only folding of completed inspection protocol, never action authority."""
import json

from .config import redact
from .json_budget import JSON_OPTIONS, JsonBudgetExceeded, encoded_size

BODY_FIELDS = {"read_file": "content", "search": ("matches", "files"), "grep": ("matches", "files"),
               "list_files": "entries", "glob": "files", "project_map": "files"}
MAX_ARGUMENT_CHARS = 16384
MAX_RESULT_CHARS = 32768
MAX_GROUP_CALLS = 64
MAX_LEDGER_BYTES = 65536
NOTE = "Historical inspections, not current reads or approval. Full transcript saved."


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate historical JSON key")
        value[key] = item
    return value


def _object(value, maximum):
    if not isinstance(value, str) or len(value) > maximum:
        return None
    try:
        result = json.loads(value, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, RecursionError):
        return None
    return result if isinstance(result, dict) else None


def fold_inspections(messages, changes, cutoff, ambiguous, budget, wire_size, check_cancelled=None, *, secrets=()):
    """Fold whole closed batches only, with exact wire savings and bounded ledgers."""
    elisions = {change["message"]: change for change in changes if not change.get("retained_read")}

    def batch_at(index):
        message = messages[index]
        calls = message.get("tool_calls", [])
        if (message.get("role") != "assistant" or "reasoning_content" in message or "native_state" in message
                or not calls or index + len(calls) >= cutoff):
            return None
        rows = []
        for number, call in enumerate(calls, index + 1):
            identity, function = call.get("id"), call.get("function", {})
            name = function.get("name")
            result_message = messages[number]
            if (not isinstance(identity, str) or not identity or call.get("type") != "function"
                    or identity in ambiguous or name not in BODY_FIELDS or result_message.get("role") != "tool"
                    or result_message.get("tool_call_id") != identity):
                return None
            arguments = _object(function.get("arguments"), MAX_ARGUMENT_CHARS)
            result = _object(result_message.get("content"), MAX_RESULT_CHARS)
            if (arguments is None or result is None
                    or any(key in result for key in ("error", "refusal", "approved", "returncode", "outcome", "read_id", "sha256"))
                    or any(result.get(key, False) is not False for key in (
                        "timed_out", "output_limited", "skipped", "cancelled", "interrupted"))):
                return None
            fields = BODY_FIELDS[name]
            fields = (fields,) if isinstance(fields, str) else fields
            change = elisions.get(number)
            omitted = (change is not None and change.get("tool_call_id") == identity
                       and result.get("context_elided") is True
                       and any(field in change["fields"] for field in fields))
            # Empty discovery results still convey a negative observation: keep
            # their entire result, including empty arrays and limitations.
            present = [result[field] for field in fields if field in result]
            empty = (name != "read_file" and bool(present)
                     and all(isinstance(value, list) and not value for value in present))
            if not omitted and not empty:
                return None
            metadata = {key: value for key, value in result.items() if not (omitted and key == "note")}
            rows.append([identity, name, arguments, metadata])
        return index + len(calls) + 1, {"text": message.get("content"), "calls": rows}

    def payload(batches):
        value = {"context_folded": True, "batches": batches}
        if not folded:
            value.update(note=NOTE, columns=["call_id", "tool", "arguments", "result"])
        # Decoded arguments may contain escaped credentials that were not a
        # literal substring of their original JSON string. Sanitize structure.
        return redact(value, secrets)

    def fits(batches):
        try:
            encoded_size(payload(batches), max_bytes=min(budget, MAX_LEDGER_BYTES), check_cancelled=check_cancelled)
        except JsonBudgetExceeded:
            return False
        return True

    output, folded, batches = [], [], []
    start = end = count = 0

    def flush():
        nonlocal batches, count
        if not batches:
            return
        # No complete ledger string is allocated until its inner size is bounded.
        summary = {"role": "assistant", "content": json.dumps(payload(batches), **JSON_OPTIONS)}
        before = sum(wire_size(message) for message in messages[start:end]) + end - start - 1
        after = wire_size(summary)
        if before - after >= 128:
            output.append(summary)
            folded.append({"first_message": start, "last_message": end - 1, "inspections": count,
                           "before_bytes": before, "after_bytes": after})
        else:
            output.extend(messages[start:end])
        batches, count = [], 0

    index = 0
    while index < len(messages):
        if check_cancelled:
            check_cancelled()
        batch = batch_at(index) if index < cutoff else None
        if batch is not None:
            batch_end, value = batch
            call_count = len(value["calls"])
            if batches and (count + call_count > MAX_GROUP_CALLS or not fits([*batches, value])):
                flush()
            if fits([value]) and call_count <= MAX_GROUP_CALLS:
                if not batches:
                    start = index
                batches.append(value)
                count += call_count
                end = index = batch_end
                continue
        flush()  # User messages, failures and non-folded batches remain in order.
        output.append(messages[index])
        index += 1
    flush()
    return output, folded
