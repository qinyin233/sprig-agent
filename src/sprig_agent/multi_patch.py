"""Apply independent revision-pinned patches using ordinary edit preflight."""

import json
from pathlib import PurePosixPath

from .config import redact
from .edit_batch import MAX_BATCH_FILES, path_key
from .revisions import valid_read_id


def patch_set(files):
    """Validate the manifest without reading, resolving or modifying files."""
    if not isinstance(files, list) or not 2 <= len(files) <= MAX_BATCH_FILES:
        raise ValueError(f"patch_files takes 2–{MAX_BATCH_FILES} independent files")
    paths = set()
    for index, entry in enumerate(files, 1):
        if (not isinstance(entry, dict) or set(entry) != {"path", "read_id", "patch"}
                or not isinstance(entry["path"], str) or not 1 <= len(entry["path"]) <= 4096
                or not valid_read_id(entry["read_id"]) or not isinstance(entry["patch"], str) or not entry["patch"]):
            raise ValueError(f"patch file {index} requires a path, read_id and nonempty patch")
        key = path_key(PurePosixPath(entry["path"]).parts)
        if key in paths:
            raise ValueError(f"patch file {index} repeats an earlier target")
        paths.add(key)
    return files


def _bounded(tools, summary, details, error=None):
    budget = tools.config.max_output
    secrets = tools.config.secrets

    def fits(candidate):
        return len(json.dumps(redact(candidate, secrets), ensure_ascii=False).encode("utf-8")) <= budget

    if error:
        text = redact(error, secrets)[:512]
        candidate = {**summary, "detail": text}
        if fits(candidate):
            summary = candidate
    if details and fits({**summary, "files_omitted": len(details)}):
        summary["files_omitted"] = len(details)
        for detail in details:
            candidate = {**summary, "files": [*summary.get("files", []), detail]}
            candidate["files_omitted"] -= 1
            if not candidate["files_omitted"]:
                del candidate["files_omitted"]
            if not fits(candidate):
                break
            summary = candidate
    return summary


def apply(tools, files, *, superseded=None):
    tools._require_tool("patch_files")
    if tools.config.read_only:
        return {"approved": False, "changed": False, "error": "read-only mode blocks file writes"}
    requests = [("edit_lines", item, None) for item in patch_set(files)]
    summary = {"requested_files": len(requests), "changed_files": 0, "unchanged_files": 0, "changed": False}
    with tools.edit_batch(requests, model_action=tools.instructions.model_action_active) as blocked:
        if blocked is not None:
            index, result = next((index, result) for index, result in enumerate(blocked, 1)
                                 if result.get("batch_preflight") == "failed")
            return _bounded(tools, {**summary, "error": "Patch preflight failed", "stopped_file": index},
                            (), result.get("error"))

        summary["batch_id"] = tools._edit_batch.identity
        details = []
        for index, (name, arguments, _) in enumerate(requests, 1):
            tools._check_cancelled()
            if superseded is not None and superseded():
                return _bounded(tools, {**summary, "error": "Patch superseded by user guidance", "stopped_file": index,
                                        "partial": summary["changed"]}, details)
            result = tools.execute(name, arguments)
            if ("error" in result or result.get("approved") is False
                    or any(result.get(flag) for flag in ("skipped", "outcome", "interrupted", "cancelled"))):
                summary.update(error="Patch stopped", stopped_file=index, partial=summary["changed"])
                if result.get("approved") is False:
                    summary["approved"] = False
                if result.get("outcome") or tools.history.state["pending"] is not None:
                    summary.update(outcome="uncertain", partial=True)
                return _bounded(tools, summary, details, result.get("error"))
            changed = result.get("approved") is True and result.get("changed") is True
            if changed:
                summary["changed_files"] += 1
                summary["changed"] = summary["approved"] = True
            else:
                summary["unchanged_files"] += 1
            detail = {"file": index, "changed": changed}
            if changed and valid_read_id(result.get("read_id")):
                detail["read_id"] = result["read_id"]
            details.append(detail)
    return _bounded(tools, summary, details)
