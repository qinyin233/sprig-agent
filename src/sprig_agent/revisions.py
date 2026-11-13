"""Short references to validated reads; content hashes remain the write guard."""

from collections import OrderedDict
import json
import re
import uuid

from .message_origin import user_request
from .search import search_window_rows

MAX_READ_VERSIONS = 128
READ_ID_PATTERN = r"^r[0-9a-f]{12}-[1-9][0-9]*$"
MAX_RESUME_READS = 8
MAX_RESUME_MESSAGES = 48
MAX_RESUME_RESULT_CHARS = 64 * 1024


def valid_read_id(value):
    return isinstance(value, str) and len(value) <= 64 and re.fullmatch(READ_ID_PATTERN, value) is not None


def _object(text, limit):
    if not isinstance(text, str) or len(text) > limit:
        return {}
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def _visible_reads(function, message, retained):
    name = function.get("name")
    if name not in {"read_file", "grep", "search"}:
        return
    arguments = _object(function.get("arguments"), 16384)
    path = arguments.get("path", "." if name != "read_file" else None)
    if not isinstance(path, str):
        return
    result = _object(message.get("content"), MAX_RESUME_RESULT_CHARS)
    if (any(key in result for key in ("error", "approved", "outcome", "returncode"))
            or any(result.get(flag) for flag in ("context_elided", "timed_out", "skipped", "interrupted", "output_limited"))):
        return
    mode = arguments.get("output_mode") if name != "read_file" else None
    if mode == "windows" and result.get("output_mode") == mode:
        rows = search_window_rows(arguments, result)
    elif name == "read_file" or (mode == "read" and result.get("output_mode") == mode):
        rows = [result]
    else:
        return
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("content"), str) or not row["content"]:
            continue
        source, digest, identity = (row.get(key) for key in ("path", "sha256", "read_id"))
        # Only this projection's retention report can identify a selected
        # historical snapshot whose former handle was deliberately removed.
        snapshot = (retained and row.get("context_retained") is True and "read_id" not in row)
        if (row.get("line_truncated") or row.get("context_elided") or row.get("window_omitted")
                or not isinstance(source, str) or not (snapshot or valid_read_id(identity))
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            continue
        if (row.get("range_complete") is not True if mode == "windows" else source != path):
            continue
        yield source, digest, identity


def recent_visible_reads(messages, check_cancelled, *, retained=()):
    """Yield source observations from closed tool groups in the current request."""
    tail = messages[-MAX_RESUME_MESSAGES:]
    for index in range(len(tail) - 1, -1, -1):
        check_cancelled()
        row = tail[index]
        if user_request(row):
            break
        calls = row.get("tool_calls", ()) if row.get("role") == "assistant" else ()
        if not calls:
            continue
        results = tail[index + 1:index + 1 + len(calls)]
        if len(results) != len(calls) or any(
                result.get("role") != "tool" or result.get("tool_call_id") != call["id"]
                for call, result in zip(calls, results)):
            continue
        for call, result in reversed(list(zip(calls, results))):
            yield from _visible_reads(call["function"], result, call["id"] in retained)


class ReadVersions:
    def __init__(self):
        self._entries = OrderedDict()
        self._prefix = "r" + uuid.uuid4().hex[:12] + "-"
        self._sequence = 0

    def clear(self):
        # Do not reuse identifiers after a new task, command or session reset.
        self._entries.clear()

    def discard(self, parts):
        for identity, (path, _) in list(self._entries.items()):
            if path == tuple(parts):
                del self._entries[identity]

    def forget(self, identity):
        self._entries.pop(identity, None)

    def snapshot(self):
        return tuple(self._entries.items())

    def restore_verified(self, entries):
        if not self._entries:
            self._entries.update(entries)

    def remember(self, parts, digest, *, validate=None):
        path = tuple(parts)
        for identity, recorded in list(self._entries.items()):
            if recorded == (path, digest):
                if validate is not None:
                    validate(identity)
                self._entries.move_to_end(identity)
                return identity
        identity = self._prefix + str(self._sequence + 1)
        if validate is not None:
            validate(identity)
        self._sequence += 1
        self._entries[identity] = (path, digest)
        if len(self._entries) > MAX_READ_VERSIONS:
            self._entries.popitem(last=False)
        return identity

    def resolve(self, identity, parts):
        value = self._entries.get(identity)
        if value is None or value[0] != tuple(parts):
            return None
        self._entries.move_to_end(identity)
        return value[1]
