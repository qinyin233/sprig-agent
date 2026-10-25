"""User-local batch recovery; sequential fresh approvals, never automatic rollback."""
import copy
import json

from .changes import ChangeError, unpack
from .config import redact, terminal_text


class BatchRestoreResult(dict):
    """Only public counters serialize; removed reference paths stay local."""
    def __init__(self, values, *, action, removed_paths=(), uncertain=False):
        super().__init__(values)
        self.restore_action = action
        self.removed_paths = tuple(removed_paths)
        self.uncertain = uncertain


def restore(tools, action):
    from .tools import ToolError
    completed, total, approved, identity = 0, 0, False, None
    removed, uncertain, error, cancelled = [], False, None, False
    owned = False

    def result():
        values = {"action": action + "-batch", "completed": completed, "total": total,
                  "complete": error is None, "changed": completed > 0, "approved": approved}
        if identity is not None:
            values["batch_id"] = identity
        if error is not None:
            values["error"] = terminal_text(str(error))[:512]
        if uncertain:
            values["outcome"] = "uncertain"
        if cancelled:
            values["cancelled"] = True
        def size():
            return len(json.dumps(redact(values, tools.config.secrets), ensure_ascii=False).encode())
        if size() > tools.config.max_output:
            values.pop("batch_id", None)
            values.pop("action", None)
        while size() > tools.config.max_output and values.get("error"):
            values["error"] = values["error"][:-16]
        safe = redact(values, tools.config.secrets)
        return BatchRestoreResult(safe, action=action + "-batch", removed_paths=removed, uncertain=uncertain)

    try:
        tools._require_tool("edit_file")
        tools._check_cancelled()
        if tools.config.read_only:
            raise ChangeError("read-only mode blocks batch undo/redo")
        if tools._edit_batch is not None or getattr(tools, "_restoring_batch", False):
            raise ChangeError("another edit or batch recovery is in progress")
        identity, remaining = tools.history.plan_batch(action)
        total = len(remaining)
        config = tools.config
        tools._restoring_batch, owned = True, True
        while remaining:
            state = tools.history.state
            snapshot = copy.deepcopy(state)

            def guard():
                tools._require_tool("edit_file")
                tools._check_cancelled()
                if tools.config is not config or tools.config.read_only:
                    raise ChangeError("configuration changed during batch recovery")
                if tools.history.store is not None:
                    tools.history.store.handle.ensure_open()
                if tools.history.state is not state or tools.history.state != snapshot:
                    raise ChangeError("change history changed during batch recovery")
                for record, revision in remaining:
                    tools._check_cancelled()
                    expected = unpack(record["after"] if action == "undo" else record["before"])
                    target = unpack(record["before"] if action == "undo" else record["after"])
                    with tools._parent(record["path"]) as (parent, name):
                        raw, info = tools._snapshot(parent, name)
                    if raw != expected or tools._revision(info) != revision:
                        raise ChangeError("file changed outside the recorded batch; recovery blocked")
                    for body in (raw, target):
                        text = tools._decode(body or b"")
                        if any(secret in text for secret in tools.config.credential_secrets):
                            raise ChangeError("configured credential blocks batch recovery")

            guard()
            prefix = f"BATCH {identity}: {completed + 1}/{total}; each file needs fresh approval; partial recovery is possible.\n"
            step = tools._restore_change(action, before_write=guard, description_prefix=redact(prefix, config.secrets))
            approved |= step.get("approved") is True
            if (step.get("approved") is not True or step.get("changed") is not True or "error" in step
                    or step.get("outcome") == "uncertain"):
                uncertain = step.get("outcome") == "uncertain"
                error = step.get("error") or "Batch recovery stopped after refusal; completed restores remain."
                break
            if step.get("file_removed"):
                removed.append(remaining[0][0]["path"])
            completed += 1
            remaining.pop(0)
    except KeyboardInterrupt:
        error, cancelled = "Batch recovery cancelled; completed restores remain.", True
        uncertain = tools.history.state.get("pending") is not None
    except (OSError, ToolError, ChangeError, ValueError, RuntimeError, RecursionError) as failure:
        error = failure
        uncertain = tools.history.state.get("pending") is not None
    finally:
        if owned:
            tools._restoring_batch = False
    return result()
