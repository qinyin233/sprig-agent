"""Revision-pinned physical line replacements; never guess or normalize text."""

import io


def validate_edits(edits, maximum, check_cancelled):
    if not isinstance(edits, list) or not 1 <= len(edits) <= 32:
        raise ValueError("line_edits must contain between 1 and 32 replacements")
    size = 0
    for edit in edits:
        check_cancelled()
        if not isinstance(edit, dict) or set(edit) != {"start_line", "end_line", "new"}:
            raise ValueError("each line edit requires only start_line, end_line and new")
        start, end, new = edit["start_line"], edit["end_line"], edit["new"]
        if (type(start) is not int or type(end) is not int or not 1 <= start <= 1000000
                or not start - 1 <= end <= 1000000 or not isinstance(new, str)):
            raise ValueError("invalid inclusive line range; insert with end_line=start_line-1")
        size += len(new.encode("utf-8"))
        if size > maximum:
            raise ValueError("combined line replacements exceed 512 KiB")


def replace_lines(text, edits, check_cancelled):
    wanted = {boundary for edit in edits for boundary in (edit["start_line"] - 1, edit["end_line"])}
    # UTF-8 BOM is a file marker, not part of a replaceable first line.
    position = 1 if text.startswith("\ufeff") else 0
    boundaries = {0: position}
    total = 0
    for total, line in enumerate(io.StringIO(text[position:], newline=""), 1):
        if total % 256 == 0:
            check_cancelled()
        position += len(line)
        if total in wanted:
            boundaries[total] = position
    check_cancelled()
    if not wanted <= boundaries.keys():
        raise ValueError(f"line edit is beyond the end of the file ({total} physical lines)")
    spans = sorted((boundaries[edit["start_line"] - 1], boundaries[edit["end_line"]], edit["new"]) for edit in edits)
    if any(left[1] > right[0] or left[0] == right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError("line edits overlap or share an insertion boundary")
    chunks, end, written, last = [], 0, 0, ""
    marker = 1 if text.startswith("\ufeff") else 0
    for start, stop, new in spans:
        prefix = text[end:start]
        prefix_last = prefix[-1] if prefix else last
        prefix_size = written + len(prefix)
        if new and stop < len(text) and not new.endswith(("\r", "\n")):
            raise ValueError("whole-line replacement before following text requires a line ending (LF, CRLF or CR) in new; no change made")
        if (new and start == stop == len(text) and prefix_size > marker and prefix_last not in "\r\n"
                and not new.startswith(("\r", "\n"))):
            raise ValueError("whole-line append after an unterminated line requires a leading line ending in new; or replace the final line explicitly")
        chunks.extend((prefix, new))
        written, last = prefix_size + len(new), new[-1] if new else prefix_last
        end = stop
    chunks.append(text[end:])
    return "".join(chunks)
