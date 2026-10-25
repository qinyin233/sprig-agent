"""Local conversation branches at whole user-turn boundaries; never replay actions."""
import json
import hashlib
import uuid

from .agent import Agent
from .command_logs import CommandLogs
from .config import redact, terminal_text
from .context import prepare_context
from .json_budget import encoded_size
from .session import MAX_SESSION, SessionStore, validate_history
from .summary import fingerprint
from .message_origin import user_request


def turn_catalog(messages, secrets=(), check=lambda: None):
    if not messages:
        return []
    encoded_size(messages, max_bytes=MAX_SESSION, check_cancelled=check)
    clean = validate_history(messages)
    starts = [i for i, row in enumerate(clean) if user_request(row)]
    rows = []
    for position, index in enumerate(starts):
        check()
        end = starts[position + 1] if position + 1 < len(starts) else len(clean)
        text = terminal_text(redact(clean[index]["content"], secrets)).replace("\n", " ").replace("\r", " ")
        rows.append({"message": index + 1, "through_message": end, "preview": text[:160]})
    return rows[::-1]


def timeline_page(messages, offset=0, *, secrets=(), budget=8192):
    if type(offset) is not int or not 0 <= offset <= 5000:
        raise ValueError("Use /timeline [OFFSET], with an offset from 0 to 5000")
    rows = turn_catalog(messages, secrets)
    if offset > len(rows):
        raise ValueError("timeline offset exceeds this conversation")
    result = {"turns": [], "offset": offset, "next_offset": None, "total": len(rows),
              "historical": True, "network_contacted": False}
    for row in rows[offset:offset + 20]:
        candidate = {**result, "turns": [*result["turns"], row]}
        candidate["next_offset"] = offset + len(candidate["turns"]) if offset + len(candidate["turns"]) < len(rows) else None
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) > budget:
            break
        result = candidate
    if offset < len(rows) and not result["turns"]:
        raise ValueError("timeline row exceeds output budget; use /history MESSAGE for a bounded page")
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > budget:
        raise ValueError("timeline metadata exceeds output budget")
    return result


class ConversationFork:
    def __init__(self, original, messages, target, *, saving=False, source_name=None, before=False, request=None):
        original.tools.root.ensure_open()
        original._check_cancelled()
        if original._running or original.tools.config is not original.config or not messages:
            raise ValueError("wait for an idle, valid conversation before branching")
        encoded_size(messages, max_bytes=MAX_SESSION, check_cancelled=original._check_cancelled)
        clean = validate_history(messages)
        if not any(user_request(row) for row in clean):
            raise ValueError("no user turn is available to branch")
        if type(before) is not bool or (before and target == "current") or (not before and request is not None):
            raise ValueError("revising requires an exact historical user message and replacement request")
        if before and (not isinstance(request, str) or not request.strip() or len(request) > original.config.max_context_bytes
                       or len(request.encode("utf-8")) > original.config.max_context_bytes):
            raise ValueError("replacement request must be nonempty and fit the task UTF-8 byte limit")
        if target == "current":
            end = len(clean)
        else:
            if not isinstance(target, str) or len(target) > 4 or not target.isascii() or not target.isdecimal():
                raise ValueError("choose current or an exact user message number from /timeline")
            row = next((row for row in turn_catalog(messages) if row["message"] == int(target)), None)
            if row is None:
                raise ValueError("branch point must be a user message number from /timeline")
            end = row["message"] - 1 if before else row["through_message"]
        self.original, self.source = original, messages
        self.version = original.tools._conversation_version
        self.source_hash = fingerprint(messages, len(messages), original._check_cancelled)
        self.through = end
        self.replaced = end + 1 if before else None
        self.request = redact(request, original.config.secrets) if before else None
        self.name = "fork-" + uuid.uuid4().hex if saving else None
        self.ready = False
        self.history = clean[:end]
        for row in self.history:
            row.pop("reasoning_content", None)
            row.pop("native_state", None)
        self.history = validate_history(redact(self.history, original.config.secrets))
        self.history.append({"role": "assistant", "content": redact(
            "[Conversation branch] Retained source " + (source_name or "(in-memory conversation)")
            + " through message " + str(end) + ". Workspace files were NOT rolled back. Historical reads, "
            "approvals and check results are not current evidence. Inspect current files and rerun fixed checks "
            "before claiming completion. Old actions will not be replayed; old command log handles are unavailable "
            "in this branch. Follow the retained user requirements and any new request.", original.config.secrets)})
        if before:
            self.history.append({"role": "user", "content": self.request})
        self.agent = Agent(original.config, original.provider, original.tools, original.emit,
                           checkpoint=original.checkpoint, emit_delta=original.emit_delta, cancel_event=original.cancel_event,
                           receive_input=original.receive_input, on_request_ready=original.on_request_ready, on_event=original.on_event)
        self.agent.restore_references(original.reference_state())
        self.agent.compact_context = original.compact_context
        self.logs = CommandLogs(original.config, original.tools._check_cancelled)

    def _fresh(self):
        self.original.tools.root.ensure_open()
        self.original._check_cancelled()
        encoded_size(self.source, max_bytes=MAX_SESSION, check_cancelled=self.original._check_cancelled)
        if (self.original._running or self.original.tools.config is not self.original.config
                or self.original.tools._conversation_version != self.version
                or fingerprint(self.source, len(self.source), self.original._check_cancelled) != self.source_hash):
            raise ValueError("conversation branch proposal is stale; select again")

    def preflight(self, approve, directory):
        self.ready = False
        self._fresh()
        self._prepare_request()  # Reject impossible branches before asking to create one.
        body = ""
        if self.replaced:
            raw = self.request.encode("utf-8")
            body = (json.dumps(self.request, ensure_ascii=False) if len(raw) <= 8192 else
                    f"正文 {len(raw)} UTF-8 字节，SHA256 {hashlib.sha256(raw).hexdigest()}。此处未展开全文；请在编辑区查看或核对输入后确认。")
        text = ((f"编辑历史请求并重发：截在用户消息 {self.replaced} 之前，旧请求及其后对话不进入新分支。\n"
                 "确认后允许发送下面的新请求；本地准备本身不调用模型。取消或发送失败后，请核对分支再明确继续或重新提案。\n"
                 "新请求正文（字面文字，不作为本地命令或任务模板再展开）：\n"
                 + body + "\n"
                 + f"当前模型：{self.original.config.model}；端点：{self.original.config.base_url}；协议：{self.original.config.wire_api}\n"
                 if self.replaced else f"从历史建立新对话分支：保留到消息 {self.through}（含该用户轮次的完整工具结果）。\n不会请求模型。\n")
                + "保留主输入草稿和待发送文件/粘贴。\n"
                + ("只发送编辑区正文，不重新附加旧的本轮文件。\n" if self.replaced else "")
                + "不会重放旧命令或恢复工作区文件；新写入、命令和发送图片仍各自新批准。\n"
                "旧读取标识、推理/原生元数据、摘要、MCP 连接和命令日志所有权清除；旧验收失效。\n"
                "受管后台命令停止；LSP 进程停止并清除结果。已有文件/网络副作用不会撤销，历史任务不重启。\n"
                "模型和工具策略保持；实时引用沿用当前选择并在请求前重新检查；文件编辑备份保留。\n"
                + (f"保存新私有分支：{directory}/{self.name}.json；原文件不覆盖。\n" if self.name else
                   "当前未启用保存；分支只在内存，可明确另存。\n"))
        if approve(redact(text, self.original.config.secrets)) is not True:
            return False
        self._fresh()
        self._prepare_request()  # References and policy may change while confirmation is open.
        self.ready = True
        return True

    def _prepare_request(self):
        self.agent._refresh_instructions(self.history)
        _, budget = prepare_context(self.agent.config, self.agent._request_messages(self.history, 1), self.agent.schemas,
                                    self.agent.compact_context, check_cancelled=self.original._check_cancelled)
        if not budget["within_budget"]:
            raise ValueError("branch exceeds the request budget; choose an earlier turn or explicitly enable compaction")
        self.agent.references.observed = self.agent._prepared_reference_observations

    def commit(self, directory):
        ready, self.ready = self.ready, False
        if not ready:
            raise ValueError("branch requires fresh confirmation and successful local preparation")
        self._fresh()
        store = None
        try:
            if self.name:
                store = SessionStore(directory, self.original.config)
                options = self.agent.reference_save_options()
                options.pop("logs", None)
                if self.logs.state() is not None:
                    options["logs"] = self.logs.state()
                store.save_new(self.name, self.history, **options)
            self.original.tools.rebind_model(self.original.config)
            self.original.tools.logs.restore(self.logs.state())
            self.agent.restore_plan(self.history)
        except BaseException:
            if store is not None:
                store.close()
            raise
        return self.agent, self.history, store, self.name
