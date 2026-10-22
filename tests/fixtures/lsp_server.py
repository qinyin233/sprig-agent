"""Disposable Content-Length LSP peer; fixed responses are not a language engine."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

state = Path(sys.argv[1])
documents = {}


def mode():
    path = state / "mode"
    return path.read_text() if path.exists() else "normal"


def log(value):
    with (state / "rpc.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def send(value):
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    frame = b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw
    if mode() == "fragment":
        for byte in frame:
            os.write(1, bytes([byte]))
    else:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()


def read():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        key, value = line.decode("ascii").split(":", 1)
        headers[key.lower()] = value.strip()
    return json.loads(sys.stdin.buffer.read(int(headers["content-length"])))


def span():
    return {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 8}}


def publish(uri):
    active = mode()
    if active in {"no_diagnostics", "pull"}:
        return
    version = documents[uri]["version"]
    if active == "stale_diagnostics":
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {
            "uri": uri, "version": version - 1, "diagnostics": [{"range": span(), "message": "STALE_REPORT"}]}})
    params = {"uri": uri, "diagnostics": [{"range": span(), "message": "fixture diagnostic", "severity": 1}]}
    if active != "versionless":
        params["version"] = version
    send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": params})


log({"started": os.getpid(), "cwd": os.getcwd(), "own": os.environ.get("LSP_FIXTURE_TOKEN"),
     "model_key_present": "OPENAI_API_KEY" in os.environ, "unrelated_present": "UNRELATED_FIXTURE_KEY" in os.environ,
     "command_override_present": "COMMAND_OVERRIDE" in os.environ})
if mode() == "child":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    log({"child": child.pid})
while True:
    request = read()
    if request is None:
        break
    log(request)
    active = mode()
    method, params = request.get("method"), request.get("params", {})
    if method == "initialize":
        capabilities = {"textDocumentSync": {"openClose": True, "change": 1 if active == "full_sync" else 2},
                        "definitionProvider": True, "referencesProvider": True, "implementationProvider": True,
                        "hoverProvider": True, "documentSymbolProvider": True}
        if active == "pull":
            capabilities["diagnosticProvider"] = {}
        if active == "utf8":
            capabilities["positionEncoding"] = "utf-8"
        if active == "no_sync":
            capabilities["textDocumentSync"] = 0
        if active == "no_capability":
            capabilities.pop("definitionProvider")
        result = {"capabilities": capabilities}
    elif method in {"textDocument/didOpen", "textDocument/didChange"}:
        doc = params["textDocument"]
        if method == "textDocument/didOpen":
            documents[doc["uri"]] = doc
        else:
            documents[doc["uri"]] = {**doc, "text": params["contentChanges"][0]["text"]}
        publish(doc["uri"])
        continue
    elif method == "textDocument/didClose":
        documents.pop(params["textDocument"]["uri"], None)
        continue
    elif method and method.startswith("textDocument/"):
        if active == "waiting":
            continue
        if active == "pause":
            while not (state / "release").exists():
                time.sleep(.005)
        if active == "drip":
            while True:
                send({"jsonrpc": "2.0", "method": "window/logMessage", "params": {"message": "activity"}})
                time.sleep(.02)
        if active in {"bad_header", "duplicate_json", "mismatch", "error"}:
            if active == "bad_header":
                os.write(1, b"Content-Length: 1048577\r\n\r\n")
            elif active == "duplicate_json":
                raw = b'{"jsonrpc":"2.0","id":2,"id":2,"result":null}'
                os.write(1, b"Content-Length: " + str(len(raw)).encode() + b"\r\n\r\n" + raw)
            elif active == "mismatch":
                send({"jsonrpc": "2.0", "id": request["id"] + 1, "result": None})
            else:
                send({"jsonrpc": "2.0", "id": request["id"], "error": {"code": -32603, "message": "PRIVATE_SERVER_ERROR"}})
            continue
        if active == "requests":
            for index, name in enumerate(("workspace/applyEdit", "workspace/executeCommand", "client/registerCapability", "workspace/configuration")):
                send({"jsonrpc": "2.0", "id": "server-" + str(index), "method": name, "params": {"items": [{"section": "secret"}]}})
        uri = params["textDocument"]["uri"]
        if method == "textDocument/hover":
            result = {"contents": {"kind": "markdown", "value": "中文🎍" * (5000 if active == "large_hover" else 1) + os.environ.get("LSP_FIXTURE_TOKEN", "")}}
        elif method == "textDocument/documentSymbol":
            result = [{"name": "root", "kind": 3, "range": span(), "selectionRange": span(), "children": [
                {"name": "symbol", "kind": 12, "range": span(), "selectionRange": span()}]}]
        elif method == "textDocument/diagnostic":
            result = {"kind": "full", "items": [{"range": span(), "message": "pulled diagnostic", "severity": 2}]}
        elif method == "textDocument/implementation":
            result = [{"targetUri": uri, "targetRange": span(), "targetSelectionRange": span()}]
        else:
            result = [{"uri": uri, "range": span()}]
            if active == "private_targets":
                extra = json.loads((state / "targets.json").read_text())
                result.extend({"uri": target, "range": span()} for target in extra)
    else:
        continue
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})
