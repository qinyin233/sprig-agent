"""Incremental command bytes with UTF-8 decoding, key redaction and safe controls."""
import codecs
from .config import redact, terminal_text
from .streaming import TextOutput


class CommandOutput:
    def __init__(self, emit, secret):
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.output = TextOutput((lambda text: emit(terminal_text(text))) if emit else None, secret)

    def write(self, data):
        self.output.write(self.decoder.decode(data, final=False))

    def finish(self):
        self.output.write(self.decoder.decode(b"", final=True))
        self.output.finish()


class SampledCommandOutput:
    """Redact the complete bounded stream BEFORE sampling its head and tail."""

    marker = "\n[... command output omitted; showing final output ...]\n"

    def __init__(self, emit, secrets, limit):
        self.emit, self.limit = emit, limit
        # Generated display text is subject to the same privacy policy. Keep
        # the separator bounded even with unusual, overlapping secret values.
        self.marker = redact(self.marker, secrets)
        if len(self.marker.encode("utf-8")) > limit // 2:
            self.marker = "\n"
        self.head_limit = (limit - len(self.marker.encode("utf-8"))) // 2
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.preview_decoder = codecs.getincrementaldecoder("utf-8")()
        self.output = TextOutput(self._accept, secrets)
        self.head, self.tail = bytearray(), bytearray()
        self.raw_bytes = self.total = 0

    def _accept(self, text):
        data = text.encode("utf-8")
        self.total += len(data)
        prefix = data[:max(0, self.head_limit - len(self.head))]
        if prefix:
            self.head.extend(prefix)
            shown = self.preview_decoder.decode(prefix, final=False)
            if self.emit and shown:
                self.emit(terminal_text(shown))
        self.tail.extend(data)
        if len(self.tail) > self.limit:
            del self.tail[:-self.limit]

    def write(self, data):
        self.raw_bytes += len(data)
        self.output.write(self.decoder.decode(data, final=False))

    def finish(self):
        self.output.write(self.decoder.decode(b"", final=True))
        self.output.finish()
        head = self.head.decode("utf-8", errors="ignore")
        truncated = self.total > self.limit
        if truncated:
            space = self.limit - len(head.encode("utf-8")) - len(self.marker.encode("utf-8"))
            tail = self.tail[-space:].decode("utf-8", errors="ignore")
            text = head + self.marker + tail
            omitted = self.total - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
            remaining = self.marker + tail
        else:
            text = self.tail.decode("utf-8")
            omitted = 0
            remaining = text[len(head):]
        if self.emit and remaining:
            self.emit(terminal_text(remaining))
        return {"output": text, "output_bytes": self.raw_bytes,
                "output_truncated": truncated, "output_omitted_bytes": omitted}
