"""Bounded in-memory symbol parsing; callers still read and validate fresh files."""

import ast
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json

from .syntax import IndexUnavailable, MAX_NAME_BYTES, MAX_NODES, MAX_SYMBOLS, SyntaxParser, language_for

MAX_CACHE_ENTRIES = 128
MAX_CACHE_BYTES = 1024 * 1024


class SymbolIndex:
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0
        self.syntax = SyntaxParser()
        self._scan = None

    def clear(self):
        self.entries.clear()
        self.bytes = 0
        self.syntax.clear()

    def python(self, text):
        return self.symbols(text, "python")

    @contextmanager
    def scan(self, complete=lambda: True):
        """Keep prior residents through a sweep larger than the cache capacity."""
        if self._scan is not None:
            yield
            return
        protected, used = set(self.entries), set()
        self._scan = (protected, used)
        try:
            yield
        except BaseException:
            # An interrupted/failed sweep cannot establish which entries are
            # unused. Completed individual parses remain safe cached results.
            raise
        else:
            if complete():
                for key in protected - used:
                    entry = self.entries.pop(key, None)
                    if entry is not None:
                        self.bytes -= entry[1]
        finally:
            self._scan = None

    def _retain(self, key, encoded, size):
        if size > MAX_CACHE_BYTES:
            return
        protected = self._scan[0] if self._scan is not None else set()
        while self.entries and (len(self.entries) >= MAX_CACHE_ENTRIES or self.bytes + size > MAX_CACHE_BYTES):
            victim = next((entry for entry in self.entries if entry not in protected), None)
            if victim is None:
                return  # Parse and return the result, without evicting this sweep's residents.
            self.bytes -= self.entries.pop(victim)[1]
        self.entries[key] = (encoded, size)
        self.bytes += size

    def symbols(self, text, language, check_cancelled=lambda: None, *, spans=False):
        check_cancelled()
        # Content, not mtime, identifies a parse. Never cache raw source or ASTs.
        key = language.encode("ascii") + (b"\0spans\0" if spans else b"\0") + hashlib.sha256(text.encode("utf-8")).digest()
        if self._scan is not None and key in self._scan[0]:
            self._scan[1].add(key)
        if key in self.entries:
            encoded, size = self.entries.pop(key)
            self.entries[key] = (encoded, size)
            return json.loads(encoded)
        else:
            if language == "python":
                data = self._python(text, check_cancelled, spans=True) if spans else self._python(text, check_cancelled)
            else:
                data = (self.syntax.parse(text, language, check_cancelled, spans=True) if spans
                        else self.syntax.parse(text, language, check_cancelled))
            check_cancelled()
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            size = len(encoded.encode("utf-8")) + len(key)
            self._retain(key, encoded, size)
        # Cached JSON is immutable; returned dictionaries never share storage.
        return data

    @staticmethod
    def _python(text, check_cancelled, *, spans=False):
        try:
            tree = ast.parse(text.removeprefix("\ufeff"))
            symbols, pending, visited = [], [(tree.body, "")], 0
            while pending:
                check_cancelled()
                nodes, parent = pending.pop()
                for node in nodes:
                    check_cancelled()
                    visited += 1
                    if visited > MAX_NODES:
                        raise IndexUnavailable("symbol traversal exceeds index limit")
                    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        # Check before concatenating/copying a long parent for
                        # every method. AST identifiers are normalized Unicode;
                        # the final check therefore uses their actual UTF-8.
                        if len(parent) + len(node.name) > MAX_NAME_BYTES:
                            raise IndexUnavailable("qualified declaration name exceeds index limit")
                        name = parent + node.name
                        if len(name.encode("utf-8")) > MAX_NAME_BYTES:
                            raise IndexUnavailable("qualified declaration name exceeds index limit")
                        if len(symbols) >= MAX_SYMBOLS:
                            raise IndexUnavailable("symbol count exceeds index limit")
                        record = {"name": name, "line": node.lineno,
                                  "kind": "class" if isinstance(node, ast.ClassDef) else "function"}
                        if spans:
                            record.update(start_line=min([node.lineno, *(item.lineno for item in node.decorator_list)]),
                                          end_line=node.end_lineno)
                        symbols.append(record)
                        if isinstance(node, ast.ClassDef):
                            pending.append((node.body, name + "."))
            return sorted(symbols, key=lambda item: item["line"])
        except IndexUnavailable:
            # Resource limits may change; never cache an incomplete result or
            # treat it as a syntax error against otherwise unchanged content.
            raise
        except (SyntaxError, ValueError, RecursionError):
            return None
