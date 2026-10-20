"""Bounded composer spans. Display placeholders never stand in for request size."""

MAX_PASTE_BYTES = 2 * 1024 * 1024


class TrackedDraft:
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
        for left, right, value, token in self.spans:
            if end <= left:
                left, right = left + shift, right + shift
            elif start < right:
                continue
            if text[left:right] == token:
                selected.append((left, right, value, token))
        self.text, self.spans = text, selected


def paste_text(text):
    if len(text) > MAX_PASTE_BYTES:
        raise ValueError("粘贴超过 2 MiB，未插入；请保存到文件后按范围读取")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) < 160 for char in text):
        raise ValueError("粘贴含终端控制字符，未插入；请先转换为可见文本")
    try:
        if len(text.encode("utf-8")) > MAX_PASTE_BYTES:
            raise ValueError("粘贴超过 2 MiB，未插入；请保存到文件后按范围读取")
    except UnicodeEncodeError:
        raise ValueError("粘贴包含无效 Unicode，未插入") from None
    return text


class DraftPastes(TrackedDraft):
    def expanded(self):
        parts, start = [], 0
        for left, right, body, _ in self.spans:
            parts.extend((self.text[start:left], body))
            start = right
        parts.append(self.text[start:])
        return "".join(parts)

    def fold(self, start, end, body, identity):
        if not 0 <= start <= end <= len(self.text):
            raise ValueError("粘贴位置已变化，请重新粘贴")
        token = f"[粘贴 #{identity} · {body.count(chr(10)) + 1} 行 · {len(body.encode('utf-8'))} B · F4 展开]"
        candidate = DraftPastes(self.text)
        candidate.spans = list(self.spans)
        candidate.sync(self.text[:start] + token + self.text[end:], start, end)
        if len(candidate.spans) >= 16:
            raise ValueError("草稿最多折叠 16 段粘贴；请展开或删除后继续")
        candidate.spans.append((start, start + len(token), body, token))
        candidate.spans.sort()
        if len(candidate.expanded().encode("utf-8")) > MAX_PASTE_BYTES:
            raise ValueError("展开后的草稿超过 2 MiB，粘贴未插入")
        return candidate, token
