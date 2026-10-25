"""Textual terminal UI; the existing agent and approval boundaries stay in charge."""

import io
import json
from pathlib import Path
import sys
import threading

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.document._edit import Edit
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static, TabbedContent, TabPane, TextArea
from textual.worker import get_current_worker

from .agent import Agent
from .prompt_templates import expand_prompt, prompt_catalog
from .input_inbox import (InputInbox, UserInput, InputChanged, input_state, input_preview, new_input,
                          change_inputs, check_input_growth, input_change_prompt, input_page, input_revision, pending_command)
from .input_inbox import delivered_inputs, queue_command
from .changes import ChangeHistory
from .config import SHORTCUT_ACTIONS, redact, terminal_text
from .composer import DraftPastes, MAX_PASTE_BYTES, paste_text
from .mentions import DraftFiles, mention_query
from .model_selection import ModelSwitch
from .conversation_fork import ConversationFork, timeline_page, turn_catalog
from .conversation_revision import RevisionSource, revision_command
from .revision_ui import RevisionScreen
from .message_origin import application_origin, user_request
from .provider import Provider
from .picker import PickerScreen
from .question_ui import QuestionScreen
from .runtime import runtime_label
from .session import SessionError, SessionStore, validate_history
from .settings import public_config
from .tools import Tools


class UiRequest(Message):
    """A cancellable bridge; worker threads never wait on a stopped UI loop."""

    def __init__(self, callback, args):
        super().__init__()
        self.callback, self.args = callback, args
        self.done = threading.Event()
        self.result, self.error = None, None


class ComposerEdit(Edit):
    """Keep user-pasted text with its own undo entry, never with typed labels."""

    def do(self, editor, record_selection=True):
        result = super().do(editor, record_selection)
        if not record_selection:
            editor.restore_paste_state(self.paste_after)
            self._updated_selection = self.redo_selection
        return result

    def undo(self, editor):
        result = super().undo(editor)
        editor.restore_paste_state(self.paste_before)
        return result


class TaskInput(TextArea):
    """Chat composer: Enter sends; modified Enter inserts a newline."""

    BINDINGS = [
        Binding("enter", "send", "发送", priority=True),
        Binding("shift+enter,ctrl+j", "newline", "换行", priority=True),
        Binding("tab", "complete", "补全", priority=True),
        Binding("f4", "expand_pastes", "展开粘贴", priority=True),
    ]

    def __init__(self, *args, **kwargs):
        self.files = DraftFiles()
        self.pastes = DraftPastes()
        self.verbatim_paste = self.literal_paste = False
        self._queue_paste_mode = None
        self._paste_identity = 0
        self._expanding_paste = False
        self._last_composer_edit = None
        super().__init__(*args, **kwargs)
        self.files.sync(self.text)
        self.pastes.sync(self.text)

    def load_text(self, text):
        super().load_text(text)
        self.files = DraftFiles(self.text)
        self.pastes = DraftPastes(self.text)
        self.verbatim_paste = self.literal_paste = False
        self._queue_paste_mode = None
        self._last_composer_edit = None

    def paste_state(self):
        return tuple(self.pastes.spans), self.verbatim_paste, self.literal_paste, self._queue_paste_mode

    def restore_paste_state(self, state):
        spans, self.verbatim_paste, self.literal_paste, self._queue_paste_mode = state
        self.pastes = DraftPastes(self.text)
        self.pastes.spans = [row for row in spans if self.text[row[0]:row[1]] == row[3]]

    def edit(self, edit):
        self.files.sync(self.text)
        self.pastes.sync(self.text)
        edit = ComposerEdit(edit.text, edit.from_location, edit.to_location, edit.maintain_selection_offset)
        edit.paste_before = self.paste_state()
        start = self.document.get_index_from_location(edit.top)
        end = self.document.get_index_from_location(edit.bottom)
        if self._queue_paste_mode is not None and start <= len(self.text) - len(self.text.lstrip()) + len('/queue'):
            self._queue_paste_mode = None
        previous = len(self.pastes.spans)
        result = super().edit(edit)
        self.files.sync(self.text, start, end)
        self.pastes.sync(self.text, start, end)
        if previous > len(self.pastes.spans) and not self._expanding_paste and self.is_mounted:
            self.notify("已删除或修改粘贴标记，对应正文已移除；编辑正文前请先 F4 展开", severity="warning")
        if not self.text:
            self.verbatim_paste = self.literal_paste = False
            self._queue_paste_mode = None
        edit.paste_after = self.paste_state()
        edit.redo_selection = self.selection
        self._last_composer_edit = edit
        return result

    def undo(self):
        if self.history.undo_stack:
            # Textual refreshes scrollbars before restoring the saved caret.
            # A multiline replacement may already have removed its old row.
            self.move_cursor((0, 0))
        super().undo()
        self.files.sync(self.text)
        self.pastes.sync(self.text)

    def redo(self):
        if self.history.redo_stack:
            self.move_cursor((0, 0))
        super().redo()
        self.files.sync(self.text)
        self.pastes.sync(self.text)

    async def _on_paste(self, event):
        event.stop()
        event.prevent_default()
        if self.read_only:
            return
        try:
            body = paste_text(event.text)
            if not body:
                return
            self.pastes.sync(self.text)
            start, end = sorted(self.selection)
            left, right = (self.document.get_index_from_location(point) for point in (start, end))
            folded = len(body) > 200 or body.count("\n") >= 2
            if folded:
                candidate, inserted = self.pastes.fold(left, right, body, self._paste_identity + 1)
            else:
                inserted = body
                candidate = DraftPastes(self.text)
                candidate.spans = list(self.pastes.spans)
                candidate.sync(self.text[:left] + body + self.text[right:], left, right)
                expanded = candidate.expanded()
                if len(expanded) > MAX_PASTE_BYTES or len(expanded.encode("utf-8")) > MAX_PASTE_BYTES:
                    raise ValueError("展开后的草稿超过 2 MiB，粘贴未插入")
        except ValueError as error:
            self.notify(str(error), title="粘贴未插入，原草稿保留", severity="warning")
            return
        self.history.checkpoint()
        queue_mode = self._queue_paste_mode
        prefix = self.text[:left].lstrip()
        if not self.literal_paste and prefix.split()[:1] == ['/queue']:
            queue_mode = queue_mode or ('json' if prefix.split() == ['/queue', 'add'] else 'raw')
        if left <= len(self.text) - len(self.text.lstrip()) + len('/queue'):
            queue_mode = None
        self.replace(inserted, start, end, maintain_selection_offset=False)
        self.pastes = candidate
        self.verbatim_paste = True
        self.literal_paste = self.literal_paste or folded
        self._queue_paste_mode = queue_mode
        self._paste_identity += 1
        self._last_composer_edit.paste_after = self.paste_state()
        self.history.checkpoint()
        self.focus()

    def action_expand_pastes(self):
        self.pastes.sync(self.text)
        if not self.pastes.spans:
            return
        self.history.checkpoint()
        self._expanding_paste = True
        try:
            for left, right, body, _ in reversed(self.pastes.spans):
                self.replace(body, self.document.get_location_from_index(left),
                             self.document.get_location_from_index(right))
        finally:
            self._expanding_paste = False
        self.history.checkpoint()
        self.focus()

    class Submitted(Message):
        def __init__(self, editor, text, draft=None):
            super().__init__()
            self.editor, self.text = editor, text
            self.draft = editor.text if draft is None else draft
            editor.files.sync(self.draft)
            self.files = editor.files.paths()
            self.preflight = editor.verbatim_paste
            self.literal = editor.literal_paste
            self.queue_paste_mode = editor._queue_paste_mode

    class Complete(Message):
        def __init__(self, editor):
            super().__init__()
            self.editor = editor

    def action_complete(self):
        self.post_message(self.Complete(self))

    def action_send(self):
        self.pastes.sync(self.text)
        queued = self.text.lstrip().split(None, 1)[:1] == ["/queue"]
        text = self.pastes.expanded() if self.verbatim_paste or queued or self.text.lstrip().startswith("/") else self.text.strip()
        if text.strip() and not getattr(self, "_sending", False):
            self._sending = True
            self.post_message(self.Submitted(self, text, self.text))

    def action_newline(self):
        self.replace("\n", *self.selection, maintain_selection_offset=False)


class ApprovalScreen(ModalScreen[bool]):
    """No single-letter approval shortcut; Enter initially selects rejection."""

    BINDINGS = [Binding("escape", "reject", "拒绝")]
    AUTO_FOCUS = "#reject"
    CSS = """
    ApprovalScreen { align: center middle; background: $background 70%; }
    #approval { width: 90%; height: 85%; border: thick $warning; padding: 1 2; }
    #approval-title { height: 2; color: $warning; text-style: bold; }
    #approval-detail { height: 1fr; }
    #approval-buttons { height: 3; margin-top: 1; }
    #approval-buttons Button { width: 1fr; margin: 0 1; }
    #approval-task { height: 4; margin-top: 1; }
    #steer { width: 1fr; }
    """

    def __init__(self, description, allow_steering=False):
        super().__init__()
        self.description = description
        self.allow_steering = allow_steering

    def compose(self) -> ComposeResult:
        with Vertical(id="approval"):
            yield Static("请检查这一次操作，默认拒绝", id="approval-title", markup=False)
            yield TextArea(self.description, read_only=True, show_cursor=False, id="approval-detail")
            if self.allow_steering:
                yield TaskInput(placeholder="调整要求：Enter 发送，停止这次待确认操作", id="approval-task")
                yield Button("发送新要求，重新规划", id="steer", variant="primary")
            with Horizontal(id="approval-buttons"):
                yield Button("拒绝 / Esc", id="reject", variant="default")
                yield Button("批准这次操作", id="approve", variant="warning")

    @on(Button.Pressed)
    def decide(self, event):
        event.stop()
        if event.button.id == "steer":
            self.query_one("#approval-task", TaskInput).action_send()
        else:
            self.dismiss(event.button.id == "approve")

    def action_reject(self):
        self.dismiss(False)


class SprigApp(App[int]):
    OUTPUT_BUFFER_LIMIT = 64000
    OUTPUT_REFRESH_SECONDS = 0.05
    TRANSCRIPT_LIMIT = 64000
    TRANSCRIPT_MARKER = "[显示已裁剪：仅保留最近文本；不代表模型上下文。]\n"
    MAX_PENDING_TASKS = 32
    TITLE = "Sprig Agent"
    BINDINGS = [
        Binding("ctrl+enter", "submit", "发送", show=False, priority=True),
        Binding("ctrl+c", "cancel", "取消", priority=True),
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+l", "focus_task", "任务输入", priority=True),
        # Textual named actions need a nonempty base key. These identifiers
        # are never terminal events; set_keymap activates configured aliases.
        *(Binding("sprig_unbound_" + action, "configured_shortcut(" + repr(action) + ")", "",
                  id="sprig." + action, show=False, priority=True) for action in SHORTCUT_ACTIONS),
    ]
    CSS = """
    #context { height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    #body { height: 1fr; }
    #chat { width: 1fr; padding: 0 1; }
    .pane-title { height: 1; text-style: bold; color: $accent; }
    #transcript { height: 1fr; border: round $primary; }
    #task { height: 5; border: round $accent; }
    #task-buttons { height: 3; }
    #task-buttons Button { width: 1fr; min-width: 8; margin-right: 1; }
    #sidebar { width: 30; border-left: solid $primary; padding: 0 1; }
    #state { height: auto; min-height: 2; color: $success; }
    #task-state { height: auto; min-height: 2; color: $text-muted; }
    #side-tabs { height: 1fr; }
    #operations Button { width: 1fr; margin-bottom: 1; }
    #session-name { margin-top: 1; }
    #session-buttons { height: 4; }
    #session-buttons Button { min-width: 8; width: 1fr; }
    #events { height: 1fr; }
    """

    def __init__(self, config, task=None, session_dir=None, save_session=None, resume=None, provider=None, change_dir=None, files=(),
                 model_selection=None, provider_factory=None):
        super().__init__()
        self.config = config
        self.model_selection = model_selection
        self.provider_factory = provider_factory or Provider
        self.set_keymap({"sprig." + action: ",".join(keys) for action, keys in config.tui_keybindings if keys})
        self.initial_task = task
        self.session_dir = Path(session_dir or Path.home() / ".local/state/sprig-agent/sessions")
        self.session_name = save_session or resume
        self.store = None
        self.messages = None
        self.busy = False
        self.task_status = 2 if resume else None
        self.task_message = "已恢复历史，未重新验收" if resume else "尚未运行任务"
        self.last_operation_status = 0
        self.local_action = None
        self.local_command_approved = False
        self.cancel_event = threading.Event()
        self._output_condition = threading.Condition()
        self._output_buffer = io.StringIO()
        self._output_size = 0
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._unmounted = False
        self._ui_ready = False
        self._ui_thread_id = threading.get_ident()
        self.quit_when_idle = False
        self.transcript = ""
        self._transcript_trimmed = False
        self.streaming = False
        self.approval = None
        self.question = None
        # UI-thread-owned queue. Only the single agent worker takes its contents.
        self.pending_tasks = []
        self.recovered_inputs = input_state()
        self._input_retry = None
        self._job_seen = set()
        self._draft_submission = None
        self._revision_editor = None
        self._revision_ready = False
        if self.session_name:
            self.store = SessionStore(self.session_dir, config)
            try:
                if resume:
                    self.messages = self.store.load(resume)
                    self.recovered_inputs = self.store.loaded_inputs
                else:
                    self.store.ensure_new(save_session)
            except BaseException:
                self.store.close()
                raise
        app = self

        class UiTools(Tools):
            def execute(self, name, arguments):
                result = super().execute(name, arguments)
                if result.get("approved") is False and app.ui_call(lambda: bool(app._input_inbox.batch(delivery="steer"))):
                    note = ("Stopped after a workspace change; inspect current files: " if result.get("changed") or result.get("outcome")
                            else "Not executed: ")
                    result = {**result, "skipped": True, "error": note +
                              "superseded by new user guidance. Follow the updated request and request fresh approval for any new action."}
                summary = {key: result[key] for key in ("approved", "changed", "returncode", "timed_out", "output_limited",
                                                       "outcome", "partial", "copied", "source_removed", "path", "destination", "error") if key in result}
                if app._ui_ready and not app._unmounted:
                    if app.local_action is not None:
                        app.ui_call(app.event, name + ": " + json.dumps(summary, ensure_ascii=False))
                    if name == "update_plan":
                        app.ui_call(app.refresh_plan)
                return result

        history = None
        try:
            history = ChangeHistory(config, change_dir)
            self.tools = UiTools(config, self.confirm, self.cancel_event, self.emit_command, history=history, ask=self.ask_questions)
        except BaseException:
            if history:
                history.close()
            if self.store:
                self.store.close()
            raise
        try:
            self.agent = Agent(config, provider or self.provider_factory(config), self.tools, self.emit,
                               checkpoint=self.checkpoint, emit_delta=self.emit_delta, cancel_event=self.cancel_event,
                               receive_input=self.receive_input, on_request_ready=self.request_ready, on_event=self.runtime_event)
            self.agent.restore_plan(self.messages)
            if resume and self.store:
                selected = getattr(self.store, "loaded_references", None)
                self.agent.restore_references(selected if isinstance(selected, dict) else None)
                self.agent.restore_summary(self.store.loaded_summary, self.messages)
                self.agent.restore_logs(getattr(self.store, "loaded_logs", None))
                if dict(config.setting_sources).get("reference_files") == "CLI" and config.reference_files:
                    self.messages, _ = self.agent.update_references(config.reference_files, messages=self.messages)
            if files:
                self.messages, _ = self.agent.attach_files(files, self.messages)
        except BaseException:
            self.tools.close()
            if self.store:
                self.store.close()
            raise

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self.safe(f"{self.config.model} | {self.config.base_url}\n{self.config.workdir} | {'只读模式' if self.config.read_only else '编辑模式'} | 验收命令: {len(self.config.checks)}"), id="context", markup=False)
        with Horizontal(id="body"):
            with Vertical(id="chat"):
                yield Static("对话", classes="pane-title", markup=False)
                yield TextArea(read_only=True, show_cursor=False, id="transcript")
                yield TaskInput(placeholder="Enter 发送，Shift+Enter 换行；@关键词 + Tab 选本轮文件；执行中可追加文字", id="task")
                with Horizontal(id="task-buttons"):
                    yield Button("发送 / Enter", id="send", variant="primary")
                    yield Button("选择文件", id="files")
                    yield Button("展开粘贴 / F4", id="expand-paste")
                    yield Button("取消 / Ctrl+C", id="cancel", disabled=True)
            with Vertical(id="sidebar"):
                yield Static("就绪", id="state", markup=False)
                yield Static(self.safe("任务结果：" + self.task_message), id="task-state", markup=False)
                with TabbedContent(id="side-tabs"):
                    with TabPane("操作", id="actions-tab"):
                        with VerticalScroll(id="operations"):
                            yield Button("继续当前对话", id="continue")
                            yield Button("管理待处理要求", id="pending")
                            yield Button("新建对话", id="new")
                            yield Button("切换模型", id="model", disabled=self.model_selection is None)
                            yield Button("刷新模型目录", id="reload-models", disabled=self.model_selection is None)
                            yield Button("查看 Git diff", id="diff", disabled=self.config.read_only)
                            yield Button("查看 Git 状态", id="git", disabled=self.config.read_only)
                            yield Button("查看项目地图", id="map")
                            yield Button("查看上下文预算", id="context-budget")
                            yield Button("查看请求统计", id="usage")
                            yield Button("压缩旧工具记录", id="compact")
                            yield Button("查看修改记录", id="changes")
                            yield Button("撤销最近文件编辑", id="undo", disabled=self.config.read_only)
                            yield Button("重做文件编辑", id="redo", disabled=self.config.read_only)
                            yield Button("清除修改备份", id="discard-history")
                            yield Static("本地会话（不加密）", markup=False)
                            yield Input(value=self.session_name or "", placeholder="会话名称", id="session-name")
                            with Horizontal(id="session-buttons"):
                                yield Button("保存", id="save")
                                yield Button("恢复", id="restore")
                            yield Button("列出本地会话", id="sessions")
                            yield Button("从历史建立分支", id="fork")
                            yield Button("编辑历史请求并重发", id="revise")
                            yield Button("删除指定会话", id="delete")
                    with TabPane("计划", id="plan-tab"):
                        yield Static("模型报告的进度；不代表审批或验收", markup=False)
                        yield TextArea(read_only=True, show_cursor=False, id="task-plan")
                    with TabPane("事件", id="events-tab"):
                        yield RichLog(id="events", wrap=True, markup=False, highlight=False, max_lines=1000)
        yield Footer()

    def on_mount(self):
        self._ui_ready = True
        if self.config.tui_theme:
            self.theme = self.config.tui_theme
        self.query_one("#sidebar").display = self.config.tui_sidebar
        self.query_one("#task").styles.height = self.config.tui_editor_height
        for name in ("diff", "git"):
            self.query_one("#" + name, Button).disabled = self.config.read_only or "run" in self.config.disabled_tools
        for name in ("undo", "redo"):
            self.query_one("#" + name, Button).disabled = self.config.read_only or "edit_file" in self.config.disabled_tools
        self.query_one("#map", Button).disabled = "project_map" in self.config.disabled_tools
        self._ui_thread_id = threading.get_ident()
        self.set_interval(self.OUTPUT_REFRESH_SECONDS, self.flush_output)
        self.set_interval(.25, self.refresh_jobs)
        self.action_focus_task()
        self.refresh_plan()
        self.append("Sprig 就绪。任务、读取内容和工具结果会发送到所示服务。\n写入和命令逐次确认；命令没有沙箱。\n")
        self.append("输入 /help 查看本地命令；Enter 发送，Shift+Enter / Ctrl+J 换行。\n")
        if self.config.preserve_reasoning or self.config.wire_api != "chat_completions":
            self.append("已启用推理协议兼容：推理字段不显示，会随后续请求发送；启用会话保存时一并保存（不加密）。\n")
        if self.tools.history.store is not None:
            self.append("已显式启用本地修改备份（不加密）；仅记录文件工具编辑，命令修改不在撤销范围内。\n")
        if self.messages:
            self.show_history()
        if self.recovered_inputs["pending"]:
            self.append("发现已保存的待处理要求；/pending 查看，/pending apply 加入对话，/pending discard 逐次确认丢弃。之后 /continue 才请求模型。\n")
        if self.initial_task:
            if self.recovered_inputs["pending"]:
                self.query_one("#task", TextArea).load_text(self.initial_task)
            else:
                self.start(self.initial_task)

    def refresh_plan(self):
        if not self._ui_ready or self._unmounted:
            return
        snapshot = self.agent.plan_snapshot()
        labels = {"pending": "待处理", "in_progress": "进行中", "completed": "已完成（模型报告）"}
        lines = [str(index) + ". [" + labels[step["status"]] + "] " + step["step"]
                 for index, step in enumerate(snapshot["plan"], 1)]
        if snapshot["explanation"]:
            lines.append("说明：" + snapshot["explanation"])
        self.query_one("#task-plan", TextArea).load_text(self.safe("\n".join(lines) or "尚无任务计划"))

    def refresh_jobs(self):
        if not self._ui_ready or self._unmounted:
            return
        rows = self.tools.jobs.observations()
        self._job_seen.intersection_update(row["job_id"] for row in rows)
        for row in rows:
            if row["state"] != "running" and row["job_id"] not in self._job_seen:
                self._job_seen.add(row["job_id"])
                self.append("\n[后台任务状态；不是验收结果] " + json.dumps(row) + "；/job output ID 查看输出。\n")
                self.event("后台命令已结束；当前工作区仍需独立检查")

    def safe(self, text):
        return terminal_text(redact(str(text), self.config.secrets))

    def append(self, text):
        # Drain older worker output before user messages and completion notices.
        self.flush_output()
        self._append(text)

    def _append(self, text):
        text = self.safe(text)
        previous = self.transcript[len(self.TRANSCRIPT_MARKER):] if self._transcript_trimmed else self.transcript
        combined = previous + text
        self._transcript_trimmed = self._transcript_trimmed or len(combined) > self.TRANSCRIPT_LIMIT
        view = self.query_one("#transcript", TextArea)
        if self._transcript_trimmed:
            self.transcript = self.TRANSCRIPT_MARKER + combined[-(self.TRANSCRIPT_LIMIT - len(self.TRANSCRIPT_MARKER)):]
            view.load_text(self.transcript)
        else:
            self.transcript = combined
            view.insert(text, location=view.document.end)
        view.scroll_end(animate=False)

    def flush_output(self):
        with self._output_condition:
            if not self._output_size:
                return
            text = self._output_buffer.getvalue()
            self._output_buffer = io.StringIO()
            self._output_size = 0
            self._output_condition.notify_all()
        if not self._unmounted:
            self._append(text)

    def queue_output(self, text):
        """Coalesce fragments with bounded memory and cancellable backpressure."""
        offset = 0
        while offset < len(text):
            self.check_cancelled()
            with self._output_condition:
                while self._output_size == self.OUTPUT_BUFFER_LIMIT:
                    self._output_condition.wait(0.05)
                    self.check_cancelled()
                    if self._unmounted:
                        raise RuntimeError("terminal interface has closed")
                if self._unmounted:
                    raise RuntimeError("terminal interface has closed")
                count = min(len(text) - offset, self.OUTPUT_BUFFER_LIMIT - self._output_size)
                self._output_buffer.write(text[offset:offset + count])
                self._output_size += count
                offset += count

    def event(self, text):
        self.query_one("#events", RichLog).write(self.safe(text))

    def runtime_event(self, event):
        if self._ui_ready and not self._unmounted:
            self.ui_call(self.show_runtime, event)

    def show_runtime(self, event):
        label = runtime_label(event, chinese=True)
        self.set_state(label)
        self.event(f"[{event.sequence}] " + label)

    def set_state(self, text):
        self.query_one("#state", Static).update(self.safe(text))

    @property
    def last_status(self):
        # Inspecting local metadata cannot erase a failed/cancelled task.
        return self.task_status if self.task_status is not None else self.last_operation_status

    def set_task_result(self, status, message):
        self.task_status, self.task_message = status, message
        if status == 0 and not self.config.checks:
            self.task_message += "；未配置验收命令"
        self.query_one("#task-state", Static).update(self.safe("任务结果：" + self.task_message))

    def invalidate_task(self):
        self.set_task_result(2, "工作区已变更或可能变更，先前验收失效")

    def set_busy(self, busy):
        self.busy = busy
        for identity in ("files", "continue", "new", "diff", "git", "map", "context-budget", "usage", "compact", "changes", "undo", "redo", "discard-history", "save", "restore", "sessions", "fork", "revise", "delete", "session-name"):
            self.query_one("#" + identity).disabled = busy or (self.config.read_only and identity in {"diff", "git", "undo", "redo"})
        self.query_one("#cancel", Button).disabled = not busy
        self.query_one("#model", Button).disabled = busy or self.model_selection is None
        self.query_one("#reload-models", Button).disabled = busy or self.model_selection is None
        if not busy:
            self.action_focus_task()

    def request_ready(self):
        if self._ui_ready and not self._unmounted:
            self.ui_call(self.clear_ready_draft)

    def clear_ready_draft(self):
        selected, self._draft_submission = self._draft_submission, None
        if selected is None:
            return
        identity, draft, _ = selected
        try:
            editor = self.query_one("#" + identity, TaskInput)
        except NoMatches:
            return
        if editor.text == draft:
            editor.load_text("")

    def checkpoint(self, history):
        self.messages = history
        if self.store and self.session_name:
            self.store.save(self.session_name, history, **self.agent.reference_save_options())
        self._input_inbox.acknowledge(history)

    @property
    def pending_tasks(self):
        return [entry.text for entry in self._input_inbox.batch()]

    @pending_tasks.setter
    def pending_tasks(self, tasks):
        self._input_inbox = InputInbox({"version": 1, "pending": [new_input(text).row() for text in tasks]})
        self._queue_running = self._queue_next = False
        self._queue_scope = frozenset()

    def pending_state(self):
        state = input_state(self.recovered_inputs if self.recovered_inputs["pending"] else self._input_inbox.snapshot())
        if self._input_retry and not any(row["id"] == self._input_retry.id for row in state["pending"]):
            state["pending"].append(self._input_retry.row())
        return input_state(state)

    def manage_pending(self):
        from .pending_ui import PendingScreen
        self.push_screen(PendingScreen(self))

    def change_pending(self, request, *, expected=None, completed=None):
        """Review local queue mutations without replacing the active agent worker."""
        def report(result):
            if completed:
                completed(result)
            else:
                self.append("\n[本地队列修改；未请求模型]\n" + json.dumps(result, ensure_ascii=False) + "\n")

        try:
            if self._input_retry or self._input_inbox.blocked:
                raise ValueError("输入保存尚未确认；请先恢复会话或明确处理 /pending，不能直接修改队列")
            current = self.pending_state()
            expected = current if expected is None else expected
            if input_revision(expected) != input_revision(current) or self._input_inbox.claimed():
                raise InputChanged("要求已变化或正在递送，请刷新后再修改")
            options = {key: request[key] for key in ("text", "position", "delivery") if key in request}
            if "text" in options:
                options["text"] = redact(options["text"], self.config.secrets)
            candidate, result = change_inputs(expected, request["action"], request["identity"], **options)
            check_input_growth(expected, candidate, self.config.max_context_bytes)
            source, agent, session = self._input_inbox, self.agent, self.session_name
            recovered = bool(self.recovered_inputs["pending"])
        except (OSError, ValueError, RuntimeError, SessionError) as error:
            report({"approved": False, "error": self.safe(error), "network_contacted": False})
            return

        def decide(approved):
            if approved is not True:
                report({"approved": False, "network_contacted": False})
                return
            try:
                if (self._closed or self._unmounted or self.agent is not agent or self.session_name != session
                        or self._input_inbox is not source or self.busy and self.cancel_event.is_set()
                        or input_revision(self.pending_state()) != input_revision(expected)):
                    raise InputChanged("对话或待处理要求已变化；修改未应用，请刷新")
                def persist():
                    if self.store and session:
                        self.store.revise_inputs(session, expected, candidate)
                if recovered:
                    source.revise(source.snapshot(), source.snapshot(), persist)
                    self.recovered_inputs = candidate
                else:
                    source.revise(expected, candidate, persist)
                self.set_task_result(2, "待处理要求已修改，尚未递送或重新验收")
                report({**result, "approved": True})
            except (OSError, ValueError, RuntimeError, SessionError) as error:
                if source.blocked and self.busy:
                    self.action_cancel()
                report({"approved": False, "error": self.safe(error), "save_uncertain": source.blocked,
                        "network_contacted": False})
        self.push_screen(ApprovalScreen(self.safe(input_change_prompt(expected, candidate, result))), decide)

    def take_pending(self):
        count = 1 if self.config.steering_mode == "one-at-a-time" else None
        tasks = self._input_inbox.claim(count, delivery="steer")
        if tasks:
            self.set_state("已接收新要求，继续当前会话")
        return tasks

    def receive_input(self):
        if self._unmounted:
            return self._input_inbox.claim(delivery="steer")
        return self.ui_call(self.take_pending)

    @on(UiRequest)
    def ui_request(self, event):
        try:
            if self._unmounted:
                raise RuntimeError("terminal interface has closed")
            event.result = event.callback(*event.args)
        except BaseException as error:
            event.error = error
        finally:
            event.done.set()

    def ui_call(self, callback, *args):
        if self._unmounted:
            raise RuntimeError("terminal interface has closed")
        if threading.get_ident() == self._ui_thread_id:
            return callback(*args)
        event = UiRequest(callback, args)
        if not self.post_message(event):
            raise RuntimeError("terminal interface has closed")
        while not event.done.wait(0.05):
            if self._unmounted:
                raise RuntimeError("terminal interface has closed")
        if event.error:
            raise event.error
        return event.result

    def check_cancelled(self):
        if get_current_worker().is_cancelled:
            self.cancel_event.set()
        if self.cancel_event.is_set():
            raise KeyboardInterrupt

    def emit(self, text):
        if self._unmounted:
            return
        self.streaming = False
        self.ui_call(self.append, str(text) + "\n")

    def emit_delta(self, text):
        self.check_cancelled()
        if not self.streaming:
            self.queue_output("\nSprig> ")
            self.streaming = True
        self.queue_output(text)

    def emit_command(self, text):
        self.check_cancelled()
        self.streaming = False
        self.queue_output(text)

    def confirm(self, description):
        self.check_cancelled()
        answer, done = [False], threading.Event()

        def show():
            self.flush_output()
            # Guidance could arrive between the agent boundary and this approval.
            if self._input_inbox.batch(delivery="steer"):
                done.set()
                return

            def decide(approved):
                approved = approved is True
                answer[0] = approved
                self.approval = None
                if approved and self.local_action in {"run", "job", "worktree-create", "worktree-list", "mcp", "lsp", "verify"}:
                    # Even a viewing command can invoke Git helpers. Treat its
                    # workspace effects conservatively, including cancellation.
                    self.invalidate_task()
                    self.local_command_approved = True
                done.set()
                if self.local_action is not None or not self.busy:
                    self.set_state("处理中")
            self.approval = ApprovalScreen(self.safe(description), allow_steering=True)
            if self.local_action is not None or not self.busy:
                self.set_state("等待确认")
            self.push_screen(self.approval, decide)

        self.ui_call(show)
        while not done.wait(0.05):
            self.check_cancelled()
        self.check_cancelled()
        return answer[0]

    def ask_questions(self, questions):
        self.check_cancelled()
        answers, done = [None], threading.Event()
        def show():
            self.flush_output()
            if self._input_inbox.batch(delivery="steer"):
                done.set()
                return
            def finish(result):
                answers[0] = result
                self.question = None
                done.set()
            self.question = QuestionScreen(questions, self.config.max_output, self.config.secrets,
                                           self.queue_guidance, self.config.max_context_bytes)
            self.push_screen(self.question, finish)
        self.ui_call(show)
        while not done.wait(0.05):
            self.check_cancelled()
        self.check_cancelled()
        return answers[0]

    def start(self, task=None, local=None, *, context_files=(), preflight=False):
        if self.busy:
            return
        if (self.recovered_inputs["pending"] or self._input_retry or self._input_inbox.blocked) and (not local or local[0] in {"model-switch", "conversation-fork", "conversation-revise", "summary", "verify"}):
            self.reject_input("请先用 /pending apply 或 /pending discard 处理已恢复的要求；未请求模型。")
            return
        if not local and not task and not self.messages and not self.pending_tasks:
            self.notify("请先输入任务", severity="warning")
            return
        if task and self.pending_tasks:
            if not self.queue_guidance(task):
                return
            task = None
        self.cancel_event.clear()
        self.streaming = False
        self.local_action = local[0] if local else None
        self.local_command_approved = False
        if not local:
            self.set_task_result(2, "任务进行中，等待本轮结果")
        elif local[0] == "verify":
            self.set_task_result(2, "固定检查进行中；尚未取得当前结果")
        self.set_busy(True)
        self.set_state("处理中")
        if task:
            self.append("\n你> " + task + "\n")
        self.execute(task, local, context_files, preflight)

    @work(thread=True, group="agent", exclusive=True, exit_on_error=False)
    def execute(self, task, local, context_files=(), preflight=False):
        # Finishing and submission are serialized on the UI thread. A late
        # request keeps this worker alive instead of racing a second worker.
        with self._lifecycle_lock:
            if self._closed:
                return
            while True:
                options = {"context_files": context_files} if context_files else {}
                if preflight:
                    options["preflight"] = True
                status, message = self.execute_turn(task, local, **options)
                try:
                    again = self.ui_call(self.finished, status, message)
                except RuntimeError:
                    return  # The terminal/app may already have closed during teardown.
                if not again:
                    return
                task, local = None, None
                context_files = ()
                preflight = False

    def execute_turn(self, task, local, context_files=(), preflight=False):
        status, message = 0, "完成"
        try:
            if local:
                name, arguments = local
                if name == "verify":
                    if self.messages is None:
                        self.messages = []  # Keep intent/results on checkpoint errors.
                    self.messages, result = self.agent.verify(self.messages)
                    status = 0 if result["verification"] == "passed" else 2
                    message = "固定检查通过；未请求模型" if status == 0 else "固定检查未通过或未完成；未请求模型，未自动继续"
                elif name == "pending":
                    action = arguments["action"]
                    if action == "apply":
                        if self._input_inbox.blocked:
                            raise ValueError("队列修改保存未确认；请恢复此会话或明确丢弃待处理要求后继续")
                        if self._input_retry and self._input_retry.delivery == "queue":
                            raise ValueError("排队输入保存尚未确认；请用相同 /queue 正文重试，或明确丢弃/恢复会话")
                        pending = self.pending_state()
                        self.messages, result = self.agent.recover_inputs(
                            self.messages, [UserInput(**row) for row in pending["pending"] if row.get("delivery", "steer") == "steer"])
                        for row in pending["pending"]:
                            if row.get("delivery") == "queue":
                                self._input_inbox.append(UserInput(**row))
                        self.recovered_inputs = input_state()
                        self._input_retry = None
                        result["queued_inputs"] = len(self._input_inbox.batch(delivery="queue"))
                        self.ui_call(self.set_task_result, 2, "引导已加入对话；排队项保留，/queue run 才启动队列")
                    elif action == "discard":
                        if self.confirm("丢弃本地保存的待处理要求？这不会运行任务，也不会撤销工作区操作。") is True:
                            result = (self.get_store().discard_inputs(self.session_name) if self.session_name
                                      else {"discarded_inputs": len(self.pending_tasks), "network_contacted": False})
                            self.recovered_inputs = input_state()
                            self.pending_tasks = []
                            self._input_retry = None
                        else:
                            result = {"approved": False, "network_contacted": False}
                    else:
                        result = input_preview(self.pending_state(), min(4096, self.config.max_output))
                elif name == "sessions":
                    result = self.get_store().scan(cancel_event=self.cancel_event)
                    if result["limited"]:
                        status, message = 2, "会话列表达到上限，结果可能不完整"
                elif name == "prune-sessions":
                    result = self.get_store().prune(self.confirm, cancel_event=self.cancel_event, **arguments)
                    if result.get("declined") or result.get("skipped"):
                        status, message = 2, "会话清理未全部执行，详见本地结果"
                elif name in {"changes", "undo", "redo", "discard-history"}:
                    result = self.tools.change_action(name)
                    if result.get("changed") or result.get("outcome") == "uncertain":
                        # Invalidate before checkpointing: a failed save cannot
                        # make a successful file restoration appear verified.
                        self.ui_call(self.invalidate_task)
                        status, message = 2, "本地文件已变更；先前验收失效，请检查并重新验收"
                    self.agent.record_workspace_change(self.messages, result)
                elif name == "attach":
                    self.messages, result = self.agent.attach_files(arguments["paths"], self.messages)
                elif name == "references":
                    self.messages, result = self.agent.update_references(messages=self.messages, **arguments)
                elif name == "summary":
                    result = self.agent.summarize(self.messages, **arguments)
                elif name == "clear-summary":
                    result = self.agent.clear_summary(self.messages)
                elif name == "logs":
                    result = self.tools.log_control(**arguments)
                elif name == "skills":
                    result = self.tools.skills.catalog()
                elif name == "web-status":
                    result = self.tools.web.status()
                elif name == "mcp-status":
                    result = self.tools.mcp_manager.status()
                elif name == "model-catalog-refresh":
                    if self.model_selection is None:
                        raise ValueError("当前启动没有模型选择快照")
                    result = self.model_selection.refresh(self.agent)
                    message = "模型目录已刷新；当前模型和策略保持，选择目标后仍需确认"
                elif name == "model-switch":
                    candidate = ModelSwitch(self.model_selection, self.agent, self.messages, arguments["target"],
                                            saving=bool(self.session_name) or self.config.auto_save_session,
                                            provider_factory=self.provider_factory)
                    if not candidate.changed:
                        result = {"model_changed": False, "network_contacted": False}
                    elif candidate.preflight(self.confirm, self.session_dir):
                        result = self.ui_call(self.commit_model, candidate)
                    else:
                        result = {"approved": False, "model_changed": False, "network_contacted": False}
                elif name == "timeline":
                    result = timeline_page(self.messages, arguments["offset"], secrets=self.config.secrets,
                                           budget=min(self.config.max_output, 8192))
                elif name == "conversation-revise":
                    self._revision_ready = False
                    source = arguments["source"]
                    if self.agent is not source.original or self.messages is not source.source:
                        raise ValueError("历史来源已切换；编辑草稿保留，请重新选择")
                    candidate = source.prepare(arguments["text"], saving=bool(self.session_name) or self.config.auto_save_session,
                                               source_name=self.session_name)
                    if candidate.preflight(self.confirm, self.session_dir):
                        result = self.ui_call(self.commit_revision, candidate)
                    else:
                        result = {"approved": False, "revised": False, "network_contacted": False}
                elif name == "conversation-fork":
                    candidate = ConversationFork(self.agent, self.messages, arguments["target"],
                                                 saving=bool(self.session_name) or self.config.auto_save_session,
                                                 source_name=self.session_name)
                    if candidate.preflight(self.confirm, self.session_dir):
                        result = self.ui_call(self.commit_fork, candidate)
                    else:
                        result = {"forked": False, "approved": False, "network_contacted": False}
                elif name in {"worktree-create", "worktree-list"}:
                    from .worktrees import worktree_action
                    result = worktree_action(self.config, self.confirm, cancel_event=self.cancel_event, **arguments)
                else:
                    result = self.tools.execute(name, arguments)
                self.emit(("[固定检查观察；下次明确请求模型时发送]\n" if name == "verify" else
                           "[显式摘要；不执行任务]\n" if name == "summary" else "[本地操作结果，不自动上传]\n")
                          + json.dumps(result, ensure_ascii=False, indent=2))
                if "error" in result or result.get("approved") is False or result.get("returncode", 0) != 0:
                    status, message = 2, "本地操作未完成"
            else:
                if self._queue_next:
                    self._queue_next = False
                    entries = self._input_inbox.claim(1, delivery="queue")
                    if not entries or entries[0].id not in self._queue_scope:
                        self._input_inbox.release(entries)
                        raise ValueError("排队项已变化；没有启动新的模型请求")
                    if self.messages is None:
                        self.messages = []  # Own history before any uncertain checkpoint.
                    try:
                        self.messages, _ = self.agent.recover_inputs(self.messages, entries)
                    except BaseException:
                        if not any(entry.id in delivered_inputs(self.messages) for entry in entries):
                            self._input_inbox.release(entries)
                        else:
                            self._input_inbox.blocked = True
                        raise
                    self.emit("[queue] 开始下一项；本项最多 " + str(self.config.max_steps) + " 次模型请求，工具仍逐次批准")
                options = {"context_files": context_files} if context_files else {}
                if preflight:
                    options["preflight"] = True
                self.messages, complete = self.agent.run(task, self.messages, **options)
                status = 0 if complete else 2
                message = "验收通过" if complete and self.config.checks else ("本轮结束" if complete else "未完成 / 未验收")
                if not complete and self.agent._stop_reason == "question":
                    message = "问题未回答；等待新输入，旧提案未执行 / 未验收"
        except KeyboardInterrupt:
            status, message = 130, "已取消；待定操作不会重放"
            if local and local[0] in {"conversation-revise", "model-catalog-refresh"}:
                return status, message  # Local preparation must not recopy history or deliver guidance.
            try:
                if self.messages:
                    self.messages = validate_history(self.messages)
                    # Preserve guidance even when the user explicitly stops work.
                    if self.agent._steer(self.messages):
                        message += "；新要求已保留，点击继续执行"
                    if self.store and self.session_name:
                        self.store.save(self.session_name, self.messages, **self.agent.reference_save_options())
            except (OSError, SessionError, RuntimeError) as error:
                status, message = 1, "已取消，但会话保存失败: " + self.safe(error)
        except Exception as error:
            status, message = 1, "错误: " + self.safe(error)
            if local and local[0] in {"conversation-revise", "model-catalog-refresh"}:
                return status, message  # Failed local preparation leaves history and pending guidance intact.
            # Keep the in-memory conversation usable after a failed checkpoint or action.
            try:
                if self.messages:
                    self.messages = validate_history(self.messages)
                    self.agent._steer(self.messages)
            except (OSError, SessionError, RuntimeError) as recovery_error:
                message += "；会话恢复或保存失败: " + self.safe(recovery_error)
        if local and self.local_command_approved and local[0] != "verify":
            try:
                self.tools.read_versions.clear()
                self.tools.index.clear()
                # Keep only the invalidation notice in model history; command
                # output from the local operation is still not auto-uploaded.
                self.agent.record_workspace_change(self.messages,
                                                   {"action": "command", "path": ".", "outcome": "uncertain"})
            except Exception as error:
                status, message = 1, "本地命令后会话保存失败: " + self.safe(error)
        return status, message

    def on_unmount(self):
        self._unmounted = True
        self.tools.jobs.request_cancel_all()
        self.cancel_event.set()
        with self._output_condition:
            self._output_condition.notify_all()

    def finished(self, status, message):
        self.flush_output()
        if self.local_action == "conversation-revise":
            accepted, self._revision_ready = self._revision_ready and status == 0, False
            if self._revision_editor is not None:
                self._revision_editor.operation_finished(accepted)
            self.last_operation_status = status
            self.local_action = None
            self._queue_running = False
            if accepted and not self.cancel_event.is_set() and not self._input_retry and not self._input_inbox.blocked:
                self.set_task_result(2, "分支已建立，正在发送编辑后的请求；主草稿保留")
                self.set_state("发送编辑后的请求")
                return True
            self.set_busy(False)
            self.set_state(message + "；未自动请求模型，编辑区或已提交分支保留")
            self.append("\n[" + message + "；未自动请求模型]\n")
            if self.quit_when_idle:
                self.exit(self.last_status)
            return False
        verification = self.local_action == "verify"
        local_only = verification or self.local_action == "model-catalog-refresh"
        if verification and (self._input_inbox.batch(delivery="steer") or self._input_retry or self._input_inbox.blocked):
            status, message = 2, "检查后有新要求或保存待确认；未请求模型，请明确继续或处理 /pending"
        if self._queue_running and self.agent.had_rejected_action and status == 0:
            status, message = 2, "本项有操作被拒绝；排队任务已暂停，结果待确认"
        if self.local_action is not None:
            self.last_operation_status = status
        if status != 0 or self.agent.had_rejected_action or self.cancel_event.is_set() or self._input_retry or self._input_inbox.blocked:
            self._queue_running = False
        if not local_only and self._input_inbox.batch(delivery="steer") and not self._input_retry and not self._input_inbox.blocked and status in (0, 2) and not self.cancel_event.is_set():
            self.local_action = None
            self.set_task_result(2, "新要求待处理，等待更新后的结果")
            self.set_state(f"继续处理 {len(self.pending_tasks)} 条新要求")
            return True
        queued = self._input_inbox.batch(1, delivery="queue")
        if self.local_action is None and status == 0 and self._queue_running and queued and queued[0].id in self._queue_scope:
            self._queue_next = True
            self.set_task_result(2, "按明确请求继续下一条排队任务")
            return True
        self._queue_running = False
        if self.local_action is None or verification:
            self.set_task_result(status, message)
        self.local_action = None
        self.set_busy(False)
        if self.pending_tasks:
            queued = len(self._input_inbox.batch(delivery="queue"))
            message += f"；{queued} 条排队未执行，/queue run 启动" if queued else f"；{len(self.pending_tasks)} 条引导待处理，点击继续"
        if self._input_retry:
            message += "；一条输入保存未确认，请用 /pending 处理或重试原文"
        self.set_state(message)
        self.event(message)
        self.append("\n[" + message + "]\n")
        if self.quit_when_idle:
            self.exit(self.last_status)
        return False

    def action_focus_task(self):
        if isinstance(self.screen, QuestionScreen):
            self.screen.query_one("#question-guidance", Input).focus()
        elif isinstance(self.screen, ApprovalScreen) and self.screen.allow_steering:
            self.screen.query_one("#approval-task", TextArea).focus()
        elif not isinstance(self.screen, ModalScreen):
            self.query_one("#task", TextArea).focus()

    def action_submit(self):
        if isinstance(self.screen, QuestionScreen):
            self.screen.send_guidance()
        elif isinstance(self.screen, ApprovalScreen) and self.screen.allow_steering:
            self.screen.query_one("#approval-task", TaskInput).action_send()
        elif not isinstance(self.screen, ModalScreen):
            self.query_one("#task", TaskInput).action_send()

    def open_picker(self, *, commands=False, query="", editor=None, command=None, mention=None):
        if self.busy or isinstance(self.screen, ModalScreen):
            self.notify("请等待当前任务结束后选择文件或命令", severity="warning")
            return

        draft = editor.text if editor is not None else None

        def selected(result):
            if result is None or self.busy:
                return
            kind, value = result
            if editor is not None:
                if editor.text != draft:
                    self.notify("草稿已变化，请重新选择文件或命令", severity="warning")
                    return
                if mention is not None:
                    editor.files.sync(draft)
                    try:
                        candidate = editor.files.select(*mention, value)
                    except ValueError as error:
                        self.reject_input(str(error))
                        return
                    token = "@" + json.dumps(value, ensure_ascii=False)
                    editor.replace(token, editor.document.get_location_from_index(mention[0]),
                                   editor.document.get_location_from_index(mention[1]),
                                   maintain_selection_offset=False)
                    editor.files = candidate
                    editor.move_cursor(editor.document.get_location_from_index(
                        mention[0] + len(token)))
                    editor.focus()
                    return
                if kind != "command" and value != value.strip():
                    self.notify("路径含首尾空白，请用“选择文件”直接添加；输入未修改", severity="warning")
                    return
                editor.load_text((value if kind == "command" else command + " " + value) + (" " if kind == "command" else ""))
                editor.move_cursor(editor.document.end)
                editor.focus()
            elif kind == "attach":
                self.start(local=("attach", {"paths": list(value) if isinstance(value, tuple) else [value]}))
            else:
                self.start(local=("references", {"paths": list(value) if isinstance(value, tuple) else [value], "operation": "add"}))

        self.push_screen(PickerScreen(commands=commands, query=query, complete_only=editor is not None), selected)

    def refresh_models(self):
        if self.busy or isinstance(self.screen, ModalScreen):
            self.notify("请等待当前操作结束后刷新模型目录", severity="warning")
        elif self.model_selection is None:
            self.notify("当前启动没有模型选择快照", severity="warning")
        else:
            self.start(local=("model-catalog-refresh", {}))

    def choose_model(self, target=""):
        if self.busy or isinstance(self.screen, ModalScreen):
            self.notify("请等待当前任务结束后切换模型", severity="warning")
            return
        if self.model_selection is None:
            self.notify("当前启动没有模型选择快照；请从配置启动 Sprig", severity="warning")
            return
        if target:
            self.start(local=("model-switch", {"target": target}))
            return
        choices = [(row["key"], ("当前  " if row["provider"] == self.config.provider_name and row["id"] == self.config.model else "")
                    + row["key"] + "  " + row["id"]) for row in self.model_selection.catalog()]
        def selected(result):
            if result and not self.busy:
                self.choose_model(result[1])
        self.push_screen(PickerScreen(commands=True, choices=choices,
                                     title="选择配置中的模型；Enter 后确认，Esc 保留当前模型和草稿"), selected)

    def choose_fork(self, target=""):
        if self.busy or isinstance(self.screen, ModalScreen):
            self.notify("请等待当前任务结束后建立分支", severity="warning")
            return
        if target:
            self.start(local=("conversation-fork", {"target": target}))
            return
        try:
            self.tools.root.ensure_open()
            choices = [("current", "current  保留整个当前对话，建立新分支")]
            choices.extend((str(row["message"]), f"消息 {row['message']}–{row['through_message']}  {row['preview']}")
                           for row in turn_catalog(self.messages, self.config.secrets))
        except (OSError, ValueError, SessionError) as error:
            self.notify(self.safe(error), severity="error")
            return
        def selected(result):
            if result and not self.busy:
                self.choose_fork(result[1])
        self.push_screen(PickerScreen(commands=True, choices=choices,
                                     title="历史分支：保留到该轮结束；搜索消息号/预览，Enter 后确认，Esc 关闭"), selected)

    def choose_revision(self, target="", text=None):
        if self.busy or isinstance(self.screen, ModalScreen) or self.pending_tasks or self.recovered_inputs["pending"] or self._input_retry or self._input_inbox.blocked:
            self.notify("请等待空闲并处理待发送要求后，再编辑历史请求", severity="warning")
            return
        self.cancel_event.clear()
        try:
            if not target:
                choices = [(str(row["message"]), f"编辑消息 {row['message']}：{row['preview']}") for row in turn_catalog(self.messages, self.config.secrets)]
                def selected(result):
                    if result and not self.busy:
                        self.choose_revision(result[1])
                self.push_screen(PickerScreen(commands=True, choices=choices, title="选择旧请求；该请求和后续对话将被替换，工作区不回滚"), selected)
                return
            source = RevisionSource(self.agent, self.messages, target)
            editor = RevisionScreen(self, source, text)
            self._revision_editor = editor
            def closed(_):
                if self._revision_editor is editor:
                    self._revision_editor = None
            self.push_screen(editor, closed)
        except (OSError, ValueError, SessionError) as error:
            self.notify(self.safe(error), severity="error")

    def commit_revision(self, candidate):
        result = self.commit_fork(candidate)
        self._draft_submission = None  # The main draft is unrelated to this editor submission.
        self._revision_ready = True
        return {**result, "revised": True, "replaced_message": candidate.replaced}

    def commit_fork(self, candidate):
        if (self._unmounted or self._closed or self.cancel_event.is_set() or self.pending_tasks or self.recovered_inputs["pending"] or self._input_retry or self._input_inbox.blocked
                or self.agent is not candidate.original or self.messages is not candidate.source):
            raise ValueError("分支已取消或被新要求取代；原对话和草稿保留")
        old_store, pending_draft = self.store, self._draft_submission
        self.agent, self.messages, self.store, self.session_name = candidate.commit(self.session_dir)
        latest_user = next((row for row in reversed(self.messages) if user_request(row)), {})
        self._draft_submission = pending_draft if pending_draft is not None and latest_user.get("content") == redact(pending_draft[2], self.config.secrets) else None
        self.query_one("#session-name", Input).value = self.session_name or ""
        self.refresh_plan()
        self.set_task_result(2, "已建立对话分支，工作区未回滚，尚未重新验收")
        self.transcript = ""
        self._transcript_trimmed = False
        self.query_one("#transcript", TextArea).load_text("")
        self.append("已建立历史分支。工作区文件保持现状，" + ("接下来发送已确认的编辑请求。\n" if candidate.replaced else "下一次 Enter 才请求模型。\n"))
        self.show_history()
        result = {"forked": True, "session": self.session_name, "through_message": candidate.through,
                  "workspace_rolled_back": False, "network_contacted": False}
        if old_store is not None:
            try:
                old_store.close()
            except OSError:
                result["warning"] = "branch active, but old session lock cleanup failed; exit before resuming that file elsewhere"
        return result

    def commit_model(self, candidate):
        if (self._unmounted or self._closed or self.cancel_event.is_set() or self.pending_tasks or self.recovered_inputs["pending"] or self._input_retry or self._input_inbox.blocked
                or self.agent is not candidate.original):
            raise ValueError("模型切换已取消或被新要求取代；原模型与草稿保留")
        old_store = self.store
        pending_draft = self._draft_submission
        self.agent, self.messages, self.store, self.session_name = candidate.commit(self.session_dir)
        self.config = self.agent.config
        latest_user = next((row for row in reversed(self.messages or []) if user_request(row)), {})
        self._draft_submission = pending_draft if pending_draft is not None and latest_user.get("content") == redact(pending_draft[2], self.config.secrets) else None
        self.query_one("#session-name", Input).value = self.session_name or ""
        self.query_one("#context", Static).update(self.safe(
            f"{self.config.model} | {self.config.base_url}\n{self.config.workdir} | {'只读模式' if self.config.read_only else '编辑模式'} | 验收命令: {len(self.config.checks)}"))
        self.refresh_plan()
        self.set_task_result(2, "模型已切换，尚未重新验收")
        self.event("模型已切换；下一次任务才请求目标模型，旧操作不重放")
        result = {"model_changed": True, "provider": self.config.provider_name, "model": self.config.model,
                  "wire_api": self.config.wire_api, "session": self.session_name, "network_contacted": False,
                  "old_runtime_metadata_removed": candidate.dropped, "previous_usage": candidate.original.usage_report()}
        if old_store is not None:
            try:
                old_store.close()
            except OSError:
                result["warning"] = "model changed, but old session lock cleanup failed; exit before resuming that file elsewhere"
        return result

    @on(TaskInput.Complete)
    def complete_input(self, event):
        event.stop()
        text = event.editor.text
        if event.editor.id != "task" or isinstance(self.screen, ModalScreen) or self.busy:
            self.screen.focus_next()
            return
        cursor = event.editor.document.get_index_from_location(event.editor.cursor_location)
        mention = mention_query(text, cursor) if not text.startswith("/") else None
        if mention is not None and event.editor.selection.is_empty:
            self.open_picker(query=mention[2], editor=event.editor, mention=mention[:2])
            return
        if "\n" in text or not text.startswith("/") or text.startswith("//"):
            self.screen.focus_next()
            return
        command, separator, query = text.partition(" ")
        if separator and command in {"/add", "/ref"}:
            self.open_picker(query=query, editor=event.editor, command=command)
        elif not separator:
            self.open_picker(commands=True, query=text, editor=event.editor)
        else:
            self.screen.focus_next()

    @on(TaskInput.Submitted)
    def submitted(self, event):
        event.stop()
        try:
            self._submitted(event)
        finally:
            event.editor._sending = False

    def reject_input(self, reason):
        self.notify(reason, title="输入未发送，草稿保留", severity="warning")
        self.append("\n[输入未发送，草稿保留] " + reason + "\n")

    def _submitted(self, event):
        task = event.text
        if isinstance(self.screen, ModalScreen) and not (
                isinstance(self.screen, ApprovalScreen) and self.screen.allow_steering):
            return
        limit = self.config.max_context_bytes
        templated = False
        try:
            if not event.literal:
                expanded = expand_prompt(self.config, task)
                if expanded is not None:
                    task, templated = expanded, True
            if len(task) > limit or len(task.encode("utf-8")) > limit:
                raise ValueError(f"单条输入超过 {limit} UTF-8 字节；请缩短或保存到工作区文件后按范围读取。")
        except UnicodeEncodeError:
            self.reject_input("输入包含无效 Unicode。")
            return
        except ValueError as error:
            self.reject_input(str(error))
            return
        command = "" if templated else task.strip()
        queue_mode = getattr(event, 'queue_paste_mode', None)
        if command.split(None, 1)[:1] == ['/queue'] and (not event.literal or queue_mode is not None):
            if event.files:
                self.reject_input("排队项当前接收文本；文件选择与草稿保留，请在对应任务中显式附加或按文件名读取")
                return
            try:
                if queue_mode is not None:
                    body = task.lstrip()[len('/queue'):].removeprefix(' ')
                    if queue_mode == 'json':
                        body = queue_command('add ' + body.lstrip().removeprefix('add '))['text']
                    accepted = self.queue_guidance(body, delivery='queue')
                else:
                    accepted = self.slash_command(task.lstrip())
            except (ValueError, TypeError, RecursionError):
                accepted = False
                self.reject_input("排队输入无效；草稿保留，/queue add 后需为一个 JSON 字符串")
            if accepted and event.editor.text == event.draft:
                event.editor.load_text('')
            return
        if not event.literal and command.startswith("//"):
            task = command[1:]  # A literal task starting with a slash.
        elif not event.literal and command.startswith("/"):
            if event.editor.text == event.draft:
                event.editor.load_text("")
            self.slash_command(command)
            return
        if event.files and (self.busy or self.pending_tasks or self._input_retry or self.recovered_inputs["pending"]):
            self.reject_input("文件选择只能随空闲时的新消息发送；执行中可发送普通文本引导。")
            return
        if (self.busy and self._draft_submission is not None
                and self._draft_submission == (event.editor.id, event.draft, task)):
            self.set_state("请求仍在准备；可编辑输入发送新要求")
            return
        if ((self.busy or self.pending_tasks) and (len(self.pending_tasks) >= self.MAX_PENDING_TASKS
                or sum(len(item.encode("utf-8")) for item in self.pending_tasks) + len(task.encode("utf-8")) > limit)):
            self.reject_input(f"待处理引导最多 {self.MAX_PENDING_TASKS} 条、合计 {limit} UTF-8 字节；请等待处理后重发。")
            return
        if self.busy or self._input_retry:
            if self.queue_guidance(task) and event.editor.text == event.draft:
                event.editor.load_text("")
                if not self.busy:
                    self.start()
        else:
            previous = self._draft_submission
            latest_user = next((message for message in reversed(self.messages or []) if user_request(message)), {})
            retry = previous is not None and previous[2] == task and latest_user.get("content") == redact(task, self.config.secrets)
            self._draft_submission = (event.editor.id, event.draft, task)
            self.start(None if retry else task, context_files=event.files, preflight=event.preflight or templated)

    def queue_guidance(self, task, *, delivery="steer"):
        if self._input_inbox.blocked:
            self.reject_input("队列修改保存未确认；请恢复当前会话后再发送。草稿保留。")
            return False
        if self.recovered_inputs["pending"]:
            self.reject_input("请先处理 /pending apply 或 /pending discard；新要求未接收，草稿保留。")
            return False
        if self._input_retry and (self._input_retry.text != redact(task, self.config.secrets) or self._input_retry.delivery != delivery):
            self.reject_input("上一条要求的保存尚未确认；请重试原文，或 /pending apply、/pending discard 处理后再发送。")
            return False
        limit = self.config.max_context_bytes
        try:
            if (not task.strip() or len(task) > limit or len(task.encode("utf-8")) > limit
                    or len(self.pending_tasks) >= self.MAX_PENDING_TASKS
                    or sum(len(item.encode("utf-8")) for item in self.pending_tasks) + len(task.encode("utf-8")) > limit):
                raise ValueError("新要求为空或超过待处理引导的数量/UTF-8 字节上限；请缩短后重发。")
        except (ValueError, UnicodeError) as error:
            self.reject_input(str(error))
            return False
        try:
            text = redact(task, self.config.secrets)
            entry = self._input_retry if self._input_retry and self._input_retry.text == text else new_input(text, delivery=delivery)
            state = self._input_inbox.snapshot()
            if not any(row["id"] == entry.id for row in state["pending"]):
                input_state({"version": 1, "pending": [*state["pending"], entry.row()]})
            self._input_retry = entry
            # Publish only after the private atomic save succeeds. Retrying an
            # uncertain save reuses the identity, so an accepted input stays unique.
            if self.store and self.session_name:
                self.store.enqueue_input(self.session_name, entry, history=self.messages if self.messages is not None else
                                         [{"role": "system", "content": self.agent._prompt()}],
                                         **self.agent.reference_save_options())
            self._input_inbox.append(entry)
            self._input_retry = None
        except (OSError, ValueError, SessionError) as error:
            self.reject_input("要求未接收；保存失败，请保留草稿后重试：" + self.safe(error))
            return False
        if delivery == "queue":
            self.append("\n你[排队]> " + task + "\n[已收存排队项"
                        + ("并保存" if self.store and self.session_name else "；仅当前进程内存")
                        + "；不打断当前任务或批准。/queue run 明确启动，/queue pause 暂停后续项]\n")
            return True
        self.set_task_result(2, "新要求待处理，等待更新后的结果")
        self.streaming = False
        self.append("\n你> " + task + "\n")
        self.append("[已接收新要求" + ("并保存" if self.store and self.session_name else "（仅当前进程内存）")
                    + "，将在下一个安全节点调整当前任务]\n")
        self.event(f"收到执行中引导，待处理 {len(self.pending_tasks)} 条")
        self.set_state(f"已接收 {len(self.pending_tasks)} 条新要求，等待安全节点")
        if self.approval:
            self.approval.dismiss(False)
        if self.question:
            self.question.settle(None)
        return True

    def run_queue(self):
        if self.recovered_inputs["pending"] or self._input_retry or self._input_inbox.blocked:
            self.reject_input("请先处理 /pending apply、重试未确认保存或明确丢弃；队列未启动")
            return False
        entries = self._input_inbox.batch(delivery="queue")
        count = len(entries)
        if not count:
            self.append("[queue] 暂无排队项；/queue 正文 只收存，不请求模型。\n")
            return True
        if self.busy and self.local_action is not None:
            self.reject_input("请等待当前本地操作结束后再 /queue run")
            return False
        self._queue_running = True
        self._queue_scope = frozenset(entry.id for entry in entries)
        self.append(f"[queue] 已明确请求顺序执行 {count} 项；每项最多 {self.config.max_steps} 次模型请求。"
                    "当前调用结束后处理；未配置 checks 时仅表示模型回复结束。拒绝、取消或未完成时暂停；"
                    "新增或编辑过的排队项须再次 /queue run。\n")
        if not self.busy:
            self._queue_next = True
            self.start()
        return True

    def action_cancel(self):
        self._queue_running = False
        had_jobs = self.tools.jobs.unsettled()
        self.tools.jobs.request_cancel_all()
        if not self.busy:
            if had_jobs:
                self.set_state("已请求停止后台任务；使用 /jobs 查看清理结果")
            return
        self.cancel_event.set()
        with self._output_condition:
            self._output_condition.notify_all()
        if self.approval:
            self.approval.dismiss(False)
        if self.question:
            self.question.settle(None)
        self.set_state("正在取消；网络等待可能持续至超时")

    def action_quit(self):
        if self.busy:
            self.quit_when_idle = True
            self.action_cancel()
        else:
            self.exit(self.last_status)

    def get_store(self):
        if self.store is None:
            self.store = SessionStore(self.session_dir, self.config)
        return self.store

    def show_history(self):
        self.append("".join("\n" + ("context" if application_origin(message) else message["role"]) + "> " + message["content"] + "\n"
                            for message in self.messages or []
                            if message["role"] in {"user", "assistant"} and message.get("content")))
        self.event("已加载对话内容；未发起模型请求，未重放工具")

    def save(self):
        try:
            if not self.messages:
                raise ValueError("还没有可保存的对话")
            name = self.query_one("#session-name", Input).value.strip()
            store = self.get_store()
            if name != self.session_name:
                state = self.pending_state()
                store.save_new(name, self.messages, inputs=state, **self.agent.reference_save_options())
            else:
                store.save(name, self.messages, **self.agent.reference_save_options())
            if self.session_name and name != self.session_name:
                store.release(self.session_name)
            self.session_name = name
            self.event("已保存会话: " + name + "；后续轮次自动保存")
            self.set_state("会话已保存")
        except (OSError, ValueError, SessionError) as error:
            self.notify(self.safe(error), severity="error")

    def restore(self):
        name = self.query_one("#session-name", Input).value.strip()

        def load(approved=True):
            if approved is not True:
                return
            try:
                history = self.get_store().load(name)
                if self.session_name and name != self.session_name:
                    self.store.release(self.session_name)
                self.messages, self.session_name = history, name
                self.agent.reset_runtime()
                self.agent.restore_plan(history)
                selected = getattr(self.store, "loaded_references", None)
                self.agent.restore_references(selected if isinstance(selected, dict) else None)
                self.agent.restore_summary(self.store.loaded_summary, history)
                self.agent.restore_logs(getattr(self.store, "loaded_logs", None))
                self._draft_submission = None
                self.refresh_plan()
                self.pending_tasks = []
                self.recovered_inputs = self.store.loaded_inputs
                self._input_retry = None
                self.transcript = ""
                self._transcript_trimmed = False
                self.query_one("#transcript", TextArea).load_text("")
                self.append("已恢复本地会话。输入新任务或点击继续。\n")
                self.show_history()
                if self.recovered_inputs["pending"]:
                    self.append("恢复了待处理要求；请先 /pending 查看并 apply 或 discard，然后明确继续。\n")
                self.set_state("已恢复，等待任务")
                self.set_task_result(2, "已恢复历史，未重新验收")
            except (OSError, ValueError, SessionError) as error:
                self.notify(self.safe(error), severity="error")

        if self.messages or self.pending_tasks:
            self.push_screen(ApprovalScreen("恢复会话 " + self.safe(name) + " 将替换当前内存对话和待处理要求。\n未保存的对话将丢失；已保存的会话文件不变。"), load)
        else:
            load()

    def new_conversation(self):
        def reset(approved=True):
            if approved is not True or self.busy:
                return
            # Detach autosave before replacing history: never overwrite the old
            # session on the next turn, and release its interprocess lease.
            if self.store and self.session_name:
                self.store.release(self.session_name)
            self.session_name = None
            self.messages = None
            self.agent.reset_runtime()
            self.agent.restore_plan(None)
            self.agent.restore_references()
            self.agent.restore_summary()
            self.agent.restore_logs()
            self._draft_submission = None
            self.refresh_plan()
            self.pending_tasks = []
            self.recovered_inputs = input_state()
            self._input_retry = None
            self.query_one("#session-name", Input).value = ""
            self.transcript = ""
            self._transcript_trimmed = False
            self.query_one("#transcript", TextArea).load_text("")
            self.set_task_result(2, "新对话，尚未运行任务")
            self.set_state("就绪")
            self.append("新对话已就绪；工作区和已保存会话保留。修改备份仍可查看。\n")
        if self.messages or self.pending_tasks:
            self.push_screen(ApprovalScreen("新建对话将清除当前内存对话并停止向原会话自动保存。\n未保存的对话将丢失；工作区、修改备份和已保存会话保留。"), reset)
        else:
            reset()

    def action_configured_shortcut(self, action):
        # Aliases never approve a modal or replace the fixed composer keys.
        if isinstance(self.screen, ModalScreen):
            if action in {"cancel", "quit"}:
                (self.action_cancel if action == "cancel" else self.action_quit)()
            return
        if action == "cancel":
            self.action_cancel()
        elif action == "quit":
            self.action_quit()
        elif action == "focus_input":
            self.action_focus_task()
        elif action == "toggle_sidebar":
            sidebar = self.query_one("#sidebar")
            sidebar.display = not sidebar.display
        elif action == "help":
            self.slash_command("/help")
        elif action == "new_conversation" and not self.busy:
            self.new_conversation()

    def slash_command(self, text):
        command, _, argument = text.partition(" ")
        argument = argument.strip()
        if text.split(None, 1) and text.split(None, 1)[0] == "/queue":
            command = "/queue"
        if command == "/help":
            self.append("\n本地命令（不发送给模型）：\n"
                        "/help /keys /plan /status /config /models /context /usage /cancel /exit\n"
                        "/skills（已配置技能摘要） /skill NAME [PATH [OFFSET PAGE_SHA256]]（本地预览，不运行脚本）\n"
                        "/mcp（本地状态） /mcp connect|disconnect|tools|resources|templates|prompts SERVER（逐次确认请求）\n"
                        "/web（本地缓存） /web URL（逐次批准公开网页） /web result ID [UTF8偏移]（缓存分页）\n"
                        "/jobs /job start {\"argv\":[\"程序\",\"参数\"]}（新批准） /job status|output|wait|cancel ID\n"
                        "/lsp（本地状态） /lsp definition|references|implementation|hover SERVER PATH 行 列；symbols|diagnostics SERVER PATH；workspace_symbols SERVER QUERY\n"
                        "/continue /new /files /add 文件路径 /ref 文件路径 /unref 文件路径 /refs [clear]\n"
                        "/verify（仅固定检查；逐次批准，观察进入会话；不调用模型或自动继续）\n"
                        "/map [名称或路径查询] /git /diff /compact\n"
                        "/summary（本地预览） /summary create [END]（逐次确认额外请求） /summary clear\n"
                        "/logs（当前会话日志） /log ID [OFFSET]（本地回查） /logs clear（逐次确认清理）\n"
                        "/history 消息编号 [字符偏移]（本地分页原文，系统提示与推理元数据不展示）\n"
                        "/changes /undo /redo /discard-history /sessions /prune [--dry-run]\n"
                        "/save [名称] /restore [名称] /delete [名称]\n"
                        "/pending 查看；/pending manage 管理；show ID [字符偏移] 查看原文；edit ID JSON字符串、drop ID、move ID 位置逐次确认。\n"
                        "/pending apply 仅将引导加入对话，保留排队项；/pending discard 逐次确认全部丢弃。\n"
                        "/continue 继续当前任务；/queue run 才启动排队项。\n"
                        "/queue 正文 仅收存下一项；/queue add JSON字符串 保留空白；/queue run 明确顺序请求模型，/queue pause 暂停后续项。\n"
                        "/pending delivery ID steer|queue 逐次确认切换模式；管理窗口也可使用“模式”按钮。\n"
                        "/worktrees /worktree 新目录路径（完整剩余文本作为路径；不会切换当前对话）\n"
                        "/prompts [OFFSET] 列出任务模板；/模板名 参数直接发送展开任务，执行中作为引导；模板不执行本地操作。\n"
                        "执行中可输入普通消息调整引导。// 开头发送以 / 开头的普通消息。\n"
                        "Tab 补全本地命令或 /add、/ref 的文件参数；普通消息 @关键词 + Tab 选择本轮文件。\n"
                        "选择不会发送任务；Enter 发送时读取文件，修改/删除标记撤销选择；/ref 用于后续轮次实时引用。\n"
                        "长粘贴只折叠显示；F4 或“展开粘贴”查看编辑，完整正文仍计入请求预算。\n"
                        "/model 打开配置模型选择；/model 提供商/别名 或 @startup 明确切换，需确认且不发送请求。恢复仍校验绑定。\n"
                        "/models reload 或侧栏刷新模型目录：重读原私有配置，当前模型保持；新目标仍需选择确认。\n"
                        "/timeline 或 /fork 选择历史轮次；/fork current 或 消息号 明确建立分支。文件不回滚，草稿保留，Enter 才请求模型。\n"
                        "/revise [消息号] 编辑旧请求；编辑区 Enter 后确认建立该请求之前的分支并发送。主草稿保留。\n")
            return
        if command == "/revise":
            try:
                target, body = revision_command(argument, self.config.max_context_bytes)
                self.choose_revision(target or "", body)
            except ValueError as error:
                self.reject_input(str(error))
            return
        if command == "/prompts":
            try:
                self.append("\n" + json.dumps(prompt_catalog(self.config, int(argument) if argument else 0), ensure_ascii=False) + "\n")
            except ValueError as error:
                self.reject_input(str(error))
            return
        if command == "/keys":
            self.append("\n[快捷键] Enter 发送；Shift+Enter / Ctrl+J 换行；F4 展开粘贴；Ctrl+C 取消；Ctrl+Q 退出；Ctrl+L 输入。审批默认拒绝。\n"
                        + json.dumps(dict(self.config.tui_keybindings), ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/files":
            self.open_picker()
            return
        if command == "/plan":
            self.append("\n[任务计划；模型报告，不代表审批或验收]\n"
                        + json.dumps(self.agent.plan_snapshot(), ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/status":
            self.append(f"\n[本地状态] {self.task_message} | 执行中: {self.busy} | 消息: {len(self.messages or [])} | 本次基线: {self.agent.baseline_status} | 编辑后检查: {self.agent.edit_check_status} | 待处理引导: {len(self.pending_tasks)}\n")
            self.append(f"[待恢复要求: {len(self.recovered_inputs['pending'])}；运行状态仅当前进程]\n" + json.dumps(self.agent.runtime_snapshot(), ensure_ascii=False) + "\n")
            self.append(f"[队列: {'已启动' if self._queue_running else '未启动/暂停'}；引导 {len(self._input_inbox.batch(delivery='steer'))} 条；排队 {len(self._input_inbox.batch(delivery='queue'))} 条]\n")
            return
        if command == "/queue":
            try:
                request = queue_command(text.lstrip()[len("/queue"):].removeprefix(" "))
                if request["action"] == "run":
                    return self.run_queue()
                elif request["action"] == "pause":
                    self._queue_running = False
                    self.append("[queue] 后续项已暂停；当前任务继续，可用 Ctrl+C 取消。\n")
                elif request["action"] == "preview":
                    self.append(json.dumps(input_preview(self.pending_state(), min(4096, self.config.max_output)), ensure_ascii=False) + "\n")
                else:
                    return self.queue_guidance(request["text"], delivery="queue")
            except (ValueError, TypeError, RecursionError) as error:
                self.reject_input("排队输入无效；使用 /queue 正文，或 /queue add JSON字符串 保留完整原文")
                return False
            return True
        if command == "/pending":
            try:
                request = pending_command(argument)
                if request["action"] == "manage":
                    self.manage_pending()
                elif request["action"] == "show":
                    result = input_page(self.pending_state(), request["identity"], request["offset"], min(8192, self.config.max_output))
                    self.append(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                elif request["action"] in {"edit", "drop", "move", "delivery"}:
                    self.change_pending(request)
                elif self.busy:
                    if request["action"] == "preview":
                        self.append(json.dumps(input_preview(self.pending_state(), min(4096, self.config.max_output)), ensure_ascii=False, indent=2) + "\n")
                    else:
                        self.notify("请等待当前操作结束", severity="warning")
                else:
                    self.start(local=("pending", {"action": request["action"]}))
            except (ValueError, RuntimeError, OSError, SessionError) as error:
                self.notify(self.safe(error), severity="warning")
            return
        if command == "/refs" and not argument:
            self.append("\n[当前文件引用；准备记录不是已发送证明]\n" + json.dumps(
                redact(self.agent.reference_state(), self.config.secrets), ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/verify":
            if argument:
                self.notify("/verify 不接受命令参数；请用 --check 或 [agent].checks 配置固定 argv", severity="warning")
            elif self.busy:
                self.notify("请等待当前操作结束，或先取消；/verify 不会替换当前操作", severity="warning")
            else:
                self.start(local=("verify", {}))
            return
        if command == "/config":
            details = {"provider": self.config.provider_name,
                       "profile": self.config.profile_name,
                       "config_file": str(self.config.config_path) if self.config.config_path else None}
            self.append("\n[生效配置；密钥值不显示]\n" + json.dumps(public_config(self.config, details), ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/models":
            if argument:
                if argument != "reload":
                    editor = self.query_one("#task", TaskInput)
                    if not editor.text and not isinstance(self.screen, ModalScreen):
                        editor.load_text(text)
                    self.reject_input("用 /models 列出目录，或 /models reload 刷新；未请求模型")
                else:
                    self.refresh_models()
                return
            catalog = self.model_selection.catalog() if self.model_selection is not None else self.config.model_catalog
            self.append("\n[配置中的模型；未联网]\n" + json.dumps(catalog, ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/model":
            self.choose_model(argument)
            return
        if command == "/lsp":
            if self.busy:
                self.notify("请等待当前操作结束；可正常发送文本引导或取消", severity="warning")
                return
            try:
                from .lsp import local_arguments
                self.start(local=("lsp", local_arguments(argument)))
            except ValueError as error:
                self.notify(self.safe(error), severity="warning")
            return
        if command in {"/jobs", "/job"}:
            if self.busy:
                self.notify("请等待当前操作结束；Ctrl+C 可取消当前与后台任务", severity="warning")
                return
            try:
                from .jobs import local_arguments
                if command == "/jobs" and argument:
                    raise ValueError("用法：/jobs 或 /job list OFFSET")
                name, arguments = local_arguments(argument)
                self.start(local=(name, arguments))
            except (ValueError, OSError) as error:
                self.notify(self.safe(error), severity="warning")
            return
        if command == "/web" and not argument:
            if not self.busy:
                self.start(local=("web-status", {}))
            else:
                self.notify("请等待当前操作结束后查看网页缓存", severity="warning")
            return
        if command == "/web":
            from .web_fetch import local_arguments
            if self.busy:
                self.notify("请等待当前操作结束后读取网页；执行中可输入普通引导", severity="warning")
                return
            try:
                self.start(local=("web_fetch", local_arguments(argument)))
            except ValueError as error:
                self.notify(self.safe(error), severity="warning")
            return
        if command in {"/fork", "/timeline"}:
            if command == "/timeline" and argument:
                try:
                    offset = int(argument)
                    if self.busy:
                        raise ValueError("请等待当前任务结束后查看时间线")
                    self.start(local=("timeline", {"offset": offset}))
                except ValueError as error:
                    self.notify(self.safe(error), severity="warning")
            else:
                self.choose_fork(argument)
            return
        if command == "/mcp":
            if self.busy:
                self.notify("请等待任务结束或取消后操作 MCP；可正常发送任务引导", severity="warning")
                return
            try:
                from .mcp import local_arguments
                arguments = local_arguments(argument)
                self.start(local=("mcp-status", {}) if arguments["action"] == "status" else ("mcp", arguments))
            except (ValueError, OSError) as error:
                self.notify(self.safe(error), severity="error")
            return
        if command in {"/skills", "/skill"}:
            if self.busy:
                self.notify("请等待任务结束或取消后查看技能；当前引导仍可正常发送", severity="warning")
                return
            try:
                if command == "/skills" and not argument:
                    self.start(local=("skills", {}))
                elif command == "/skill":
                    from .skills import local_arguments
                    self.start(local=("skill", local_arguments(argument)))
                else:
                    raise ValueError("用法：/skills 或 /skill NAME [PATH [OFFSET PAGE_SHA256]]")
            except (ValueError, OSError) as error:
                self.notify(self.safe(error), severity="error")
            return
        if command in {"/logs", "/log"}:
            if self.busy:
                self.notify("请先等待当前操作结束或取消，再回查命令日志", severity="warning")
                return
            try:
                parts = argument.split()
                if command == "/logs" and not parts:
                    self.start(local=("logs", {}))
                elif command == "/logs" and parts == ["clear"]:
                    self.start(local=("logs", {"action": "clear"}))
                elif command == "/log" and len(parts) in (1, 2):
                    self.start(local=("logs", {"action": "read", "log_id": parts[0],
                                               "offset": int(parts[1]) if len(parts) == 2 else 0}))
                else:
                    raise ValueError("用法：/logs、/logs clear、/log ID [OFFSET]")
            except (ValueError, OSError) as error:
                self.notify(self.safe(error), severity="error")
            return
        if command in {"/summary", "/history"}:
            if self.busy:
                self.append("\n摘要与原文查看需等待当前任务结束；可发送普通引导或 /cancel。\n")
                return
            self.cancel_event.clear()  # A completed cancellation cannot cancel a new local intent.
            try:
                parts = argument.split()
                if command == "/history":
                    from .summary import history_page
                    if len(parts) not in {1, 2}:
                        raise ValueError("用法：/history 消息编号 [字符偏移]")
                    result = history_page(self.messages or [], int(parts[0]), int(parts[1]) if len(parts) == 2 else 0,
                                          budget=min(self.config.max_output, 8192), secrets=self.config.secrets)
                elif not parts:
                    result = self.agent.summary_preview(self.messages)
                elif parts == ["clear"]:
                    self.start(local=("clear-summary", {}))
                    return
                elif parts[0] == "create" and len(parts) <= 2:
                    self.start(local=("summary", {"end": int(parts[1]) if len(parts) == 2 else None}))
                    return
                else:
                    raise ValueError("用法：/summary、/summary create [END]、/summary clear")
                self.append("\n[本地预览，不发送请求]\n" + json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            except (ValueError, OSError, SessionError) as error:
                self.notify(self.safe(error), severity="error")
            return
        if command in {"/cancel", "/exit"}:
            (self.action_cancel if command == "/cancel" else self.action_quit)()
            return
        actions = {"/continue": "continue", "/new": "new", "/map": "map", "/git": "git", "/diff": "diff",
                   "/context": "context-budget", "/usage": "usage", "/compact": "compact",
                   "/changes": "changes", "/undo": "undo", "/redo": "redo", "/discard-history": "discard-history",
                   "/sessions": "sessions", "/save": "save", "/restore": "restore", "/delete": "delete", "/add": "attach", "/prune": "prune-sessions",
                   "/worktrees": "worktree-list", "/worktree": "worktree-create"}
        actions.update({"/ref": "references", "/unref": "references", "/refs": "references"})
        if command not in actions:
            self.append("\n未知本地命令；输入 /help。消息未发送给模型。\n")
            return
        if self.busy:
            self.append("\n该本地操作需要等待当前任务结束；可用 /cancel，或发送普通消息调整任务。\n")
            return
        if argument and command not in {"/map", "/save", "/restore", "/delete", "/add", "/prune", "/worktree", "/ref", "/unref", "/refs"}:
            self.append("\n该命令不接受参数；输入 /help。\n")
            return
        if command in {"/save", "/restore", "/delete"} and argument:
            self.query_one("#session-name", Input).value = argument
        if command in {"/ref", "/unref", "/refs"}:
            if (command == "/refs" and argument != "clear") or (command != "/refs" and not argument):
                self.append("\n用法：/ref 文件路径、/unref 文件路径、/refs 或 /refs clear；引用本身不触发模型。\n")
                return
            operation = "clear" if command == "/refs" else "remove" if command == "/unref" else "add"
            self.start(local=("references", {"operation": operation, "paths": [] if operation == "clear" else [argument]}))
        elif command in {"/worktrees", "/worktree"}:
            if command == "/worktree" and not argument:
                self.append("\n用法：/worktree 新目录路径；父目录须已存在，目标须在当前仓库外且不存在。\n")
                return
            self.start(local=(actions[command], {"path": argument} if argument else {}))
        elif command == "/prune":
            if argument not in {"", "--dry-run"}:
                self.append("\n用法：/prune [--dry-run]；需先配置会话保留策略。\n")
                return
            self.start(local=("prune-sessions", {"dry_run": argument == "--dry-run"}))
        elif command == "/add":
            if not argument:
                self.append("\n用法：/add 工作区相对文件路径；文件只作为快照加入，下一条消息才请求模型。\n")
                return
            self.start(local=("attach", {"paths": [argument]}))
        elif command == "/map" and argument:
            self.start(local=("project_map", {"query": argument}))
        else:
            try:
                self.local_control(actions[command])
            except (OSError, ValueError, SessionError) as error:
                self.append("\n本地操作失败: " + self.safe(error) + "\n")

    @on(Button.Pressed)
    def buttons(self, event):
        self.local_control(event.button.id)

    def local_control(self, identity):
        if self.config.read_only and identity in {"diff", "git", "undo", "redo"}:
            self.append("\n只读模式禁止命令和文件修改。\n")
            return
        if identity == "send":
            self.action_submit()
        elif identity == "files":
            self.open_picker()
        elif identity == "expand-paste":
            self.query_one("#task", TaskInput).action_expand_pastes()
        elif identity == "cancel":
            self.action_cancel()
        elif identity == "pending":
            self.manage_pending()
        elif not self.busy:
            if identity == "continue":
                self.start()
            elif identity == "new":
                self.new_conversation()
            elif identity == "model":
                self.choose_model()
            elif identity == "reload-models":
                self.refresh_models()
            elif identity == "fork":
                self.choose_fork()
            elif identity == "revise":
                self.choose_revision()
            elif identity in {"diff", "git"}:
                argv = ["git", "diff", "--", "."] if identity == "diff" else ["git", "status", "--short", "--", "."]
                self.start(local=("run", {"argv": argv}))
            elif identity == "map":
                self.start(local=("project_map", {}))
            elif identity == "save":
                self.save()
            elif identity == "restore":
                self.restore()
            elif identity == "sessions":
                self.start(local=("sessions", {}))
            elif identity in {"changes", "undo", "redo", "discard-history"}:
                self.start(local=(identity, {}))
            elif identity == "context-budget":
                self.append("\n[上下文预算；本地预览]\n" + json.dumps(self.agent.context_preview(self.messages), ensure_ascii=False, indent=2) + "\n")
            elif identity == "usage":
                self.append("\n[请求统计；本地记录，缺失用量为未知]\n" + json.dumps(self.agent.usage_report(), ensure_ascii=False, indent=2) + "\n")
            elif identity == "compact":
                preview = self.agent.context_preview(self.messages)

                def compact(approved):
                    if approved is True:
                        self.agent.compact_context = True
                        self.event("已启用请求上下文压缩；保存的完整对话保留")

                self.push_screen(ApprovalScreen(f"按配置阈值压缩旧成功工具输出及已批准完成的旧编辑正文；限额内可保留少量完整源码快照，仍超限时折叠已完成的旧只读调用。用户要求、失败、拒绝和最近 {self.config.context_keep_recent_messages} 条消息保留。完整会话文件保留；下方为强制触发预览。\n"
                                               + json.dumps(preview, ensure_ascii=False, indent=2)), compact)
            elif identity == "delete":
                name = self.query_one("#session-name", Input).value.strip()

                def delete(approved):
                    if approved is True:
                        try:
                            self.get_store().delete(name)
                            if name == self.session_name:
                                self.session_name = None
                            self.event("已删除本地会话: " + name)
                        except (OSError, ValueError, SessionError) as error:
                            self.notify(self.safe(error), severity="error")

                self.push_screen(ApprovalScreen("删除本地会话 " + self.safe(name) + "？\n保存的对话文件将删除；当前内存对话保留。"), delete)

    def close(self):
        # Textual cancels the async wrapper without joining its worker thread.
        # Keep filesystem/session handles alive until that thread has stopped.
        self.cancel_event.set()
        self.tools.jobs.request_cancel_all()
        with self._output_condition:
            self._output_condition.notify_all()
        cleanup_timeout = max(self.config.timeout, self.config.request_timeout or 0)
        if not self._lifecycle_lock.acquire(timeout=min(60, cleanup_timeout + 5)):
            raise TimeoutError("worker cleanup is still pending; filesystem handles remain open")
        try:
            self._closed = True
            self.tools.close()
            if self.store:
                self.store.close()
        finally:
            self._lifecycle_lock.release()


def run_tui(config, task=None, session_dir=None, save_session=None, resume=None, change_dir=None, show_metrics=False, files=(), model_selection=None):
    app = SprigApp(config, task, session_dir, save_session, resume, change_dir=change_dir, files=files, model_selection=model_selection)
    try:
        return app.run() or 0
    finally:
        app.close()
        if show_metrics:
            print("[usage] " + json.dumps(app.agent.usage_report(), ensure_ascii=False), file=sys.stderr, flush=True)
