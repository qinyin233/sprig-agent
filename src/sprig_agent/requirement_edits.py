"""User-selected edit scopes shared by the transcript and exact backup journal."""

import re

from .requirements import NAME, REQUIREMENT_KEY, change

EDIT_SCOPE = "sprig_edit_scope"
SCOPE_ID = re.compile(r"[a-f0-9]{32}")


def validate_scope(value):
    if (not isinstance(value, dict) or set(value) != {"id", "name", "revision", "source"}
            or not isinstance(value["id"], str) or not SCOPE_ID.fullmatch(value["id"])
            or not isinstance(value["name"], str) or not NAME.fullmatch(value["name"])
            or type(value["source"]) is not int or not 2 <= value["source"] <= 5000
            or type(value["revision"]) is not int or not 1 <= value["revision"] < value["source"]):
        raise ValueError("invalid tracked edit scope")
    return value


def scoped_tasks(messages, name=None):
    """Find the named requirement's scopes only in this conversation or its inherited prefix."""
    selected, requirements, seen = {}, {}, set()
    for index, row in enumerate(messages or (), 1):
        if row.get(REQUIREMENT_KEY) is True:
            requirement, body = change(row["content"])
            previous = requirements.get(requirement)
            requirements[requirement] = (previous[0] + 1 if previous else 1, index, body is not None)
        if EDIT_SCOPE not in row:
            continue
        scope = validate_scope(row[EDIT_SCOPE])
        if (row.get("role") != "user" or scope["id"] in seen
                or requirements.get(scope["name"]) != (scope["revision"], scope["source"], True)):
            raise ValueError("edit scope does not match the active requirement at submission")
        seen.add(scope["id"])
        if scope["name"] == name:
            selected[scope["id"]] = (scope, index)
    if name is not None and name not in requirements:
        raise ValueError("no such tracked requirement")
    return selected
