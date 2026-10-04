"""Bounded literal search helpers; no subprocesses or runtime downloads."""

from collections import deque
import fnmatch
import io


class FileGlob:
    """Match a basename, or workspace-relative components with whole-part **."""

    def __init__(self, pattern):
        if (not isinstance(pattern, str) or not pattern or len(pattern.encode("utf-8")) > 256
                or pattern.startswith(("/", "!")) or "\\" in pattern
                or any(ord(char) < 32 or ord(char) == 127 for char in pattern)):
            raise ValueError("glob must be a relative pattern of at most 256 UTF-8 bytes")
        self.parts = pattern.split("/")
        if len(self.parts) > 32 or any(part in {"", ".", ".."} for part in self.parts):
            raise ValueError("glob must have at most 32 nonempty components without . or ..")

    def matches(self, parts):
        if len(self.parts) == 1:
            return fnmatch.fnmatchcase(parts[-1], self.parts[0])
        possible = {0}
        for pattern in self.parts:
            if not possible:
                return False
            if pattern == "**":
                possible = set(range(min(possible), len(parts) + 1))
            else:
                possible = {index + 1 for index in possible
                            if index < len(parts) and fnmatch.fnmatchcase(parts[index], pattern)}
        return len(parts) in possible


def matching_lines(text, query, ignore_case, context, check_cancelled):
    """Preserve physical CR/LF lines with at most 10 previous/pending rows."""
    needle = query.casefold() if ignore_case else query
    if not context:
        # Ordinary searches do not allocate or maintain context queues.
        for number, line in enumerate(io.StringIO(text, newline=""), 1):
            check_cancelled()
            line = line.rstrip("\r\n")
            if needle in (line.casefold() if ignore_case else line):
                yield {"line": number, "text": line}
        return
    previous, pending = deque(maxlen=context), deque()
    for number, line in enumerate(io.StringIO(text, newline=""), 1):
        check_cancelled()
        line = line.rstrip("\r\n")
        for record in pending:
            record["after"].append(line)
        if pending and len(pending[0]["after"]) == context:
            yield pending.popleft()
        if needle in (line.casefold() if ignore_case else line):
            pending.append({"line": number, "text": line, "before": list(previous), "after": []})
        previous.append(line)
    for record in pending:
        check_cancelled()
        yield record
