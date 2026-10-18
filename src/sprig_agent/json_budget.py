"""Count compact or indented UTF-8 JSON without a complete encoded allocation."""

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


def _sizes(value, indent=None, depth=0):
    if isinstance(value, str):
        yield from _string_sizes(value)
    elif isinstance(value, dict):
        yield 2
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield 1
            if indent is not None:
                yield 1 + indent * (depth + 1)  # Newline and item indentation.
            yield from _string_sizes(_key_text(key))
            yield 1 if indent is None else 2  # Colon and optional space.
            yield from _sizes(item, indent, depth + 1)
        if value and indent is not None:
            yield 1 + indent * depth  # Closing delimiter's newline/indentation.
    elif isinstance(value, (list, tuple)):
        yield 2
        for index, item in enumerate(value):
            if index:
                yield 1
            if indent is not None:
                yield 1 + indent * (depth + 1)
            yield from _sizes(item, indent, depth + 1)
        if value and indent is not None:
            yield 1 + indent * depth
    else:
        # Let the reference encoder validate scalar types and format numbers.
        yield len(json.dumps(value, **JSON_OPTIONS).encode("utf-8"))


def encoded_size(value, *, max_bytes=None, check_cancelled=None, indent=None):
    # None uses JSON_OPTIONS; an integer matches json.dumps(ensure_ascii=False,
    # indent=indent) with the standard indented separators, including indent=0.
    if indent is not None and (type(indent) is not int or indent < 0):
        raise ValueError("JSON indentation must be a nonnegative integer or None")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
        raise ValueError("JSON byte budget must be a nonnegative integer")
    if check_cancelled is not None:
        check_cancelled()
    total = 0
    for amount in _sizes(value, indent):
        if check_cancelled is not None:
            check_cancelled()
        total += amount
        if max_bytes is not None and total > max_bytes:
            raise JsonBudgetExceeded("context byte budget exceeded; request was not sent")
    return total
