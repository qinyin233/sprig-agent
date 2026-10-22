"""Disposable stdlib-only MCP process. Never use real credentials or external models."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

root = Path(sys.argv[1])
mode_path = root / "mode"
log_path = root / "rpc.jsonl"
queue_in = queue.Queue()


def mode():
    return mode_path.read_text(encoding="utf-8") if mode_path.exists() else "normal"


def log(value):
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")


def send(value):
    raw = json.dumps(value, ensure_ascii=False).encode() + b"\n"
    if mode() == "fragment":
        for byte in raw:
            os.write(1, bytes([byte]))
    else:
        sys.stdout.buffer.write(raw)
        sys.stdout.buffer.flush()


def reader():
    for line in sys.stdin.buffer:
        queue_in.put(line)
    queue_in.put(None)


log({"started": os.getpid(), "cwd": os.getcwd(), "own": os.environ.get("MCP_FIXTURE_TOKEN"),
     "model_key_present": "OPENAI_API_KEY" in os.environ, "unrelated_present": "UNRELATED_FIXTURE_KEY" in os.environ,
     "command_override_present": "COMMAND_OVERRIDE" in os.environ})
if mode() in ("child", "parent_exit"):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    log({"child": child.pid})
if mode() == "block_input":
    request = json.loads(sys.stdin.buffer.readline())
    log(request)
    send({"jsonrpc": "2.0", "id": request["id"], "result": {
        "supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}},
        "serverInfo": {"name": "fixture", "version": "fixture"}}})
    while True:
        time.sleep(1)
threading.Thread(target=reader, daemon=True).start()
notified = False
while True:
    active = mode()
    if active == "changed" and not notified:
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        (root / "notified").write_text("sent")
        notified = True
    if active == "delayed" and not notified:
        os.write(1, b'{"jsonrpc":"2.0","method":"notifications/message","params":{"message":"')
        (root / "partial-notification").write_text("sent")
        time.sleep(.25)
        os.write(1, b'complete"}}\n')
        notified = True
    try:
        line = queue_in.get(timeout=.01)
    except queue.Empty:
        continue
    if line is None:
        break
    active = mode()
    if active == "delayed":
        time.sleep(.25)
    request = json.loads(line)
    log(request)
    if active == "marker_wait" and "id" in request:
        Path("LOCAL_EFFECT").write_text("CREATED")
        continue
    if "id" not in request:
        continue
    if active == "waiting":
        continue
    if active == "invalid":
        os.write(1, b"not JSON\n")
        continue
    if active == "partial":
        os.write(1, b'{"jsonrpc":"2.0"')
        continue
    if active == "request":
        send({"jsonrpc": "2.0", "id": "server-sampling", "method": "sampling/createMessage", "params": {}})
        continue
    if active == "flood":
        while True:
            os.write(1, b"x" * 4096)
    if active == "stderr_flood":
        while True:
            os.write(2, b"PRIVATE_STDERR_FIXTURE" * 4096)
    method = request["method"]
    if method in ("server/discover", "initialize"):
        result = {"capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                  "serverInfo": {"name": "stdio fixture", "version": "fixture"}}
        if method == "server/discover":
            result["supportedVersions"] = ["2026-07-28"]
        else:
            result["protocolVersion"] = request["params"]["protocolVersion"]
    elif method == "tools/list":
        result = {"tools": [{"name": name, "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                                                          "required": ["text"], "additionalProperties": False}}
                            for name in ("echo", "write_marker")]}
    elif method == "tools/call":
        if request["params"]["name"] == "write_marker":
            Path("MCP_MARKER").write_text("CREATED")
        result = {"content": [{"type": "text", "text": request["params"]["arguments"]["text"]}]}
    elif method == "resources/list":
        result = {"resources": [{"uri": "fixture://text", "name": "fixture"}]}
    elif method == "resources/templates/list":
        result = {"resourceTemplates": [{"uriTemplate": "fixture://{name}", "name": "fixture"}]}
    elif method == "resources/read":
        result = {"contents": [{"uri": request["params"]["uri"], "text": "RESOURCE_TEXT"}]}
    elif method == "prompts/list":
        result = {"prompts": [{"name": "fixture"}]}
    elif method == "prompts/get":
        result = {"messages": [{"role": "user", "content": {"type": "text", "text": "EXTERNAL_PROMPT"}}]}
    else:
        result = {}
    os.write(2, b"private diagnostics are not protocol output\n")
    send({"jsonrpc": "2.0", "id": "wrong" if active == "bad_id" else request["id"], "result": result})
    if active == "parent_exit":
        os._exit(0)
    if active == "duplicate":
        send({"jsonrpc": "2.0", "id": request["id"], "result": result})
