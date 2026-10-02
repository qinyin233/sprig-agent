"""Incremental command bytes with UTF-8 decoding, key redaction and safe controls."""
import codecs
from .config import terminal_text
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
