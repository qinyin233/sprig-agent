"""Bounded model-reported task progress; never approval or acceptance evidence."""

import copy
import json
import unicodedata

MAX_PLAN_BYTES = 4096
STATUSES = ("pending", "in_progress", "completed")


def validate_plan(plan, explanation=""):
    if not isinstance(plan, list) or len(plan) > 16:
        raise ValueError("plan must be an array of at most 16 steps")
    def text(value, limit, *, empty=False):
        if (not isinstance(value, str) or len(value) > limit or (not empty and not value.strip())
                or any(unicodedata.category(char).startswith("C") for char in value)):
            raise ValueError("plan descriptions must be bounded plain text without control characters")
        return value.strip()
    normalized, active = [], 0
    for item in plan:
        if not isinstance(item, dict) or set(item) != {"step", "status"}:
            raise ValueError("each plan step must contain only step and status")
        step = text(item["step"], 256)
        status = item["status"]
        if not isinstance(status, str) or status not in STATUSES:
            raise ValueError("plan status must be pending, in_progress or completed")
        active += status == "in_progress"
        normalized.append({"step": step, "status": status})
    if active > 1:
        raise ValueError("at most one plan step may be in_progress")
    result = {"plan_updated": True, "plan": normalized, "explanation": text(explanation, 512, empty=True)}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_PLAN_BYTES:
        raise ValueError("serialized plan exceeds 4 KiB; shorten the descriptions")
    return result


def plan_result(result):
    """Recognize complete successes only; partial, failed or elided data is ignored."""
    if (not isinstance(result, dict) or set(result) != {"plan_updated", "plan", "explanation"}
            or result.get("plan_updated") is not True):
        return None
    try:
        return validate_plan(result["plan"], result["explanation"])
    except (TypeError, ValueError):
        return None


class TaskPlan:
    def __init__(self):
        self._result = {"plan_updated": False, "plan": [], "explanation": ""}

    def snapshot(self):
        return copy.deepcopy(self._result)

    def update(self, result):
        validated = plan_result(result)
        if validated is None:
            raise ValueError("invalid complete plan result")
        self._result = validated

    def restore(self, messages):
        """Read completed plan results; never execute a saved tool intent."""
        restored = {"plan_updated": False, "plan": [], "explanation": ""}
        calls = {}
        for message in messages or []:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                entries = message.get("tool_calls", [])
                for call in entries if isinstance(entries, list) else []:
                    if (isinstance(call, dict) and isinstance(call.get("id"), str)
                            and isinstance(call.get("function"), dict)):
                        calls[call["id"]] = call["function"].get("name")
            elif (message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str)
                    and calls.pop(message["tool_call_id"], None) == "update_plan"):
                try:
                    result = plan_result(json.loads(message["content"]))
                except (TypeError, ValueError, RecursionError):
                    result = None
                if result is not None:
                    restored = result
        self._result = restored
