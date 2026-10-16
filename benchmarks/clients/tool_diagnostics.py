"""Numeric tool diagnostics; raw arguments, paths and output stay private."""

import hashlib
import json
from pathlib import Path


class ToolDiagnostics:
    limit = 128

    def __init__(self):
        self._recent = {}

    def record(self, name, arguments, result):
        summary = {"error": "error" in result}
        for field in ("approved", "timed_out", "output_limited", "truncated", "line_truncated"):
            if type(result.get(field)) is bool:
                summary[field] = result[field]
        for field in ("returncode", "files_read", "bytes_read", "entries_scanned", "skipped_files"):
            if type(result.get(field)) is int:
                summary[field] = result[field]
        if name == "search" and isinstance(result.get("matches"), list):
            summary["matched_lines"] = len(result["matches"])
            if isinstance(arguments, dict) and type(arguments.get("regex", False)) is bool:
                summary["regex"] = arguments.get("regex", False)
        if name == "read_file" and isinstance(result.get("content"), str):
            summary["content_bytes"] = len(result["content"].encode("utf-8"))
        if name == "list_files" and isinstance(result.get("entries"), list):
            summary["listed_entries"] = len(result["entries"])
        if name == "project_map" and isinstance(result.get("files"), list):
            summary["mapped_files"] = len(result["files"])
        if name == "update_plan" and result.get("plan_updated") is True and isinstance(result.get("plan"), list):
            summary["planned_steps"] = len(result["plan"])
            for status in ("pending", "in_progress", "completed"):
                summary["plan_" + status] = sum(isinstance(step, dict) and step.get("status") == status for step in result["plan"])
        if name in {"read_file", "search", "list_files", "project_map"} and not summary["error"]:
            # Equality of bounded tool output does not imply a complete or
            # unchanged workspace. Never cache or skip the actual inspection.
            digest = hashlib.sha256(json.dumps([name, arguments, result], sort_keys=True,
                                               ensure_ascii=True).encode()).digest()
            repeats = self._recent.pop(digest, 0)
            summary["same_output_repeats"] = repeats
            self._recent[digest] = repeats + 1
            if len(self._recent) > self.limit:
                self._recent.pop(next(iter(self._recent)))
        return summary

    def metadata(self):
        return {"source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "recent_inspection_limit": self.limit,
                "scope": "numeric tool results; repeats require identical arguments and output within the retained window; no inspection skipped"}
