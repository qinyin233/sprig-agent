"""Bounded literal/RE2 search helpers; no subprocesses or runtime downloads."""

from collections import deque
import fnmatch
import io
import time

REGEX_MEMORY = 1024 * 1024
REGEX_SECONDS = 1.0
MAX_READ_RANGES = 8
MAX_RANGE_LINES = 10000


class RegexBudgetExceeded(Exception):
    """Stop the entire search, preserving only already complete match records."""


class RegexMatcher:
    """One uncached non-backtracking pattern per search, checked between lines."""

    def __init__(self, query, ignore_case, check_cancelled):
        import re2

        self.check_cancelled = check_cancelled
        check_cancelled()
        started = time.perf_counter()
        options = re2.Options()
        options.max_mem = REGEX_MEMORY
        options.log_errors = False  # Native parse errors can otherwise print the query.
        options.never_capture = True  # Only line membership is needed, not captured groups.
        options.case_sensitive = not ignore_case
        try:
            # Use the locked binding's native boolean operation directly: public
            # compile() caches 128 patterns, and search() builds unused captures
            # and Unicode offsets. Keep pattern/native caches call-local instead.
            self.pattern = re2._re2.RE2(query.encode("utf-8"), options)
            if not self.pattern.ok():
                raise ValueError("invalid, unsupported, or too complex RE2 pattern")
        except (re2.error, UnicodeError):
            raise ValueError("invalid, unsupported, or too complex RE2 pattern") from None
        finally:
            check_cancelled()
        self.elapsed = time.perf_counter() - started
        self.anchor = re2._re2.RE2.Anchor.UNANCHORED

    def _search(self, line):
        encoded = line.encode("utf-8")
        return self.pattern.Match(self.anchor, encoded, 0, len(encoded))[0] != (-1, -1)

    def __call__(self, line):
        self.check_cancelled()
        if self.elapsed >= REGEX_SECONDS:
            raise RegexBudgetExceeded
        started = time.perf_counter()
        try:
            found = self._search(line)
        finally:
            self.elapsed += time.perf_counter() - started
            self.check_cancelled()
        # This is a cooperative work budget, not preemption of a native call.
        # RE2 also bounds pattern resources; Tools bounds the full input file.
        if self.elapsed >= REGEX_SECONDS:
            raise RegexBudgetExceeded
        return found


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

    def could_descend(self, parts):
        """Whether a directory prefix can contain a future matching file."""
        if len(self.parts) == 1:
            return True  # Basename patterns apply at any depth.
        possible = {0}
        for pattern in self.parts:
            if not possible:
                return False
            if len(parts) in possible:
                return True  # Remaining pattern parts can consume a file suffix.
            if pattern == "**":
                possible = set(range(min(possible), len(parts) + 1))
            else:
                possible = {index + 1 for index in possible
                            if index < len(parts) and fnmatch.fnmatchcase(parts[index], pattern)}
        return self.parts[-1] == "**" and len(parts) in possible


def matching_lines(text, query, ignore_case, context, check_cancelled, matcher=None, *, start_line=1):
    """Preserve physical CR/LF lines with at most 10 previous/pending rows."""
    needle = query.casefold() if ignore_case else query
    if not context:
        # Ordinary searches do not allocate or maintain context queues.
        for number, line in enumerate(io.StringIO(text, newline=""), 1):
            check_cancelled()
            if number < start_line:
                continue
            line = line.rstrip("\r\n")
            if (matcher(line) if matcher is not None else
                    needle in (line.casefold() if ignore_case else line)):
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
        if number >= start_line and (matcher(line) if matcher is not None else
                                     needle in (line.casefold() if ignore_case else line)):
            pending.append({"line": number, "text": line, "before": list(previous), "after": []})
        previous.append(line)
    for record in pending:
        check_cancelled()
        yield record


def match_window_prefixes(matches, limit):
    """Record bounded prefixes and the first omitted match's physical line."""
    ranges, prefixes, lines = [], [], 0
    for match in matches:
        number = match["line"]
        start = number - len(match.get("before", ()))
        end = number + len(match.get("after", ()))
        adjacent = bool(ranges) and start <= ranges[-1][1] + 1
        added = max(0, end - ranges[-1][1]) if adjacent else end - start + 1
        if (len(prefixes) == limit or lines + added > MAX_RANGE_LINES
                or (not adjacent and len(ranges) == MAX_READ_RANGES)):
            return prefixes, number
        if adjacent:
            ranges = [*ranges[:-1], [ranges[-1][0], max(ranges[-1][1], end)]]
        else:
            ranges = [*ranges, [start, end]]
        lines += added
        prefixes.append((number, ranges))
    return prefixes, None
