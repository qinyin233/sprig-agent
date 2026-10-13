"""Optional live API test. Uploads only a disposable fixture; takes key from env.

The test permits one exact fixture edit and one exact acceptance argv. Every other
write or command is denied. This whitelist is a test harness, not an agent option.
"""

import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent.agent import Agent
from sprig_agent.config import Config, environment_api_key
from sprig_agent.provider import Provider, ProviderError
from sprig_agent.session import SessionStore
from sprig_agent.tools import Tools


def smoke(stream):
    key = environment_api_key(os.environ.get("SPRIG_BASE_URL", "https://apihub.agnes-ai.com/v1"), os.environ)
    if not key:
        raise ValueError("set SPRIG_API_KEY or OPENAI_API_KEY")
    with tempfile.TemporaryDirectory(prefix="sprig-smoke-") as temporary:
        parent = Path(temporary)
        root = parent / "workspace"
        root.mkdir()
        initial = "def add(a, b):\n    return a - b\n"
        expected = initial.replace("a - b", "a + b")
        (root / "calc.py").write_text(initial, encoding="utf-8", newline="")
        check = (sys.executable, "-c", "from calc import add; assert add(2, 3) == 5; print('ACCEPTANCE_OK')")
        config = Config(root, os.environ.get("SPRIG_MODEL", "agnes-3.0-flash"),
                        os.environ.get("SPRIG_BASE_URL", "https://apihub.agnes-ai.com/v1"),
                        key, max_steps=8, timeout=30, stream=stream, checks=(check,))
        seen, chunks = [], []

        class FixtureTools(Tools):
            def execute(self, name, arguments):
                # Restrict before tool approval, using structured args rather than prompt text.
                if name == "edit_file" and not (
                    isinstance(arguments, dict) and arguments.get("path") == "calc.py"
                    and set(arguments) == {"path", "old", "new"}
                    and isinstance(arguments.get("old"), str) and arguments["old"]
                    and isinstance(arguments.get("new"), str)
                    and initial.count(arguments["old"]) == 1
                    and initial.replace(arguments["old"], arguments["new"], 1) == expected):
                    return {"approved": False, "changed": False}
                if name == "run" and arguments != {"argv": list(check)}:
                    return {"approved": False}
                result = super().execute(name, arguments)
                seen.append({"tool": name, "approved": result.get("approved"), "error": "error" in result,
                             "returncode": result.get("returncode"), "changed": result.get("changed")})
                return result

        store = SessionStore(parent / "sessions", config)
        tools = FixtureTools(config, lambda _: True)
        try:
            task = ("In this disposable fixture, read calc.py, then fix add(a,b) using edit_file so it returns a+b. "
                    "Keep the existing formatting. Do not run commands yourself; the application runs the fixed "
                    "acceptance check after you finish. Finish with a short sentence.")
            agent = Agent(config, Provider(config), tools, lambda _: None,
                          lambda history: store.save("smoke", history), chunks.append)
            messages, complete = agent.run(task)
            restored = store.load("smoke")
            success = complete and (root / "calc.py").read_text(encoding="utf-8") == expected and restored == messages
            print(json.dumps({"model": config.model, "stream": stream, "success": success,
                              "text_chunks": len(chunks), "session_roundtrip": restored == messages, "tools": seen}))
            return success
        finally:
            tools.close()
            store.close()


def main():
    try:
        return 0 if all([smoke(False), smoke(True)]) else 1
    except (OSError, ValueError, ProviderError) as error:
        # Provider and tool errors are sanitized; do not print raw responses or env.
        from sprig_agent.config import redact, terminal_text
        key = environment_api_key(os.environ.get("SPRIG_BASE_URL", "https://apihub.agnes-ai.com/v1"), os.environ)
        print(terminal_text(redact(str(error), key)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
