"""Count compact UTF-8 JSON exactly, without allocating a complete encoding."""

import json

STRING_BLOCK = 4096
JSON_OPTIONS = {"ensure_ascii": False, "separators": (",", ":")}


class JsonBudgetExceeded(ValueError):
    """The serialized JSON cannot fit the caller's byte budget."""


def _string_sizes(value):
    yield 2  # Opening and closing quotes; each block has its own removed quotes.
    for start in range(0, len(value), STRING_BLOCK):
        yield len(json.dumps(value[start:start + STRING_BLOCK], **JSON_OPTIONS).encode("utf-8")) - 2


def _key_text(key):
    if isinstance(key, str):
        return key
    if key is None or isinstance(key, (bool, int, float)):
        return json.dumps(key, **JSON_OPTIONS)
    raise TypeError("JSON object keys must be strings or JSON scalars")


def _sizes(value):
    if isinstance(value, str):
        yield from _string_sizes(value)
    elif isinstance(value, dict):
        yield 2
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield 1
            yield from _string_sizes(_key_text(key))
            yield 1  # Colon.
            yield from _sizes(item)
    elif isinstance(value, (list, tuple)):
        yield 2
        for index, item in enumerate(value):
            if index:
                yield 1
            yield from _sizes(item)
    else:
        # Let the reference encoder validate scalar types and format numbers.
        yield len(json.dumps(value, **JSON_OPTIONS).encode("utf-8"))


def encoded_size(value, *, max_bytes=None, check_cancelled=None):
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("JSON byte budget must be a nonnegative integer")
    if check_cancelled is not None:
        check_cancelled()
    total = 0
    for amount in _sizes(value):
        if check_cancelled is not None:
            check_cancelled()
        total += amount
        if max_bytes is not None and total > max_bytes:
            raise JsonBudgetExceeded("context byte budget exceeded; request was not sent")
    return total
