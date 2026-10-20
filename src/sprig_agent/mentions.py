"""Explicit composer selections. Typed labels never select attachments."""

import json


class DraftFiles:
    def __init__(self, text=""):
        self.text = text
        self.spans = []

    def sync(self, text, start=None, end=None):
        if start is None:
            if text == self.text:
                return
            start = 0
            while start < min(len(text), len(self.text)) and text[start] == self.text[start]:
                start += 1
            suffix = 0
            while (suffix < min(len(text), len(self.text)) - start
                   and text[len(text) - suffix - 1] == self.text[len(self.text) - suffix - 1]):
                suffix += 1
            end = len(self.text) - suffix
        shift = len(text) - len(self.text)
        selected = []
        for left, right, path, token in self.spans:
            if end <= left:
                left, right = left + shift, right + shift
            elif start < right:
                continue  # An edit touching a token revokes its selection.
            if text[left:right] == token:
                selected.append((left, right, path, token))
        self.text, self.spans = text, selected

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
