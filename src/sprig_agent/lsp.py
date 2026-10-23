"""Approved semantic queries with private paths, current source snapshots and pages."""
from collections import OrderedDict
import json
from pathlib import Path
import re
import shlex
import threading
import urllib.parse
import urllib.request
import uuid

from .config import redact
from .lsp_client import Budget, Client, LspError, MAX_FRAME, encode

METHODS = {"definition": ("textDocument/definition", "definitionProvider"),
           "references": ("textDocument/references", "referencesProvider"),
           "implementation": ("textDocument/implementation", "implementationProvider"),
           "hover": ("textDocument/hover", "hoverProvider"),
           "symbols": ("textDocument/documentSymbol", "documentSymbolProvider")}
LSP_SCHEMA = {"type": "function", "function": {"name": "lsp",
    "description": "Approved configured language servers: definition/references/implementation/hover/symbols/diagnostics by relative path; 1-based Unicode positions. workspace_symbols: explicit server + query, no path. Observations, not edits/acceptance. result_id+offset pages cached data without RPC/replay.",
    "parameters": {"type": "object", "properties": {
        "action": {"enum": ["status", "definition", "references", "implementation", "hover", "symbols", "workspace_symbols", "diagnostics", "disconnect", "result"]},
        "server": {"type": "string", "maxLength": 64}, "path": {"type": "string", "maxLength": 4096},
        "query": {"type": "string", "maxLength": 256},
        "line": {"type": "integer", "minimum": 1, "maximum": 1000000},
        "column": {"type": "integer", "minimum": 1, "maximum": 1000000},
        "result_id": {"type": "string", "maxLength": 40}, "offset": {"type": "integer", "minimum": 0, "maximum": MAX_FRAME}},
        "required": ["action"], "additionalProperties": False}}}


def position(text, line, column):
    lines = re.split(r"\r\n|\r|\n", text)
    if type(line) is not int or type(column) is not int or not 1 <= line <= len(lines) or not 1 <= column <= len(lines[line - 1]) + 1:
        raise LspError("LSP line/column must identify a 1-based Unicode position in the current document")
    return {"line": line - 1, "character": len(lines[line - 1][:column - 1].encode("utf-16-le")) // 2}


def location_range(text, value):
    if not isinstance(value, dict) or set(value) != {"start", "end"}:
        raise LspError("invalid LSP range")
    lines = re.split(r"\r\n|\r|\n", text)
    result = {}
    previous = None
    for label in ("start", "end"):
        point = value[label]
        if (not isinstance(point, dict) or type(point.get("line")) is not int or type(point.get("character")) is not int
                or not 0 <= point["line"] < len(lines) or point["character"] < 0):
            raise LspError("invalid LSP position")
        raw = lines[point["line"]].encode("utf-16-le")
        if point["character"] * 2 > len(raw):
            raise LspError("LSP result position exceeds the current document")
        try:
            column = len(raw[:point["character"] * 2].decode("utf-16-le")) + 1
        except UnicodeError:
            raise LspError("LSP result splits a UTF-16 surrogate pair") from None
        current = (point["line"] + 1, column)
        if previous is not None and current < previous:
            raise LspError("LSP result has a reversed range")
        previous = current
        result[label + "_line"], result[label + "_column"] = current
    return result


class LanguageServers:
    def __init__(self, config, check, approve, prepare, document):
        self.config, self.check, self.approve = config, check, approve
        self.prepare, self.document = prepare, document
        self.clients, self.results = {}, OrderedDict()
        self.lock, self.operation = threading.RLock(), threading.Lock()
        self.epoch, self.closed = 0, False

    def reset(self, *, closing=False):
        with self.lock:
            self.closed |= closing
            self.epoch += 1
            self.results.clear()
            for name, client in list(self.clients.items()):
                client.close()
                self.clients.pop(name, None)

    def status(self):
        self.check()
        with self.lock:
            return {"servers": [{**server.public(), "connection": self.clients[server.name].status() if server.name in self.clients else {"running": False}}
                                 for server in self.config.lsp_servers], "processes_started": False, "persistent": False}

    def path_from_uri(self, uri):
        if not isinstance(uri, str) or len(uri) > 8192:
            raise LspError("invalid LSP file URI")
        parts = urllib.parse.urlsplit(uri)
        if parts.scheme != "file" or parts.netloc or parts.query or parts.fragment:
            raise LspError("LSP results must use local workspace file URIs")
        path = Path(urllib.request.url2pathname(parts.path))
        if not path.is_absolute():
            raise LspError("LSP file URI is not absolute")
        return path.relative_to(self.config.workdir).as_posix()

    def control(self, action="status", server=None, path=None, line=None, column=None, query=None, result_id=None, offset=0):
        if not self.operation.acquire(blocking=False):
            raise LspError("another LSP operation is active; wait or cancel it")
        epoch = self.epoch
        def check():
            self.check()
            if self.closed or epoch != self.epoch:
                raise LspError("LSP operation expired after owner reset/close")
        try:
            check()
            if not self.config.tool_enabled("lsp"):
                raise LspError("LSP tool is disabled")
            if action == "status":
                if any(value is not None for value in (server, path, line, column, query, result_id)) or offset:
                    raise LspError("LSP status has no query parameters")
                return self._save(self.status(), (), check)
            if action == "result":
                if any(value is not None for value in (server, path, line, column, query)):
                    raise LspError("LSP result accepts only result_id and offset")
                return self._page(result_id, offset, check)
            workspace = action == "workspace_symbols"
            if result_id is not None or offset or action not in {*METHODS, "diagnostics", "disconnect", "workspace_symbols"}:
                raise LspError("invalid LSP action or query parameters")
            if workspace:
                if not isinstance(server, str) or any(value is not None for value in (path, line, column)):
                    raise LspError("workspace_symbols requires an explicit server and query; no path or line/column")
                if (not isinstance(query, str) or len(query) > 256 or len(query.encode("utf-8")) > 1024
                        or any(ord(char) < 32 or ord(char) == 127 for char in query)):
                    raise LspError("workspace symbol query must be at most 256 characters without controls; empty requests all symbols")
                if any(secret in query for secret in self.config.credential_secrets):
                    raise LspError("LSP queries must not contain configured credentials")
            elif query is not None:
                raise LspError("query is only valid for workspace_symbols")
            if self.config.read_only:
                raise LspError("read-only mode blocks language-server execution and RPCs")
            if self.config.max_output < 1024:
                raise LspError("LSP queries require max_output >= 1024")
            choices = [row for row in self.config.lsp_servers if row.enabled and (row.name == server if server is not None else
                       path is not None and Path(path).suffix.lower() in {suffix.lower() for suffix, _ in row.languages})]
            if len(choices) != 1:
                raise LspError("select one configured enabled LSP server; suffix selection must be unambiguous")
            selected = choices[0]
            if selected.missing_env or any(name not in dict(selected.env) for name, _ in selected.env_from):
                raise LspError("LSP credential environment is missing; no process started")
            if action == "disconnect":
                if any(value is not None for value in (path, line, column)):
                    raise LspError("disconnect accepts only the server name")
                if self.approve("STOP owned language server " + selected.name + "; prior filesystem/network effects are not undone") is not True:
                    return {"approved": False}
                check()
                with self.lock:
                    client = self.clients.get(selected.name)
                    if client:
                        client.close()
                        self.clients.pop(selected.name, None)
                return {"approved": True, "disconnected": True, "server": selected.name}
            if not self.config.tool_enabled("read_file"):
                raise LspError("LSP queries require the read_file tool")
            if not workspace and not isinstance(path, str):
                raise LspError("LSP query requires a workspace-relative file path")
            suffixes = {suffix.lower(): language for suffix, language in selected.languages}
            if not workspace and Path(path).suffix.lower() not in suffixes:
                raise LspError("file suffix is not declared for the selected language server")
            primary = None if workspace else self.document(path)
            point = position(primary["text"], line, column) if action in {"definition", "references", "implementation", "hover"} else None
            if point is None and (line is not None or column is not None):
                raise LspError("this LSP operation does not use line/column")
            directory, environment, recheck = self.prepare(selected.process_config())
            with self.lock:
                client = self.clients.get(selected.name)
                if client is not None and not client.status()["running"]:
                    client.close()
                    self.clients.pop(selected.name, None)
                    client = None
                # Refresh only explicitly opened documents; cap four snapshots.
                try:
                    opened = list(client.documents.values()) if client else []
                    if primary is not None and client and primary["uri"] not in client.documents:
                        opened = opened[-3:]
                    prepared = {row["uri"]: self.document(row["path"]) for row in opened}
                    if primary is not None:
                        prepared[primary["uri"]] = primary
                except BaseException:
                    if client is not None:
                        client.close()
                        self.clients.pop(selected.name, None)
                    raise
            for document in prepared.values():
                encode({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {
                    "uri": document["uri"], "languageId": suffixes.get(Path(document["path"]).suffix.lower(), "plaintext"),
                    "version": 1, "text": document["text"]}}})
            listing = [{"path": row["path"], "sha256": row["sha256"], "bytes": row["bytes"]} for row in prepared.values()]
            text = (f"LSP {action} via {selected.name}\n" + ("START " if client is None else "REUSE ") + json.dumps(list(selected.command))
                    + "\ncwd: " + json.dumps(str(directory)) + "\nEnvironment names: " + json.dumps([key for key, _ in (*selected.env, *selected.env_from)])
                    + "\nSynchronize current UTF-8 source snapshots: " + json.dumps(listing, ensure_ascii=False)
                    + ("\nQuery: " + json.dumps(query, ensure_ascii=False) if workspace else "\nPosition: " + json.dumps(point))
                    + f"; idle {selected.timeout:g}s; total {selected.deadline:g}s."
                    + "\nExternal server has current-user filesystem/network authority; NOT an OS sandbox and may act before queries or while idle."
                    + "\nOnly semantic queries and document synchronization; server-driven edits/commands are denied, configuration replies are empty. No installation, automatic restart or replay. Results are observations, not approval, read_id or fixed acceptance.")
            if self.approve(redact(text, self.config.secrets)) is not True:
                return {"approved": False, "server_contacted": False}
            check()
            recheck()
            for document in prepared.values():
                if self.document(document["path"])["sha256"] != document["sha256"]:
                    raise LspError("LSP source changed during approval; no query sent")
            budget = Budget(selected, check)
            with self.lock:
                check()
                if client is None:
                    client = Client(selected, directory, environment, self.config.workdir.as_uri(), check)
                    self.clients[selected.name] = client
            try:
                if not client.capabilities:
                    client.initialize(budget, require_sync=bool(prepared))
                for document in prepared.values():
                    client.sync(document, suffixes.get(Path(document["path"]).suffix.lower(), "plaintext"), budget)
                params = {"query": query} if workspace else {"textDocument": {"uri": primary["uri"]}}
                if point is not None:
                    params["position"] = point
                if action == "references":
                    params["context"] = {"includeDeclaration": True}
                if action == "diagnostics":
                    if client.capabilities.get("diagnosticProvider") not in (None, False):
                        raw = client.exchange("textDocument/diagnostic", params, budget)
                        if not isinstance(raw, dict) or raw.get("kind") != "full" or not isinstance(raw.get("items"), list):
                            raise LspError("LSP pull diagnostics must return a full report without replaying old result IDs")
                        raw = {"items": raw["items"], "version": client.documents[primary["uri"]]["version"], "pull": True}
                    else:
                        raw = client.pushed_diagnostics(primary["uri"], budget)
                else:
                    method, capability = ("workspace/symbol", "workspaceSymbolProvider") if workspace else METHODS[action]
                    provider = client.capabilities.get(capability)
                    if provider is not True and not isinstance(provider, dict):
                        raise LspError("language server does not advertise this semantic operation")
                    raw = client.exchange(method, params, budget)
                budget.check()
                for document in prepared.values():
                    if self.document(document["path"])["sha256"] != document["sha256"]:
                        raise LspError("LSP source changed during the query; stale results discarded")
                value, paths = self._normalize(action, raw, primary, check=budget.check)
                budget.check()
                value.update(server=selected.name, action=action, not_authorization=True, not_acceptance=True)
                if workspace:
                    value.update(query=query, synchronized_documents=[{"path": row["path"], "sha256": row["sha256"],
                        "version": client.documents[row["uri"]]["version"]} for row in prepared.values()])
                else:
                    value["source"] = {"path": path, "sha256": primary["sha256"], "version": client.documents[primary["uri"]]["version"]}
                return self._save(value, tuple(dict.fromkeys((*[row["path"] for row in prepared.values()], *paths))), budget.check, approved=True)
            except BaseException:
                with self.lock:
                    if self.clients.get(selected.name) is client:
                        try:
                            client.close()
                        finally:
                            if client.closed:
                                self.clients.pop(selected.name, None)
                raise
        finally:
            self.operation.release()

    def _normalize(self, action, raw, primary, *, check=None):
        check = check or self.check
        paths, omitted = set(), 0
        docs = {primary["path"]: primary} if primary is not None else {}
        def doc(path):
            check()
            if path not in docs:
                if len(docs) >= 64:
                    raise LspError("LSP target document limit exceeded")
                docs[path] = self.document(path)
            return docs[path]
        def place(uri, range):
            path = self.path_from_uri(uri)
            current = doc(path)
            result = {"path": path, **location_range(current["text"], range), "source_sha256": current["sha256"]}
            paths.add(path)
            return result
        if action == "hover":
            if raw is None:
                return {"available": False, "content": ""}, paths
            if not isinstance(raw, dict):
                raise LspError("invalid LSP hover result")
            contents = raw.get("contents")
            rows = contents if isinstance(contents, list) else [contents]
            if len(rows) > 32:
                raise LspError("LSP hover exceeds 32 content parts")
            output = []
            for row in rows:
                if isinstance(row, str):
                    output.append(row)
                elif isinstance(row, dict) and isinstance(row.get("value"), str):
                    output.append(row["value"])
                else:
                    raise LspError("invalid LSP hover content")
            return {"available": True, "content": "\n".join(output), "format": "external text; never rendered/executed"}, paths
        if action == "diagnostics":
            if raw is None:
                return {"available": False, "diagnostics": [], "version_confirmed": False, "reason": "no diagnostic report observed; absence is not a clean check"}, paths
            if not isinstance(raw, dict) or not isinstance(raw.get("items"), list) or len(raw["items"]) > 256:
                raise LspError("invalid or oversized LSP diagnostic report")
            result = []
            for row in raw["items"]:
                if not isinstance(row, dict) or not isinstance(row.get("message"), str):
                    raise LspError("invalid LSP diagnostic")
                try:
                    where = place(primary["uri"], row.get("range"))
                    result.append({**where, "message": row["message"],
                                   "severity": row.get("severity") if type(row.get("severity")) is int and 1 <= row["severity"] <= 4 else None,
                                   "code": row.get("code") if type(row.get("code")) in (int, str) else None})
                except (OSError, ValueError):
                    omitted += 1
            return {"available": True, "diagnostics": result, "version_confirmed": raw.get("version") is not None,
                    "mode": "pull" if raw.get("pull") else "push", "omitted": omitted, "scope_limited": True}, paths
        if action == "workspace_symbols" and raw is not None and not isinstance(raw, list):
            raise LspError("workspace symbols must be an array or null")
        rows = [] if raw is None else raw if isinstance(raw, list) else [raw]
        if len(rows) > 512:
            raise LspError("LSP result exceeds 512 entries")
        result, queue = [], [(row, None, 0) for row in rows]
        while queue:
            check()
            row, parent, depth = queue.pop(0)
            if len(result) + len(queue) > 512 or depth > 24:
                raise LspError("LSP symbol tree exceeds bounds")
            if not isinstance(row, dict):
                omitted += 1
                continue
            try:
                if action == "workspace_symbols" and not isinstance(row.get("location"), dict):
                    raise LspError("workspace symbol requires a location")
                if action == "symbols" and "location" not in row:
                    where = place(primary["uri"], row.get("selectionRange", row.get("range")))
                else:
                    location = row.get("location", row)
                    where = place(location.get("targetUri", location.get("uri")), location.get("targetSelectionRange", location.get("range")))
                if action in {"symbols", "workspace_symbols"}:
                    if not isinstance(row.get("name"), str) or len(row["name"]) > 1024:
                        raise LspError("invalid symbol name")
                    kind = row.get("kind")
                    where.update(name=row["name"], kind=kind if type(kind) is int and 1 <= kind <= 26 else None)
                    if action == "workspace_symbols":
                        container = row.get("containerName")
                        if container is not None and (not isinstance(container, str) or len(container) > 1024):
                            raise LspError("invalid workspace symbol container")
                        where["container"] = container
                    else:
                        where["parent"] = parent
                        children = row.get("children", [])
                        if not isinstance(children, list):
                            raise LspError("invalid LSP symbol children")
                        queue.extend((child, row["name"], depth + 1) for child in children)
                result.append(where)
            except (OSError, ValueError, AttributeError):
                omitted += 1
        return {"results": result, "omitted": omitted, "scope_limited": True, "complete_project_index": False}, paths

    def _save(self, value, paths, check, *, approved=None):
        raw = json.dumps(redact(value, self.config.secrets), ensure_ascii=False).encode("utf-8")
        if len(raw) > MAX_FRAME:
            raise LspError("normalized LSP result exceeds 1 MiB")
        with self.lock:
            check()
            while len(self.results) >= 8:
                self.results.popitem(last=False)
            identity = "lsp-" + uuid.uuid4().hex
            self.results[identity] = (raw, paths)
        try:
            return self._page(identity, 0, check, approved=approved)
        except BaseException:
            with self.lock:
                self.results.pop(identity, None)
            raise

    def _page(self, identity, offset, check, *, approved=None):
        check()
        if not isinstance(identity, str) or type(offset) is not int or offset < 0:
            raise LspError("invalid LSP result handle or UTF-8 offset")
        with self.lock:
            check()
            if identity not in self.results:
                raise LspError("LSP result unavailable in this conversation; no request is replayed")
            raw, paths = self.results[identity]
        try:
            for path in paths:
                check()
                self.document(path)
            if redact(raw.decode("utf-8"), self.config.secrets).encode("utf-8") != raw:
                raise LspError("LSP result expired after credential policy changed")
        except BaseException:
            with self.lock:
                self.results.pop(identity, None)
            raise
        if offset > len(raw):
            raise LspError("LSP result offset exceeds retained data")
        try:
            text = raw[offset:].decode("utf-8")
        except UnicodeError:
            raise LspError("LSP result offset must be a UTF-8 boundary") from None
        result = {"result_id": identity, "offset": offset, "total_bytes": len(raw), "content": "", "next_offset": None,
                  "complete": False, "historical": approved is None, "not_authorization": True, "untrusted": True}
        if approved is not None:
            result["approved"] = approved
        low, high = 0, len(text)
        while low < high:
            check()
            count = (low + high + 1) // 2
            end = offset + len(text[:count].encode("utf-8"))
            result.update(content=text[:count], next_offset=end if end < len(raw) else None, complete=end == len(raw))
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) <= min(self.config.max_output, 16384):
                low = count
            else:
                high = count - 1
        end = offset + len(text[:low].encode("utf-8"))
        result.update(content=text[:low], next_offset=end if end < len(raw) else None, complete=end == len(raw))
        if text and not low or len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > min(self.config.max_output, 16384):
            raise LspError("LSP page metadata exceeds max_output")
        check()
        return result


def local_arguments(text):
    parts = shlex.split(text)
    if not parts:
        return {"action": "status"}
    if parts[0] == "result" and len(parts) in (2, 3):
        return {"action": "result", "result_id": parts[1], "offset": int(parts[2]) if len(parts) == 3 else 0}
    if parts[0] == "disconnect" and len(parts) == 2:
        return {"action": "disconnect", "server": parts[1]}
    if parts[0] == "workspace_symbols" and len(parts) == 3:
        return {"action": parts[0], "server": parts[1], "query": parts[2]}
    if parts[0] in {"symbols", "diagnostics"} and len(parts) == 3:
        return {"action": parts[0], "server": parts[1], "path": parts[2]}
    if parts[0] in {"definition", "references", "implementation", "hover"} and len(parts) == 5:
        return {"action": parts[0], "server": parts[1], "path": parts[2], "line": int(parts[3]), "column": int(parts[4])}
    raise LspError("Use /lsp, /lsp ACTION SERVER PATH [LINE COLUMN], /lsp workspace_symbols SERVER QUERY, /lsp result ID [OFFSET], or /lsp disconnect SERVER")
