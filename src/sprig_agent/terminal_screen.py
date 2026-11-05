"""Bounded VT screen observation. Terminal output never becomes input."""
import re
import unicodedata

import pyte
from wcwidth import wcwidth

from .config import redact, terminal_text


ESC = '\x1b'
STRING_STARTS = {']', 'P', 'X', '^', '_'}
C1_STRINGS = {'\x9d': ']', '\x90': 'P', '\x98': 'X', '\x9e': '^', '\x9f': '_'}
STARTERS = {ESC, '\x9b', *C1_STRINGS}
CSI_PARAMS = re.compile(r'\??[0-9;]*\Z')
CSI_DISPLAY = frozenset('ABCDEFGH IJKLMPSTXZ`abdefgrmsu@'.replace(' ', ''))
PRIVATE_MODES = {1, 5, 6, 7, 25}
ALTERNATE_MODES = {47, 1047, 1049}
SIMPLE_ESCAPES = {'7', '8', 'D', 'E', 'H', 'M', 'c', '(0', '(B', ')0', ')B', '%G', '%@'}
MAX_SEQUENCE = 96
MAX_CELL_CHARS = 8


class _Screen(pyte.Screen):
    def draw(self, text):
        # A combining run must not accumulate an arbitrarily large cell.
        start = 0
        for index, char in enumerate(text):
            if char.isascii() or wcwidth(char) > 0:
                continue
            if index > start:
                super().draw(text[start:index])
            x, y = self.cursor.x, self.cursor.y
            if x:
                previous = self.buffer[y][x - 1]
            elif y:
                previous = self.buffer[y - 1][self.columns - 1]
            else:
                previous = None
            if unicodedata.combining(char) and previous is not None and len(previous.data) < MAX_CELL_CHARS:
                super().draw(char)
            start = index + 1
        if start < len(text):
            super().draw(text[start:])

    def visible_rows(self):
        for row in range(self.lines):
            cells, text, column = self.buffer[row], [], 0
            while column < self.columns:
                glyph = cells[column].data
                width = wcwidth(glyph[0]) if glyph else 0
                if width == 2:
                    following = cells[column + 1].data if column + 1 < self.columns else ' '
                    if not following or wcwidth(following[0]) == 0:
                        text.append(glyph)
                        column += 2
                        continue
                # Overwriting either half of a wide cell can leave an orphan
                # continuation slot in the emulator's sparse buffer.
                text.append(glyph if width == 1 else ' ')
                column += 1
            yield ''.join(text).rstrip()


class TerminalScreen:
    def __init__(self, columns, rows):
        self.main = _Screen(columns, rows)
        self.alternate = None
        self.stream = pyte.Stream(self.main)
        self.mode = 'text'
        self.pending = ''
        self.string_kind = None
        self.string_escape = False
        self.revision = 0
        self.failed = False

    @property
    def active(self):
        return self.alternate or self.main

    def resize(self, columns, rows):
        for screen in (self.main, self.alternate):
            if screen is not None:
                screen.resize(lines=rows, columns=columns)
                screen.cursor.x = min(screen.cursor.x, columns - 1)
                screen.cursor.y = min(screen.cursor.y, rows - 1)
        self.revision += 1

    def _alternate_mode(self, enabled):
        if enabled and self.alternate is None:
            self.alternate = _Screen(self.main.columns, self.main.lines)
        elif not enabled and self.alternate is not None:
            self.alternate = None
        else:
            return
        self.stream = pyte.Stream(self.active)

    def _csi(self, final):
        params = self.pending
        if not CSI_PARAMS.fullmatch(params):
            return
        private = params.startswith('?')
        fields = params.removeprefix('?').split(';')
        if len(fields) > 24 or any(len(value) > 4 for value in fields):
            return
        if final in 'hl':
            for field in fields:
                value = int(field or '0')
                if private and value in ALTERNATE_MODES:
                    self._alternate_mode(final == 'h')
                elif (private and value in PRIVATE_MODES) or (not private and value in {4, 20}):
                    self.stream.feed(ESC + '[' + ('?' if private else '') + field + final)
        elif not private and final in CSI_DISPLAY:
            if final != 'm':
                limit = max(self.active.columns, self.active.lines)
                params = ';'.join(str(min(int(field), limit)) if field else '' for field in fields)
            self.stream.feed(ESC + '[' + params + final)

    def _start_string(self, kind):
        self.mode = 'string'
        self.string_kind = kind
        self.string_escape = False

    def _consume(self, text):
        plain = []
        for char in text:
            if self.mode == 'text' and char not in STARTERS:
                if char in '\r\n\b\t\x0e\x0f' or not unicodedata.category(char).startswith('C'):
                    plain.append(char)
                continue
            if plain:
                self.stream.feed(''.join(plain))
                plain.clear()
            if self.mode == 'string':
                if (char in '\x18\x1a\x9c' or self.string_escape and char == '\\'
                        or self.string_kind == ']' and char == '\x07'):
                    self.mode = 'text'
                self.string_escape = char == ESC
            elif char == ESC:
                self.mode, self.pending = 'escape', ''
            elif char == '\x9b':
                self.mode, self.pending = 'csi', ''
            elif char in C1_STRINGS:
                self._start_string(C1_STRINGS[char])
            elif char in '\x18\x1a':
                self.mode, self.pending = 'text', ''
            elif self.mode == 'escape':
                if not self.pending and char == '[':
                    self.mode = 'csi'
                elif not self.pending and char in STRING_STARTS:
                    self._start_string(char)
                elif '\x20' <= char <= '\x2f':
                    if len(self.pending) < 4:
                        self.pending += char
                    else:
                        self.mode = 'discard_escape'
                else:
                    sequence = self.pending + char
                    if sequence in SIMPLE_ESCAPES:
                        if sequence == 'c':
                            self._alternate_mode(False)
                        self.stream.feed(ESC + sequence)
                    self.mode = 'text'
            elif self.mode in {'csi', 'discard_csi'}:
                if '\x40' <= char <= '\x7e':
                    if self.mode == 'csi':
                        self._csi(char)
                    self.mode = 'text'
                elif self.mode == 'csi' and len(self.pending) < MAX_SEQUENCE:
                    self.pending += char
                else:
                    self.mode = 'discard_csi'
            elif self.mode == 'discard_escape' and '\x30' <= char <= '\x7e':
                self.mode = 'text'
        if plain:
            self.stream.feed(''.join(plain))

    def feed(self, text):
        if self.failed:
            return
        try:
            self._consume(text)
        except (ValueError, IndexError, OverflowError):
            # Keep original bounded output available if a frame cannot be rendered.
            self.failed = True
        self.revision += 1

    def frame(self, secrets):
        screen = self.active
        lines = list(screen.visible_rows()) if not self.failed else []
        content = '\n'.join(lines).rstrip('\n')
        compact = ''.join(lines)
        safe = terminal_text(content)
        withheld = any(redact(text, secrets) != text for text in (content, compact, safe))
        return {'columns': screen.columns, 'rows': screen.lines, 'revision': self.revision,
                'cursor': {'column': min(screen.columns, screen.cursor.x + 1), 'row': screen.cursor.y + 1,
                           'visible': not screen.cursor.hidden},
                'alternate': self.alternate is not None, 'available': not self.failed,
                'withheld': withheld, 'content': '' if withheld else safe}
