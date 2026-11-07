"""Revision-pinned physical line replacements with explicit text or line arrays."""

import io


def line_ending(line):
    if line.endswith("\r\n"):
        return "\r\n"
    return line[-1:] if line.endswith(("\r", "\n")) else ""


def validate_edits(edits, maximum, check_cancelled):
    if not isinstance(edits, list) or not 1 <= len(edits) <= 32:
        raise ValueError("line_edits must contain between 1 and 32 replacements")
    size = 0
    for edit in edits:
        check_cancelled()
        if not isinstance(edit, dict) or edit.keys() not in (
                {"start_line", "end_line", "new"}, {"start_line", "end_line", "lines"}):
            raise ValueError("each line edit requires start_line, end_line and exactly one of new or lines")
        start, end = edit["start_line"], edit["end_line"]
        if (type(start) is not int or type(end) is not int or not 1 <= start <= 1000000
                or not start - 1 <= end <= 1000000):
            raise ValueError("invalid inclusive line range; insert with end_line=start_line-1")
        structured = "lines" in edit
        if structured and not isinstance(edit["lines"], list):
            raise ValueError("lines must be an array of strings")
        pieces = edit["lines"] if structured else (edit["new"],)
        for index, piece in enumerate(pieces, 1):
            if index % 256 == 0:
                check_cancelled()
            if not isinstance(piece, str):
                raise ValueError("new must be text and every lines entry must be a string")
            if structured and ("\r" in piece or "\n" in piece or "\0" in piece):
                raise ValueError("lines entries cannot contain CR, LF or NUL")
            # Count at least one byte for each generated terminator. The edit
            # backend checks the actual result, including any CRLF expansion.
            size += len(piece.encode("utf-8")) + (1 if structured else 0)
            if size > maximum:
                raise ValueError("combined line replacements exceed 512 KiB")


def replace_lines(text, edits, check_cancelled):
    wanted = {boundary for edit in edits for boundary in (edit["start_line"] - 1, edit["end_line"])}
    # UTF-8 BOM is a file marker, not part of a replaceable first line.
    marker = 1 if text.startswith("\ufeff") else 0
    position = marker
    boundaries = {0: position}
    use_source_endings = any(edit.get("lines") for edit in edits)
    endings = set()
    total = 0
    for total, line in enumerate(io.StringIO(text[position:], newline=""), 1):
        if total % 256 == 0:
            check_cancelled()
        position += len(line)
        if total in wanted:
            boundaries[total] = position
        if use_source_endings:
            ending = line_ending(line)
            if ending:
                endings.add(ending)
    check_cancelled()
    if not wanted <= boundaries.keys():
        raise ValueError(f"line edit is beyond the end of the file ({total} physical lines)")
    if len(endings) > 1:
        raise ValueError("mixed source line endings require literal new text instead of lines")
    ending = next(iter(endings), "\n")
    spans = []
    for edit in edits:
        if "new" in edit:
            replacement = edit["new"]
        else:
            lines = edit["lines"]
            replacement = ending.join(lines) + ending if lines else ""
        spans.append((boundaries[edit["start_line"] - 1], boundaries[edit["end_line"]], replacement))
    spans.sort()
    if any(left[1] > right[0] or left[0] == right[0] for left, right in zip(spans, spans[1:])):
        raise ValueError("line edits overlap or share an insertion boundary")
    chunks, end, written, last = [], 0, 0, ""
    for start, stop, new in spans:
        prefix = text[end:start]
        prefix_last = prefix[-1] if prefix else last
        prefix_size = written + len(prefix)
        if new and stop < len(text) and not new.endswith(("\r", "\n")):
            raise ValueError("whole-line replacement before following text requires a line ending in new; "
                             "provide it explicitly or use lines on a file with uniform line endings")
        if (new and start == stop == len(text) and prefix_size > marker and prefix_last not in "\r\n"
                and not new.startswith(("\r", "\n"))):
            raise ValueError("whole-line append after an unterminated line requires a leading line ending in new; or replace the final line explicitly")
        chunks.extend((prefix, new))
        written, last = prefix_size + len(new), new[-1] if new else prefix_last
        end = stop
    chunks.append(text[end:])
    return "".join(chunks)
