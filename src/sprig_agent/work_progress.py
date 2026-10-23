"""Bounded per-invocation tool observations; advice, never task acceptance."""
import json

INSPECTIONS = frozenset({"list_files", "read_file", "search", "grep", "project_map"})
FILE_CHANGES = frozenset({"edit_file", "edit_lines", "file_action"})
APPROVAL_ACTIONS = FILE_CHANGES | {"run", "mcp", "web_fetch", "job", "lsp"}
UNCERTAIN = ("timed_out", "output_limited", "skipped", "cancelled", "interrupted", "outcome", "partial")


class WorkProgress:
    def __init__(self):
        self.inspection_calls = 0
        self.inspection_rounds = 0
        self.file_changes = 0
        self.approved_commands = 0
        self._last_inspection_step = None

    def reset_inspections(self):
        self.inspection_rounds = 0
        self._last_inspection_step = None

    def observe(self, step, name, result):
        if not isinstance(result, dict):
            return
        if name in APPROVAL_ACTIONS and result.get("approved") is True:
            # An approved attempt can have uncertain effects. Reset exploration
            # without inferring a file change or check success from its output.
            self.reset_inspections()
            if name == "run":
                self.approved_commands += 1
            if (name in FILE_CHANGES and result.get("changed") is True and "error" not in result
                    and not any(result.get(flag) for flag in UNCERTAIN)):
                self.file_changes += 1
            return
        if ("error" in result or result.get("approved") is False
                or any(result.get(flag) for flag in UNCERTAIN)):
            return
        if name in INSPECTIONS:
            self.inspection_calls += 1
            if step != self._last_inspection_step:
                self.inspection_rounds += 1
                self._last_inspection_step = step

    def snapshot(self):
        return {"source_inspection_calls": self.inspection_calls,
                "source_inspection_rounds_since_approved_action_or_guidance": self.inspection_rounds,
                "confirmed_file_tool_changes": self.file_changes,
                "approved_run_calls": self.approved_commands}

    def reminder(self, *, read_only):
        if self.inspection_rounds < 3:
            return ""
        return ("\nApplication observations of model-requested tools this invocation: " + json.dumps({**self.snapshot(), "read_only": read_only})
                + ". These counts do not prove sufficient evidence, unchanged files or task completion. "
                "Consider which specific uncertainty another inspection would resolve. For requested implementation, "
                "use sufficient evidence for a focused proposal and leave time to verify every stated behavior, "
                "including error and fallback cases. For review or read-only requests, finish findings when supported; "
                "no edit is required. Never bypass a rejection or treat these observations as action approval.")
