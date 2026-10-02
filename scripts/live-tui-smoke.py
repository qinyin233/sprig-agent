"""Optional real-provider TUI test; only exact fixture edits/checks are approved."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sprig_agent.config import Config, redact, terminal_text
from sprig_agent.tui import ApprovalScreen, SprigApp
from textual.widgets import TextArea


async def smoke(snapshot=None, steer=False):
    key = os.environ.get("SPRIG_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    if not key:
        raise ValueError("set SPRIG_API_KEY or OPENAI_API_KEY")
    with tempfile.TemporaryDirectory(prefix="sprig-tui-smoke-") as temporary:
        parent = Path(temporary)
        root = parent / "workspace"
        root.mkdir()
        initial = "def add(a, b):\n    return a - b\n"
        expected = initial.replace("a - b", "a + b")
        (root / "calc.py").write_text(initial, encoding="utf-8", newline="")
        check = (sys.executable, "-c", "from calc import add; assert add(2, 3) == 5; print('ACCEPTANCE_OK')")
        config = Config(root, os.environ.get("SPRIG_MODEL", "agnes-3.0-flash"),
                        os.environ.get("SPRIG_BASE_URL", "https://apihub.agnes-ai.com/v1"), key,
                        stream=True, checks=(check,), max_steps=8, timeout=30)
        task = ("Read calc.py, fix add(a,b) using edit_file so it returns a+b. Keep its formatting. "
                "Do not run commands yourself; the application runs the fixed acceptance command after you finish. "
                "Finish with a short sentence.")
        app = SprigApp(config, session_dir=parent / "sessions", save_session="smoke")
        original = app.tools.execute

        def guarded(name, arguments):
            if name == "edit_file" and not (
                isinstance(arguments, dict) and set(arguments) == {"path", "old", "new"}
                and arguments.get("path") == "calc.py" and isinstance(arguments.get("old"), str)
                and arguments["old"] and isinstance(arguments.get("new"), str)
                and initial.count(arguments["old"]) == 1
                and initial.replace(arguments["old"], arguments["new"], 1) == expected):
                return {"approved": False, "changed": False}
            if name == "run" and arguments != {"argv": list(check)}:
                return {"approved": False}
            return original(name, arguments)

        app.tools.execute = guarded
        approvals, steering_sent = 0, False
        guidance = ("Keep the original repair target and the fixed acceptance check. The earlier pending action "
                    "was superseded by this guidance. Inspect calc.py and request fresh approval to repair it. "
                    "Do not run commands yourself. Include STEERING_OK in your final summary.")
        try:
            async with app.run_test(size=(120, 40)) as pilot:
                app.query_one("#task", TextArea).load_text(task)
                await pilot.press("enter")
                deadline = time.monotonic() + 150
                while app.busy and time.monotonic() < deadline:
                    if isinstance(app.screen, ApprovalScreen) and app.screen.is_mounted:
                        await pilot.pause()
                        if isinstance(app.screen, ApprovalScreen):
                            if steer and not steering_sent:
                                editor = app.screen.query_one("#approval-task", TextArea)
                                editor.focus()
                                editor.load_text(guidance)
                                await pilot.press("enter")
                                steering_sent = True
                            else:
                                await pilot.click("#approve")
                                approvals += 1
                    await pilot.pause(0.05)
                if app.busy:
                    app.action_cancel()
                    raise TimeoutError("TUI API smoke test exceeded its deadline")
                restored = app.store.load("smoke")
                success = (app.last_status == 0 and (root / "calc.py").read_text(encoding="utf-8") == expected
                           and restored == app.messages and "All user checks passed" in app.transcript)
                if steer:
                    success = success and steering_sent and any(
                        m["role"] == "assistant" and "STEERING_OK" in (m.get("content") or "") for m in restored)
                    success = success and any(m["role"] == "user" and m["content"] == guidance for m in restored)
                if snapshot:
                    rendered = app.export_screenshot()
                    if key in rendered:
                        raise ValueError("snapshot contains the configured key")
                    Path(snapshot).write_text(rendered, encoding="utf-8")
                print(json.dumps({"model": config.model, "tui_success": success, "stream": config.stream,
                                  "enter_submission": True, "steering_sent": steering_sent,
                                  "ui_approvals": approvals, "session_roundtrip": restored == app.messages}))
                return success
        finally:
            app.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, help="optional SVG of the actual headless UI render")
    parser.add_argument("--steer", action="store_true", help="send guidance with Enter during the first pending approval")
    args = parser.parse_args()
    try:
        return 0 if asyncio.run(smoke(args.snapshot, args.steer)) else 1
    except Exception as error:
        key = os.environ.get("SPRIG_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
        print(terminal_text(redact(str(error), key)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
