"""Bounded JSON Schema 2020-12 with local refs and RE2 pattern evaluation."""
import json
import math
import re
import time
from contextvars import ContextVar

def bounded(value, maximum=65536, *, depth=24, nodes=8192):
    pending = [(value, 0)]
    count = 0
    while pending:
        item, level = pending.pop()
        count += 1
        if level > depth or count > nodes:
            raise ValueError("MCP JSON structure exceeds depth/node limits")
        if isinstance(item, dict):
            if any(not isinstance(k, str) for k in item):
                raise ValueError("MCP JSON keys must be strings")
            pending.extend((v, level + 1) for v in item.values())
        elif isinstance(item, list):
            pending.extend((v, level + 1) for v in item)
        elif not (item is None or type(item) in (str, int, bool) or type(item) is float and math.isfinite(item)):
            raise ValueError("MCP values must be finite JSON")
    raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) > maximum:
        raise ValueError("MCP JSON exceeds byte limit")
    return raw


def regex(pattern):
    import re2
    if not isinstance(pattern, str) or len(pattern) > 1024:
        raise ValueError("MCP schema regex exceeds bounds")
    options = re2.Options()
    options.log_errors = False
    options.max_mem = 1024 * 1024
    try:
        return re2.compile(pattern, options=options)
    except (re2.error, ValueError):
        raise ValueError("MCP schema patterns must use supported RE2 syntax") from None


def pattern(validator, value, instance, schema):
    from jsonschema import ValidationError
    if isinstance(instance, str) and not regex(value).search(instance):
        yield ValidationError("string does not match the MCP tool pattern")


def pattern_properties(validator, patterns, instance, schema):
    if isinstance(instance, dict):
        for expression, child in patterns.items():
            matcher = regex(expression)
            for key, value in instance.items():
                if matcher.search(key):
                    yield from validator.descend(value, child, path=key, schema_path=expression)


def additional_properties(validator, value, instance, schema):
    from jsonschema import ValidationError
    # jsonschema's stock additionalProperties uses Python re for patternProperties.
    if not isinstance(instance, dict):
        return
    patterns = [regex(p) for p in schema.get("patternProperties", {})]
    for key, item in instance.items():
        if key not in schema.get("properties", {}) and not any(p.search(key) for p in patterns):
            if value is False:
                yield ValidationError("unexpected MCP tool argument")
            elif isinstance(value, dict):
                yield from validator.descend(item, value, path=key)


_budget = ContextVar("mcp_schema_budget", default=None)


def guarded(function):
    def evaluate(*args, **kwargs):
        budget = _budget.get()
        if budget is not None:
            budget[1] += 1
            budget[2]()
            if budget[1] > 8192 or time.monotonic() > budget[0]:
                raise ValueError("MCP schema validation work budget exceeded")
        yield from function(*args, **kwargs)
    return evaluate


_validator = None


def validator_type():
    # Core/benchmark clients that do not use MCP need no schema/regex runtime.
    global _validator
    if _validator is None:
        from jsonschema import Draft202012Validator, validators
        safe = validators.extend(Draft202012Validator, {"pattern": pattern, "patternProperties": pattern_properties,
                                                       "additionalProperties": additional_properties})
        _validator = validators.extend(safe, {key: guarded(fn) for key, fn in safe.VALIDATORS.items()})
    return _validator


def valid_arguments(validator, arguments, check):
    token = _budget.set([time.monotonic() + 1, 0, check])
    try:
        return next(validator.iter_errors(arguments), None) is None
    finally:
        _budget.reset(token)


def no_remote(uri):
    from referencing.exceptions import NoSuchResource
    raise NoSuchResource(ref=uri)


def prepare(schema):
    from jsonschema.exceptions import SchemaError
    from referencing import Registry
    Validator = validator_type()
    bounded(schema, 32768, depth=16, nodes=1024)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("MCP tool inputSchema must be an object schema")
    if schema.get("$schema", "https://json-schema.org/draft/2020-12/schema") not in (
            "https://json-schema.org/draft/2020-12/schema", "https://json-schema.org/draft/2020-12/schema#"):
        raise ValueError("MCP tool schema dialect is unsupported; use JSON Schema 2020-12")
    try:
        Validator.check_schema(schema)
    except (SchemaError, RecursionError):
        raise ValueError("invalid bounded MCP tool schema") from None
    refs, compositions, schemas = [], 0, set()
    def walk(node, path=()):
        nonlocal compositions
        schemas.add(path)
        if isinstance(node, dict):
            if "unevaluatedProperties" in node:
                raise ValueError("MCP schema unevaluatedProperties is unsupported; use bounded additionalProperties")
            if path and "$schema" in node:
                raise ValueError("nested MCP schema dialect changes are unsupported")
            if "$id" in node or "$dynamicRef" in node or "$dynamicAnchor" in node:
                raise ValueError("MCP tool schemas require bounded local JSON Pointer refs without IDs/dynamic refs")
            if "$ref" in node:
                ref = node["$ref"]
                if not isinstance(ref, str) or not ref.startswith("#/"):
                    raise ValueError("MCP tool refs must be local JSON Pointers; no network resolution")
                target = tuple(p.replace("~1", "/").replace("~0", "~") for p in ref[2:].split("/"))
                refs.append((path, target))
            if "pattern" in node:
                regex(node["pattern"])
            for expression in node.get("patternProperties", {}):
                regex(expression)
            compositions += sum(key in node for key in ("allOf", "anyOf", "oneOf", "if", "not"))
            for key in ("properties", "patternProperties", "$defs", "dependentSchemas"):
                for name, child in node.get(key, {}).items():
                    walk(child, (*path, key, name))
            for key in ("items", "additionalProperties", "unevaluatedProperties", "unevaluatedItems", "propertyNames", "contains", "not", "if", "then", "else"):
                if key in node:
                    walk(node[key], (*path, key))
            for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
                for i, child in enumerate(node.get(key, [])):
                    walk(child, (*path, key, str(i)))
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, (*path, str(i)))
    walk(schema)
    if compositions > 32:
        raise ValueError("MCP schema composition exceeds limits")
    # Reject cyclic refs before invoking the validator, including indirect cycles.
    checked = set()
    def follow(target, seen):
        if target in seen:
            raise ValueError("cyclic MCP schema refs are unsupported")
        if target not in schemas:
            raise ValueError("MCP refs must target schema nodes, not arbitrary annotations")
        if target in checked:
            return
        value = schema
        for part in target:
            value = value[int(part)] if isinstance(value, list) else value[part]
        for source, child in refs:
            if source[:len(target)] == target:
                follow(child, {*seen, target})
        checked.add(target)
    try:
        for _, target in refs:
            follow(target, set())
    except (KeyError, IndexError, TypeError):
        raise ValueError("unresolved local MCP tool schema ref") from None
    return Validator(schema, registry=Registry(retrieve=no_remote))


def header_paths(schema):
    result, names = [], set()
    def visit(node, path=(), eligible=True):
        if isinstance(node, dict):
            if "x-mcp-header" in node:
                name = node["x-mcp-header"]
                kind = node.get("type")
                if (not eligible or not path or not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}", name)
                        or name.lower() in names or kind not in ("string", "integer", "boolean")):
                    raise ValueError("invalid x-mcp-header annotation")
                names.add(name.lower())
                result.append((path, name, kind))
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for prop, child in value.items():
                        visit(child, (*path, prop), eligible)
                else:
                    visit(value, path, False)
        elif isinstance(node, list):
            for value in node:
                visit(value, path, False)
    visit(schema)
    if len(result) > 16:
        raise ValueError("too many MCP mirrored parameter headers")
    return result
