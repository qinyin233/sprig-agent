"""Explicit composer selections. Typed labels never select attachments."""

import json

from .composer import TrackedDraft


class DraftFiles(TrackedDraft):
    def select(self, start, end, path):
        """Return a new draft with bounded metadata; caller installs both atomically."""
        if not 0 <= start <= end <= len(self.text):
            raise ValueError("file selection no longer matches the draft")
        if len(self.spans) >= 16 or sum(len(row[2].encode("utf-8")) for row in self.spans) + len(path.encode("utf-8")) > 8192:
            raise ValueError("每条消息最多选择 16 个文件，路径合计最多 8 KiB")
        token = "@" + json.dumps(path, ensure_ascii=False)
        candidate = DraftFiles(self.text)
        candidate.spans = list(self.spans)
        candidate.sync(self.text[:start] + token + self.text[end:], start, end)
        candidate.spans.append((start, start + len(token), path, token))
        candidate.spans.sort()
        return candidate

    def paths(self):
        return tuple(dict.fromkeys(row[2] for row in self.spans))


def mention_query(text, cursor):
    """Recognize an unfinished @word at a whitespace boundary, never emails."""
    start = text.rfind("@", 0, cursor)
    if start < 0 or (start and not text[start - 1].isspace()):
        return None
    query = text[start + 1:cursor]
    if len(query) > 256 or any(char.isspace() or char in '\"@' for char in query):
        return None
    return start, cursor, query
