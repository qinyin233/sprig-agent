"""Approval and acceptance boundary for the upstream mini-SWE-agent loop."""

import json
import os
from pathlib import Path
import platform
import shutil
import sys
import time

from sprig_agent.config import redact
from sprig_agent.tools import Tools, ToolError

MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"


def bash_path():
    if os.name == "nt":
        # Prefer Git Bash to the Windows WSL launcher: no Linux distro required.
        git = shutil.which("git")
        if git:
            for parent in Path(git).resolve().parents:
                candidate = parent / "bin" / "bash.exe"
                if candidate.is_file():
                    return str(candidate)
        raise ValueError("native Windows reference runs require Git Bash")
    bash = shutil.which("bash")
    if not bash:
        raise ValueError("reference runs require bash")
    return str(Path(bash).resolve())


class ApprovedEnvironment:
    def __init__(self, config, acceptance, intact, confirm, submitted, *, bash=None):
        self.config, self.acceptance, self.intact = config, acceptance, intact
        self.confirm, self.submitted = confirm, submitted
        self.bash = bash or bash_path()
        self.approvals = {"requests": 0, "accepted": 0, "rejected": 0, "wait_ms": 0.0}
        self.records = []
        self.acceptance_passed = False
        self.tools = Tools(config, self._approve)

    def _valid(self):
        if not self.intact():
            raise ValueError("benchmark verifier changed")

    def _approve(self, description):
        self._valid()
        started = time.perf_counter()
        try:
            answer = self.confirm(redact(description, self.config.api_key)) is True
        finally:
            self.approvals["requests"] += 1
            self.approvals["wait_ms"] += (time.perf_counter() - started) * 1000
        self.approvals["accepted" if answer else "rejected"] += 1
        self._valid()
        return answer

    def _run(self, argv, kind, timeout=None):
        self._valid()
        started, before = time.perf_counter(), self.approvals["wait_ms"]
        try:
            result = redact(self.tools.run(argv, timeout=timeout), self.config.api_key)
        except ToolError:
            result = {"approved": False, "error": "invalid command rejected"}
        finally:
            self.records.append({"tool": kind, "active_ms": round(max(0, (time.perf_counter() - started) * 1000 -
                                                      (self.approvals["wait_ms"] - before)), 3), "result_bytes": None})
        self.records[-1]["result_bytes"] = len(json.dumps(result, ensure_ascii=True).encode())
        self.records[-1].update({name: result.get(name) for name in ("approved", "returncode", "timed_out", "output_limited")})
        self._valid()
        return result

    @staticmethod
    def _passed(result):
        return (result.get("approved") is True and result.get("returncode") == 0
                and not result.get("timed_out") and not result.get("output_limited") and "error" not in result)

    def execute(self, action, cwd="", *, timeout=None):
        if (not isinstance(action, dict) or not isinstance(action.get("command"), str)
                or not action["command"] or len(action["command"]) > 60000 or "\0" in action["command"]
                or cwd not in ("", ".", str(self.config.workdir))):
            return {"returncode": -1, "output": "Invalid command or working directory rejected.", "exception_info": ""}
        relay = Path(__file__).with_name("bash_command.py")
        result = self._run([sys.executable, "-I", "-B", str(relay), self.bash, action["command"]], "bash", timeout)
        output = result.get("output", "")
        lines = output.lstrip().splitlines()
        # Preserve upstream completion semantics, then use the same external
        # verifier as Sprig. Failed acceptance goes back through mini's own loop.
        if self._passed(result) and lines and lines[0].strip() == MARKER:
            checked = self._run(list(self.acceptance), "acceptance")
            if checked.get("approved") is False:
                raise self.submitted({"role": "exit", "content": "", "extra": {"exit_status": "AcceptanceRejected"}})
            if self._passed(checked):
                self.acceptance_passed = True
                raise self.submitted({"role": "exit", "content": "", "extra": {"exit_status": "Submitted"}})
            return {"returncode": 1, "output": "Fixed acceptance failed; inspect and repair the workspace.\n" + checked.get("output", ""),
                    "exception_info": ""}
        problem = ("Command timed out" if result.get("timed_out") else
                   "Command output limit exceeded" if result.get("output_limited") else
                   "Action rejected" if result.get("approved") is False else "")
        return {"returncode": result.get("returncode", -1), "output": output, "exception_info": result.get("error", problem)}

    def get_template_vars(self, **kwargs):
        # Upstream LocalEnvironment includes os.environ in templates. Model
        # credentials belong only to the backend process, never to templates.
        return {**platform.uname()._asdict(), "cwd": str(self.config.workdir), **kwargs}

    def serialize(self):
        return {"info": {"environment_type": "sprig-benchmark-approved-bash", "raw_environment_saved": False}}

    def close(self):
        self.tools.close()
