"""Opt-in semantic check in a disposable C++ project; no model or downloads."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import tempfile

from sprig_agent.config import Config
from sprig_agent.lsp_config import LspServer
from sprig_agent.tools import Tools


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clangd", required=True, type=Path, help="explicit already installed clangd executable")
    parser.add_argument("--approve-disposable", action="store_true", help="approve each listed query on this script's temporary fixture")
    parser.add_argument("--workspace-symbols", action="store_true", help="also open a second fixture file and query project-wide symbols")
    args = parser.parse_args()
    executable = args.clangd.resolve(strict=True)
    if not executable.is_file() or not args.approve_disposable:
        parser.error("requires an installed executable and explicit --approve-disposable")
    source = ('namespace left { int choose(int value) { return value + 1; } }\n'
              'namespace right { int choose(int value) { return value + 2; } }\n'
              'int main() { return left::choose(2); }\n')
    with tempfile.TemporaryDirectory(prefix="sprig-lsp-real-") as temporary:
        root = Path(temporary) / "目录 space"
        root.mkdir()
        (root / "compile_flags.txt").write_text("-std=c++17\n", encoding="utf-8")
        path = root / "main.cpp"
        path.write_text(source, encoding="utf-8")
        command = (str(executable), "--log=error", "--background-index=false", "--enable-config=false")
        server = LspServer("cpp", command, ((".cpp", "cpp"),), timeout=10, deadline=30)
        approvals = []
        def approve(text):
            approvals.append(text)
            return True  # Only this disposable, hardcoded fixture; explicit CLI opt-in above.
        with closing(Tools(Config(root, "offline-fixture", lsp_servers=(server,), max_output=16000), approve)) as tools:
            observed = {}
            for action in ("definition", "references", "hover", "symbols", "diagnostics"):
                arguments = {"action": action, "server": "cpp", "path": "main.cpp"}
                if action in {"definition", "references", "hover"}:
                    arguments.update(line=3, column=source.splitlines()[2].index("choose") + 1)
                page = tools.execute("lsp", arguments)
                assert "error" not in page and page["complete"], page
                observed[action] = json.loads(page["content"])
            assert [row["start_line"] for row in observed["definition"]["results"]] == [1], observed
            assert {row["start_line"] for row in observed["references"]["results"]} == {1, 3}, observed
            assert "left" in observed["hover"]["content"], observed
            assert any(row.get("name") == "choose" and row.get("parent") == "left" for row in observed["symbols"]["results"]), observed
            assert observed["diagnostics"]["available"] and not observed["diagnostics"]["diagnostics"], observed
            workspace = None
            if args.workspace_symbols:
                other = root / "other.cpp"
                other.write_text('namespace third { int choose(int value) { return value + 3; } }\n', encoding="utf-8")
                page = tools.execute("lsp", {"action": "symbols", "server": "cpp", "path": "other.cpp"})
                assert "error" not in page and page["complete"], page
                page = tools.execute("lsp", {"action": "workspace_symbols", "server": "cpp", "query": "choose"})
                assert "error" not in page and page["complete"], page
                symbols = json.loads(page["content"])
                assert {row["path"] for row in symbols["results"]} == {"main.cpp", "other.cpp"}, symbols
                assert len(symbols["results"]) == 3 and all(row["name"] == "choose" for row in symbols["results"]), symbols
                assert symbols["complete_project_index"] is False, symbols
                workspace = {"symbols": 3, "files": 2, "containers": [row["container"] for row in symbols["results"]],
                             "scope_limited": symbols["scope_limited"], "complete_project_index": False}
            path.write_text(source.replace("left::choose(2)", 'left::choose("bad")'), encoding="utf-8")
            page = tools.execute("lsp", {"action": "diagnostics", "server": "cpp", "path": "main.cpp"})
            assert "error" not in page and page["complete"], page
            changed = json.loads(page["content"])
            assert changed["available"] and changed["version_confirmed"] and changed["source"]["version"] == 2, changed
            assert any(row["severity"] == 1 and row["start_line"] == 3 for row in changed["diagnostics"]), changed
            assert len(approvals) == (8 if args.workspace_symbols else 6)
            result = {"passed": True, "executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                      "approved_queries": len(approvals), "definition_lines": [1], "reference_lines": [1, 3],
                      "changed_document_version": 2, "changed_diagnostics": changed["diagnostics"],
                      "no_model_requests": True, "scope": "disposable C++ fixture; no general agent performance claim"}
            if workspace is not None:
                result["workspace_symbols"] = workspace
        print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
