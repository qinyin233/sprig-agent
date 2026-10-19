"""Bounded syntactic call candidates, without name binding or code execution."""

import ast
import heapq

from .syntax import IndexUnavailable, MAX_NAME_BYTES, MAX_NODES, MAX_SYMBOLS


def bounded_name(parts):
    if sum(len(part.encode("utf-8")) for part in parts) + max(0, len(parts) - 1) > MAX_NAME_BYTES:
        raise IndexUnavailable("call name exceeds index limit")
    return ".".join(parts)


def python_calls(tree, check_cancelled):
    calls, pending, visited, unresolved = [], [(tree, "")], 0, False
    while pending:
        check_cancelled()
        node, enclosing = pending.pop()
        visited += 1
        if visited > MAX_NODES:
            raise IndexUnavailable("call traversal exceeds index limit")
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            enclosing = bounded_name([enclosing, node.name]) if enclosing else bounded_name([node.name])
        if isinstance(node, ast.Call):
            current, parts = node.func, []
            for _ in range(64):
                check_cancelled()
                if isinstance(current, ast.Attribute):
                    parts.append(current.attr)
                    current = current.value
                else:
                    break
            else:
                raise IndexUnavailable("call target exceeds traversal limit")
            complete = isinstance(current, ast.Name)
            if complete:
                parts.append(current.id)
            unresolved |= not complete
            if parts:
                target = bounded_name(list(reversed(parts)))
                record = {"name": parts[0], "line": node.lineno, "column": node.col_offset + 1,
                          "kind": "call", "enclosing": enclosing}
                if target != record["name"]:
                    record["target"] = target
                if not complete:
                    record["receiver_unresolved"] = True
                calls.append(record)
                if len(calls) > MAX_SYMBOLS:
                    raise IndexUnavailable("call count exceeds index limit")
        pending.extend((child, enclosing) for child in reversed(list(ast.iter_child_nodes(node))))
    return {"calls": sorted(calls, key=lambda item: (item["line"], item["column"])), "unresolved": unresolved}


IDENTIFIERS = {"identifier", "field_identifier", "property_identifier", "type_identifier", "namespace_identifier",
               "self", "this", "super", "crate"}
MEMBERS = {"member_expression", "field_expression", "selector_expression", "field_access"}
WRAPPERS = {"generic_function", "template_function", "generic_type", "template_type",
            "parenthesized_expression"}
CALL_NODES = {"call_expression", "method_invocation", "object_creation_expression", "new_expression"}


def _tree_target(node, raw, check_cancelled, depth=0):
    check_cancelled()
    if node is None:
        return [], False
    if depth >= 64:
        raise IndexUnavailable("call target exceeds traversal limit")
    if node.type in IDENTIFIERS:
        if node.end_byte - node.start_byte > MAX_NAME_BYTES:
            raise IndexUnavailable("call name exceeds index limit")
        return [raw[node.start_byte:node.end_byte].decode("utf-8")], True
    if node.type in MEMBERS:
        field = node.child_by_field_name("field") or node.child_by_field_name("property")
        base = node.child_by_field_name("object") or node.child_by_field_name("operand")
        if node.type == "field_expression":
            base = node.child_by_field_name("value") or node.child_by_field_name("argument") or base
        left, left_complete = _tree_target(base, raw, check_cancelled, depth + 1)
        right, right_complete = _tree_target(field, raw, check_cancelled, depth + 1)
        return left + right, left_complete and right_complete
    if node.type in {"scoped_identifier", "scoped_type_identifier", "qualified_identifier"}:
        left, left_complete = _tree_target(node.child_by_field_name("path") or node.child_by_field_name("scope"),
                                         raw, check_cancelled, depth + 1)
        right, right_complete = _tree_target(node.child_by_field_name("name"), raw, check_cancelled, depth + 1)
        return left + right, left_complete and right_complete
    if node.type in WRAPPERS:
        child = (node.child_by_field_name("function") or node.child_by_field_name("name")
                 or node.child_by_field_name("type") or next(iter(node.named_children), None))
        return _tree_target(child, raw, check_cancelled, depth + 1)
    # Go index expressions can be either generic instantiations or dynamic
    # subscripts. Retain a name candidate and explicitly mark it unresolved.
    if node.type == "index_expression":
        parts, _ = _tree_target(node.child_by_field_name("operand"), raw, check_cancelled, depth + 1)
        return parts, False
    return [], False


def tree_calls(root, raw, declarations, check_cancelled):
    calls, pending, visited, unresolved = [], [root], 0, False
    while pending:
        check_cancelled()
        node = pending.pop()
        visited += 1
        if visited > MAX_NODES:
            raise IndexUnavailable("call traversal exceeds index limit")
        if node.type in CALL_NODES:
            target = (node.child_by_field_name("function") or node.child_by_field_name("constructor")
                      or node.child_by_field_name("type") or node.child_by_field_name("name"))
            parts, complete = _tree_target(target, raw, check_cancelled)
            if node.type == "method_invocation" and node.child_by_field_name("object") is not None:
                base, base_complete = _tree_target(node.child_by_field_name("object"), raw, check_cancelled)
                parts, complete = base + parts, base_complete and complete
            unresolved |= not complete
            if parts:
                target_name = bounded_name(parts)
                record = {"name": parts[-1].removeprefix("r#"), "line": node.start_point.row + 1,
                          "column": node.start_point.column + 1, "kind": "call", "enclosing": "",
                          "_byte": node.start_byte}
                if target_name != record["name"]:
                    record["target"] = target_name
                if not complete:
                    record["receiver_unresolved"] = True
                calls.append(record)
                if len(calls) > MAX_SYMBOLS:
                    raise IndexUnavailable("call count exceeds index limit")
        # Do not skip template strings: interpolations can contain real calls.
        if node.type not in {"comment", "string", "string_literal", "raw_string_literal"}:
            pending.extend(reversed(node.named_children))
    calls.sort(key=lambda item: (item["line"], item["column"]))
    # Nearest containing INDEXED declaration, not a runtime caller hierarchy.
    spans = sorted(declarations, key=lambda item: (item["_start_byte"], item["_end_byte"], item["name"]))
    active, offset = [], 0
    for call in calls:
        check_cancelled()
        position = call.pop("_byte")
        while offset < len(spans) and spans[offset]["_start_byte"] <= position:
            check_cancelled()
            item = spans[offset]
            heapq.heappush(active, (-item["_start_byte"], item["_end_byte"], item["name"]))
            offset += 1
        while active and active[0][1] <= position:
            heapq.heappop(active)
        if active:
            call["enclosing"] = active[0][2]
    return {"calls": calls, "unresolved": unresolved}
