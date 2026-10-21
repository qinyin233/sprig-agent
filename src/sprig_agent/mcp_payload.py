"""Text-only MCP payloads remain inert data; URLs and blobs are never fetched."""
from .mcp_http import McpError
from .config import redact


def safe_redact(value, secrets, limit=1024 * 1024):
    used = 0
    def text(item):
        nonlocal used
        for secret in secrets:
            if not secret:
                continue
            estimate = len(item.encode("utf-8")) + item.count(secret) * (10 - len(secret.encode("utf-8")))
            if estimate > limit - used:
                raise McpError("MCP redacted output exceeds its memory budget")
            item = redact(item, secret)
            if len(item.encode("utf-8")) > limit - used:
                raise McpError("MCP redacted output exceeds its memory budget")
        used += len(item.encode("utf-8"))
        if used > limit:
            raise McpError("MCP redacted output exceeds its memory budget")
        return item
    def walk(item):
        if isinstance(item, str):
            return text(item)
        if isinstance(item, dict):
            return {text(key): walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        return item
    return walk(value)


def block(value):
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise McpError("invalid MCP content block")
    kind = value["type"]
    if kind == "text":
        if not isinstance(value.get("text"), str):
            raise McpError("invalid MCP text block")
        return value
    if kind == "resource" and isinstance(value.get("resource"), dict):
        return {**value, "resource": resource(value["resource"])}
    if kind == "resource_link" and isinstance(value.get("uri"), str):
        return value
    return {"type": kind, "omitted": True, "reason": "binary or unsupported MCP content; no download or model image input"}


def resource(value):
    if not isinstance(value, dict) or not isinstance(value.get("uri"), str):
        raise McpError("invalid MCP resource content")
    if isinstance(value.get("text"), str):
        return {key: item for key, item in value.items() if key != "blob"}
    if isinstance(value.get("blob"), str):
        return {"uri": value["uri"], "omitted": True, "reason": "binary MCP resource is unsupported"}
    raise McpError("MCP resource must contain text or blob")


def payload(action, data):
    if action == "call":
        if type(data.get("isError", False)) is not bool or not (isinstance(data.get("content"), list) or isinstance(data.get("structuredContent"), dict)):
            raise McpError("invalid MCP tool result")
        return {**data, "content": [block(row) for row in data.get("content", [])]}
    fields = {"resources": ("resources", "uri"), "templates": ("resourceTemplates", "uriTemplate"), "prompts": ("prompts", "name")}
    if action in fields:
        field, identifier = fields[action]
        rows = data.get(field)
        if not isinstance(rows, list) or len(rows) > 256 or any(not isinstance(row, dict) or not isinstance(row.get(identifier), str) for row in rows):
            raise McpError("invalid or oversized MCP catalog page")
    elif action == "read_resource":
        if not isinstance(data.get("contents"), list):
            raise McpError("invalid MCP read-resource response")
        return {**data, "contents": [resource(row) for row in data["contents"]]}
    elif action == "get_prompt":
        if not isinstance(data.get("messages"), list) or len(data["messages"]) > 256:
            raise McpError("invalid MCP prompt response")
        rows = []
        for row in data["messages"]:
            if not isinstance(row, dict) or row.get("role") not in ("user", "assistant"):
                raise McpError("invalid MCP prompt message")
            rows.append({**row, "content": block(row.get("content"))})
        return {**data, "messages": rows}
    return data
