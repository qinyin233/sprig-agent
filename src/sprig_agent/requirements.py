"""Explicit user requirement revisions projected from the saved conversation."""

import json
import re

from .json_budget import JsonBudgetExceeded, encoded_size

REQUIREMENT_KEY = "sprig_requirement"
USAGE = 'Use /requirements [OFFSET], /requirements show NAME, /requirements set NAME TEXT, or /requirements clear NAME'
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


def change(text):
    if not isinstance(text, str) or len(text) > 1088:
        raise ValueError(USAGE)
    pieces = text.split(None, 2)
    if len(pieces) >= 2 and NAME.fullmatch(pieces[1]):
        action, name = pieces[:2]
        if action == "clear" and len(pieces) == 2:
            return name, None
        if action == "set" and len(pieces) == 3:
            body = pieces[2].strip()
            if body and len(body) <= 1024 and len(body.encode("utf-8")) <= 1024 and "\x00" not in body:
                return name, body
    raise ValueError(USAGE + " (TEXT: 1–1024 UTF-8 bytes).")


def command(argument):
    if len(argument) > 1088:
        raise ValueError(USAGE)
    pieces = argument.split()
    if not pieces:
        return {"offset": 0}
    if len(pieces) == 1 and re.fullmatch(r"[0-9]{1,4}", pieces[0]):
        return {"offset": int(pieces[0])}
    if len(pieces) == 2 and pieces[0] == "show" and NAME.fullmatch(pieces[1]):
        return {"name": pieces[1]}
    name, body = change(argument)
    return {"change": f"clear {name}" if body is None else f"set {name} {body}"}


def state(messages):
    rows = {}
    for index, message in enumerate(messages or (), 1):
        if message.get(REQUIREMENT_KEY) is not True:
            continue
        name, text = change(message["content"])
        revision = rows[name]["revision"] + 1 if name in rows else 1
        rows[name] = {"name": name, "revision": revision, "source_message": index, "text": text}
    return rows


def display(messages, budget, *, offset=0, name=None):
    rows = state(messages)
    if name is not None:
        if name not in rows:
            raise ValueError("no such tracked requirement")
        result = rows[name]
        try:
            encoded_size(result, max_bytes=budget)
        except JsonBudgetExceeded:
            raise ValueError("requirement exceeds the display budget; use /history on its source message") from None
        return result
    if not 0 <= offset <= len(rows):
        raise ValueError("requirement offset is out of range")
    page = {"total": len(rows), "offset": offset, "next_offset": None, "items": []}
    for index, row in enumerate(list(rows.values())[offset:offset + 16], offset):
        entry = {key: value for key, value in row.items() if key != "text"}
        entry["active"] = row["text"] is not None
        candidate = {**page, "items": [*page["items"], entry],
                     "next_offset": index + 1 if index + 1 < len(rows) else None}
        try:
            encoded_size(candidate, max_bytes=budget)
        except JsonBudgetExceeded:
            if page["items"]:
                break
            raise ValueError("requirement index exceeds the display budget") from None
        page = candidate
    return page


def model_frame(rows):
    return (
        "Current tracked requirements supplied explicitly by the user. This list supersedes older records "
        "and summaries for the same names. A null text marks a withdrawn requirement. Recheck work affected "
        "by new revisions. Every write and command still requires fresh approval.\n"
        + json.dumps([{key: value for key, value in row.items() if key != "source_message"}
                      for row in rows.values()], ensure_ascii=False)
    )
