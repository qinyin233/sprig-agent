"""Local, bounded declaration discovery. This is not semantic analysis."""

import ast
import importlib
import warnings

MAX_PARSE_MICROS = 1000000
MAX_NODES = 100000
MAX_SYMBOLS = 4096
MAX_NAME_BYTES = 1024
MAX_DIAGNOSTIC_BYTES = 512 * 1024
MAX_DIAGNOSTICS = 3

EXTENSIONS = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".jsx": "javascript", ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx", ".rs": "rust", ".go": "go", ".c": "c", ".h": "cpp",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".hxx": "cpp", ".java": "java",
}

DECLARATIONS = {
    "function_declaration": "function", "generator_function_declaration": "function",
    "function_item": "function", "function_signature_item": "function",
    "method_definition": "function", "method_signature": "function",
    "abstract_method_signature": "function", "method_declaration": "function",
    "constructor_declaration": "function", "compact_constructor_declaration": "function",
    "class_declaration": "class", "abstract_class_declaration": "class",
    "class_specifier": "class", "interface_declaration": "interface",
    "annotation_type_declaration": "interface", "enum_declaration": "enum",
    "record_declaration": "class", "struct_item": "struct", "union_item": "struct",
    "enum_item": "enum", "trait_item": "trait", "type_item": "type",
    "type_alias_declaration": "type", "alias_declaration": "type",
    "mod_item": "module", "namespace_definition": "module", "internal_module": "module",
    "struct_specifier": "struct", "union_specifier": "struct", "enum_specifier": "enum",
}
CONTAINERS = {"class", "interface", "enum", "struct", "trait", "module"}
SKIP = {
    "comment", "string", "string_literal", "template_string", "raw_string_literal",
    "block", "statement_block", "compound_statement", "constructor_body",
    "arrow_function", "function_expression", "generator_function", "lambda_expression",
    "class", "class_expression", "object", "new_expression", "object_creation_expression", "enum_constant",
}


class IndexUnavailable(ValueError):
    """Transient timeout or traversal bound; must not poison the content cache."""


def language_for(name):
    # A capital .C conventionally denotes C++, even on case-insensitive filesystems.
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return "cpp" if suffix == ".C" else EXTENSIONS.get(suffix.lower())


class SyntaxParser:
    def __init__(self):
        self.parsers = {}

    def clear(self):
        self.parsers.clear()

    def _parser(self, language):
        if language not in self.parsers:
            from tree_sitter import Language, Parser

            module = importlib.import_module("tree_sitter_" + ("typescript" if language == "tsx" else language))
            factory = {"typescript": "language_typescript", "tsx": "language_tsx"}.get(language, "language")
            parser = Parser(Language(getattr(module, factory)()))
            # 0.26 progress callbacks crash on Windows. 0.25.2's bounded native
            # timeout is pinned and exercised on every supported CI platform.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                parser.timeout_micros = MAX_PARSE_MICROS
            self.parsers[language] = parser
        return self.parsers[language]

    def parse(self, text, language, check_cancelled, *, spans=False, calls=False):
        check_cancelled()
        # Match physical CR/LF line numbers from read_file/search. These bytes
        # are temporary; only declaration metadata enters SymbolIndex's cache.
        raw = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        tree = self._tree(raw, language, check_cancelled)
        if tree.root_node.has_error:
            return None
        if calls:
            from .call_sites import tree_calls
            indexed = declarations(tree.root_node, raw, language, check_cancelled, spans=True, positions=True)
            return tree_calls(tree.root_node, raw, indexed, check_cancelled)
        return (declarations(tree.root_node, raw, language, check_cancelled, spans=True) if spans
                else declarations(tree.root_node, raw, language, check_cancelled))

    def _tree(self, raw, language, check_cancelled):
        parser = self._parser(language)
        parser.reset()
        try:
            tree = parser.parse(raw)
        except ValueError:
            raise IndexUnavailable("symbol parsing exceeded its time limit") from None
        finally:
            # Never resume an incomplete parse against another source file.
            parser.reset()
        check_cancelled()
        if tree is None:
            raise IndexUnavailable("symbol parsing exceeded its time limit")
        return tree

    def diagnostics(self, text, language, check_cancelled):
        """Parse one bounded text; return positions, never source or error text.

        Python AST parsing and tree-sitter recovery are observations, not a
        compiler, language-version compatibility check or task acceptance.
        """
        check_cancelled()
        text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        raw = text.encode("utf-8")
        if len(raw) > MAX_DIAGNOSTIC_BYTES:
            raise IndexUnavailable("diagnostic input exceeds its byte limit")
        if language == "python":
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    ast.parse(text)
            except SyntaxError as error:
                check_cancelled()
                return {"status": "parse_error", "locations": [{"kind": "parse_error",
                    "line": max(1, error.lineno or 1), "column": max(1, error.offset or 1)}]}
            check_cancelled()
            return {"status": "parsed"}
        tree = self._tree(raw, language, check_cancelled)
        if not tree.root_node.has_error:
            return {"status": "parsed"}
        locations, pending, visited = [], [tree.root_node], 0
        lines = raw.split(b"\n")
        while pending:
            check_cancelled()
            node = pending.pop()
            visited += 1
            if visited > MAX_NODES:
                raise IndexUnavailable("diagnostic traversal exceeds its node limit")
            if node.is_error or node.is_missing:
                row, column = node.start_point
                locations.append({"kind": "missing_token" if node.is_missing else "parse_error",
                    "line": row + 1, "column": len(lines[row][:column].decode("utf-8", errors="ignore")) + 1})
                if len(locations) == MAX_DIAGNOSTICS:
                    return {"status": "parse_error", "locations": locations, "limit_reached": True}
            elif node.has_error:
                if len(pending) + node.child_count > MAX_NODES:
                    raise IndexUnavailable("diagnostic traversal exceeds its node limit")
                pending.extend(reversed(node.children))
        return {"status": "parse_error", "locations": locations}


def declarations(root, raw, language, check_cancelled, *, spans=False, positions=False):
    symbols, seen, pending, visited = [], set(), [(root, "")], 0

    def name_of(node):
        if node is None:
            return ""
        if node.end_byte - node.start_byte > MAX_NAME_BYTES:
            raise IndexUnavailable("declaration name exceeds index limit")
        return raw[node.start_byte:node.end_byte].decode("utf-8")

    def add(node, name, kind, parent):
        if not name:
            return ""
        qualified = name if "::" in name else parent + name
        if len(qualified.encode("utf-8")) > MAX_NAME_BYTES:
            raise IndexUnavailable("qualified declaration name exceeds index limit")
        item = (qualified, node.start_point.row + 1, kind)
        identity = (*item, node.start_byte, node.end_byte) if spans else item
        if identity not in seen:
            seen.add(identity)
            record = {"name": item[0], "line": item[1], "kind": item[2]}
            if spans:
                record.update(start_line=item[1], end_line=max(item[1], node.end_point.row + bool(node.end_point.column)))
            if positions:
                record.update(_start_byte=node.start_byte, _end_byte=node.end_byte)
            symbols.append(record)
            if len(symbols) > MAX_SYMBOLS:
                raise IndexUnavailable("symbol count exceeds index limit")
        return qualified + "."

    def declarator(node):
        """Follow declarator fields, excluding C function-pointer variables."""
        current, function = node.child_by_field_name("declarator"), None
        for _ in range(64):
            if current is None:
                return None
            if current.type == "function_declarator":
                function = current
            if current.type in {"identifier", "field_identifier", "qualified_identifier", "operator_name",
                                "destructor_name"}:
                return current if function is not None else None
            if function is not None and current.type in {"pointer_declarator", "reference_declarator"}:
                return None
            following = current.child_by_field_name("declarator")
            if following is None and current.type == "parenthesized_declarator" and len(current.named_children) == 1:
                following = current.named_children[0]
            current = following
        raise IndexUnavailable("declarator nesting exceeds index limit")

    while pending:
        check_cancelled()
        node, parent = pending.pop()
        visited += 1
        if visited > MAX_NODES:
            raise IndexUnavailable("syntax traversal exceeds index limit")
        kind, child_parent = DECLARATIONS.get(node.type), parent
        if language == "go" and node.type == "method_declaration":
            kind = None  # Receiver-qualified Go method, not a Java member.
        if kind:
            name = name_of(node.child_by_field_name("name"))
            child_parent = add(node, name, kind, parent) or parent
            if kind not in CONTAINERS:
                continue
        elif node.type == "impl_item" and language == "rust":
            target = node.child_by_field_name("type")
            if target is not None and target.type == "generic_type":
                target = target.child_by_field_name("type")
            child_parent = parent + name_of(target) + "."
        elif node.type == "type_spec" and language == "go":
            target = node.child_by_field_name("type")
            kind = {"struct_type": "struct", "interface_type": "interface"}.get(target.type if target else "", "type")
            child_parent = add(node, name_of(node.child_by_field_name("name")), kind, parent) or parent
        elif node.type == "method_elem" and language == "go":
            add(node, name_of(node.child_by_field_name("name")), "function", parent)
            continue
        elif node.type == "method_declaration" and language == "go":
            receiver = node.child_by_field_name("receiver")
            receiver = next(iter(receiver.named_children), None) if receiver else None
            receiver = receiver.child_by_field_name("type") if receiver else None
            for _ in range(32):
                if receiver is None or receiver.type not in {"pointer_type", "generic_type"}:
                    break
                receiver = receiver.child_by_field_name("type") or next(iter(receiver.named_children), None)
            add(node, name_of(node.child_by_field_name("name")), "function", parent + name_of(receiver) + ".")
            continue
        elif node.type in {"function_definition", "declaration", "field_declaration"} and language in {"c", "cpp"}:
            name = declarator(node)
            if name is not None:
                add(node, name_of(name), "function", parent)
                continue
        elif node.type == "type_definition" and language in {"c", "cpp"}:
            name = node.child_by_field_name("declarator")
            if name is not None and name.type == "type_identifier":
                target = node.child_by_field_name("type")
                tag = target.child_by_field_name("name") if target is not None else None
                # A same-name tagged typedef already has its struct/enum entry.
                if name_of(tag) != name_of(name):
                    add(node, name_of(name), "type", parent)
        elif node.type in {"variable_declarator", "field_definition", "public_field_definition"}:
            value = node.child_by_field_name("value")
            name = node.child_by_field_name("name") or node.child_by_field_name("property")
            if name is not None and name.type in {"identifier", "property_identifier"} and value is not None:
                if value.type in {"arrow_function", "function_expression", "generator_function"}:
                    add(node, name_of(name), "function", parent)
                    continue
                if value.type in {"class", "class_expression"}:
                    child_parent = add(node, name_of(name), "class", parent) or parent
                    body = value.child_by_field_name("body")
                    if body is not None:
                        pending.append((body, child_parent))
                    continue
        if node.type in SKIP:
            continue
        children = node.named_children
        pending.extend((child, child_parent) for child in reversed(children))
    return sorted(symbols, key=lambda item: item["line"])
