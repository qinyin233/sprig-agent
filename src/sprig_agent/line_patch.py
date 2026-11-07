"""Strict single-file unified hunks, translated to revision-pinned line edits."""
import io
import re

from .line_edits import line_ending, replace_lines

HEADER = re.compile(r"@@ -([0-9]{1,7})(?:,([0-9]{1,7}))? \+([0-9]{1,7})(?:,([0-9]{1,7}))? @@(?: .*)?")
NO_NEWLINE = "\\ No newline at end of file"


def apply(text, patch, maximum, check):
    check()
    if not isinstance(patch, str) or not patch or len(patch) > maximum or len(patch.encode('utf-8')) > maximum:
        raise ValueError('patch must be nonempty UTF-8 unified hunks within 512 KiB')
    marker = 1 if text.startswith('\ufeff') else 0
    source = []
    endings = set()
    for index, line in enumerate(io.StringIO(text[marker:], newline='')):
        if index % 256 == 0:
            check()
        ending = line_ending(line)
        source.append((line[:-len(ending)] if ending else line, ending))
        if ending:
            endings.add(ending)
    # The patch's line separators are transport framing, not file EOL changes.
    lines = patch.split('\n')
    if lines[-1] == '':
        lines.pop()
    lines = [line[:-1] if line.endswith('\r') else line for line in lines]
    edits, cursor, delta, previous_end, previous_start = [], 0, 0, 0, -1
    while cursor < len(lines):
        check()
        header = HEADER.fullmatch(lines[cursor])
        if header is None or len(edits) == 32:
            raise ValueError('patch requires 1-32 numbered @@ hunks; no file headers, fences or routing directives')
        old_start, old_count, new_start, new_count = [int(value) if value is not None else 1 for value in header.groups()]
        if (max(old_start, old_count, new_start, new_count) > 1000000
                or (old_count and not old_start) or (new_count and not new_start)):
            raise ValueError('invalid patch line coordinates')
        start = old_start - bool(old_count)
        target = new_start - bool(new_count)
        if (start < previous_end or start == previous_start or start + old_count > len(source)
                or target != start + delta):
            raise ValueError('patch coordinates overlap, exceed the file, or disagree with preceding hunks')
        cursor += 1
        old_seen, new_seen, replacement = 0, 0, []
        while cursor < len(lines) and not lines[cursor].startswith('@@'):
            if cursor % 256 == 0:
                check()
            line = lines[cursor]
            if not line or line[0] not in ' +-':
                raise ValueError('each patch line requires a space, + or - prefix')
            kind, body = line[0], line[1:]
            cursor += 1
            unterminated = cursor < len(lines) and lines[cursor] == NO_NEWLINE
            if unterminated:
                cursor += 1
            if kind != '+':
                position = start + old_seen
                if old_seen >= old_count or position >= len(source):
                    raise ValueError('patch old-line count does not match its hunk')
                original, ending = source[position]
                if original != body or bool(ending) == unterminated:
                    raise ValueError(f'patch context/deletion mismatch at physical line {position + 1}; reread, no fuzzy matching')
                old_seen += 1
            if kind != '-':
                new_seen += 1
                if new_seen > new_count:
                    raise ValueError('patch new-line count does not match its hunk')
                if kind == '+':
                    if len(endings) > 1 and not unterminated:
                        raise ValueError('mixed source line endings require explicit edits instead of patch additions')
                    ending = '' if unterminated else next(iter(endings), '\n')
                if replacement and not replacement[-1].endswith(('\r', '\n')):
                    raise ValueError('unterminated patch line must be the final output line')
                replacement.append(body + ending)
        if old_seen != old_count or new_seen != new_count or not (old_count or new_count):
            raise ValueError('patch hunk counts must match complete payload lines')
        edits.append({'start_line': start + 1, 'end_line': start + old_count, 'new': ''.join(replacement)})
        previous_start, previous_end = start, start + old_count
        delta += new_count - old_count
    check()
    return replace_lines(text, edits, check)
