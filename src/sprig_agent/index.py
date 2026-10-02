"""Bounded in-memory symbol parsing; callers still read and validate fresh files."""

import ast
from collections import OrderedDict
import hashlib
import json

MAX_CACHE_ENTRIES = 128
MAX_CACHE_BYTES = 1024 * 1024


class SymbolIndex:
    def __init__(self):
        self.entries = OrderedDict()
        self.bytes = 0

    def clear(self):
        self.entries.clear()
        self.bytes = 0

    def python(self, text):
        # Content, not mtime, identifies a parse. Never cache raw source or ASTs.
        key = hashlib.sha256(text.encode("utf-8")).digest()
        if key in self.entries:
            encoded, size = self.entries.pop(key)
            self.entries[key] = (encoded, size)
            return json.loads(encoded)
        else:
            try:
                tree = ast.parse(text)
                symbols, pending = [], [(tree.body, "")]
                while pending:
                    nodes, parent = pending.pop()
                    for node in nodes:
                        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                            name = parent + node.name
                            symbols.append({"name": name, "line": node.lineno,
                                            "kind": "class" if isinstance(node, ast.ClassDef) else "function"})
                            if isinstance(node, ast.ClassDef):
                                pending.append((node.body, name + "."))
                data = sorted(symbols, key=lambda item: item["line"])
            except (SyntaxError, ValueError, RecursionError):
                data = None
            encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            size = len(encoded.encode("utf-8")) + len(key)
            if size <= MAX_CACHE_BYTES:
                while self.entries and (len(self.entries) >= MAX_CACHE_ENTRIES or self.bytes + size > MAX_CACHE_BYTES):
                    _, (_, removed) = self.entries.popitem(last=False)
                    self.bytes -= removed
                self.entries[key] = (encoded, size)
                self.bytes += size
        # Cached JSON is immutable; returned dictionaries never share storage.
        return data
