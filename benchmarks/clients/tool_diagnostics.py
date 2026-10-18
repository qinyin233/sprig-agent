"""Numeric and allowlisted status diagnostics; raw arguments and output stay private."""

import hashlib
import json
from pathlib import Path
import re


class ToolDiagnostics:
    limit = 128

    def __init__(self):
        self._recent = {}

    def record(self, name, arguments, result):
        summary = {"error": "error" in result}
        for field in ("approved", "timed_out", "output_limited", "truncated", "line_truncated", "range_complete"):
            if type(result.get(field)) is bool:
                summary[field] = result[field]
        for field in ("returncode", "files_read", "bytes_read", "entries_scanned", "skipped_files"):
            if type(result.get(field)) is int:
                summary[field] = result[field]
        if name == "search":
            if isinstance(result.get("matches"), list):
                summary["matched_lines"] = len(result["matches"])
            elif result.get("output_mode") in ("files", "count") and isinstance(result.get("files"), list):
                summary["output_mode"] = result["output_mode"]
                summary["matched_files"] = len(result["files"])
                if result["output_mode"] == "count" and all(isinstance(row, dict) and type(row.get("count")) is int
                                                          and row["count"] > 0 for row in result["files"]):
                    summary["matched_lines"] = sum(row["count"] for row in result["files"])
            if isinstance(arguments, dict) and type(arguments.get("regex", False)) is bool:
                summary["regex"] = arguments.get("regex", False)
        if name == "read_file" and isinstance(result.get("content"), str):
            summary["content_bytes"] = len(result["content"].encode("utf-8"))
            digest = result.get("sha256")
            read_id = result.get("read_id")
            summary["revision_available"] = isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            summary["read_id_available"] = (isinstance(read_id, str) and len(read_id) <= 64
                                            and re.fullmatch(r"r[0-9a-f]{12}-[1-9][0-9]*", read_id) is not None)
        failure = result.get("edit_failure")
        if name in {"edit_file", "edit_lines"} and isinstance(failure, dict):
            if isinstance(failure.get("reason"), str) and failure["reason"] in {"missing_revision", "invalid_revision", "unknown_read_id", "stale_revision"}:
                summary["edit_failure_reason"] = failure["reason"]
            if isinstance(failure.get("source"), str) and failure["source"] in {"sha256", "read_id"}:
                summary["revision_source"] = failure["source"]
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
            inspected = {key: value for key, value in result.items() if name != "read_file" or key != "read_id"}
            digest = hashlib.sha256(json.dumps([name, arguments, inspected], sort_keys=True,
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
                "scope": "numeric and fixed-status tool results; repeats require identical arguments/output apart from read_id within the retained window; no inspection skipped"}
