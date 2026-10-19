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
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static, TabbedContent, TabPane, TextArea
from textual.worker import get_current_worker

from .agent import Agent
from .changes import ChangeHistory
from .config import SHORTCUT_ACTIONS, redact, terminal_text
from .provider import Provider
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


class TaskInput(TextArea):
    """Chat composer: Enter sends; modified Enter inserts a newline."""

    BINDINGS = [
        Binding("enter", "send", "发送", priority=True),
        Binding("shift+enter,ctrl+j", "newline", "换行", priority=True),
    ]

    class Submitted(Message):
        def __init__(self, editor, text, draft=None):
            super().__init__()
            self.editor, self.text = editor, text
            self.draft = editor.text if draft is None else draft

    def action_send(self):
        text = self.text.strip()
        if text and not getattr(self, "_sending", False):
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
    #task-buttons Button { width: 1fr; margin-right: 1; }
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

    def __init__(self, config, task=None, session_dir=None, save_session=None, resume=None, provider=None, change_dir=None, files=()):
        super().__init__()
        self.config = config
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
        # UI-thread-owned queue. Only the single agent worker takes its contents.
        self.pending_tasks = []
        self._draft_submission = None
        if self.session_name:
            self.store = SessionStore(self.session_dir, config)
            try:
                if resume:
                    self.messages = self.store.load(resume)
                else:
                    self.store.ensure_new(save_session)
            except BaseException:
                self.store.close()
                raise
        app = self

        class UiTools(Tools):
            def execute(self, name, arguments):
                result = super().execute(name, arguments)
                if result.get("approved") is False and app.ui_call(lambda: bool(app.pending_tasks)):
                    note = ("Stopped after a workspace change; inspect current files: " if result.get("changed") or result.get("outcome")
                            else "Not executed: ")
                    result = {**result, "skipped": True, "error": note +
                              "superseded by new user guidance. Follow the updated request and request fresh approval for any new action."}
                summary = {key: result[key] for key in ("approved", "changed", "returncode", "timed_out", "output_limited",
                                                       "outcome", "partial", "copied", "source_removed", "path", "destination", "error") if key in result}
                if app._ui_ready and not app._unmounted:
                    app.ui_call(app.event, name + ": " + json.dumps(summary, ensure_ascii=False))
                    if name == "update_plan":
                        app.ui_call(app.refresh_plan)
                return result

        history = None
        try:
            history = ChangeHistory(config, change_dir)
            self.tools = UiTools(config, self.confirm, self.cancel_event, self.emit_command, history=history)
        except BaseException:
            if history:
                history.close()
            if self.store:
                self.store.close()
            raise
        try:
            self.agent = Agent(config, provider or Provider(config), self.tools, self.emit,
                               checkpoint=self.checkpoint, emit_delta=self.emit_delta, cancel_event=self.cancel_event,
                               receive_input=self.receive_input, on_request_ready=self.request_ready)
            self.agent.restore_plan(self.messages)
            if resume and self.store:
                selected = getattr(self.store, "loaded_references", None)
                self.agent.restore_references(selected if isinstance(selected, dict) else None)
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
                yield TaskInput(placeholder="输入消息；Enter 发送，Shift+Enter / Ctrl+J 换行；执行中可追加要求", id="task")
                with Horizontal(id="task-buttons"):
                    yield Button("发送 / Enter", id="send", variant="primary")
                    yield Button("取消 / Ctrl+C", id="cancel", disabled=True)
            with Vertical(id="sidebar"):
                yield Static("就绪", id="state", markup=False)
                yield Static(self.safe("任务结果：" + self.task_message), id="task-state", markup=False)
                with TabbedContent(id="side-tabs"):
                    with TabPane("操作", id="actions-tab"):
                        with VerticalScroll(id="operations"):
                            yield Button("继续当前对话", id="continue")
                            yield Button("新建对话", id="new")
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
        if self.initial_task:
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
        for identity in ("continue", "new", "diff", "git", "map", "context-budget", "usage", "compact", "changes", "undo", "redo", "discard-history", "save", "restore", "sessions", "delete", "session-name"):
            self.query_one("#" + identity).disabled = busy or (self.config.read_only and identity in {"diff", "git", "undo", "redo"})
        self.query_one("#cancel", Button).disabled = not busy
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

    def take_pending(self):
        count = 1 if self.config.steering_mode == "one-at-a-time" else len(self.pending_tasks)
        tasks, self.pending_tasks = self.pending_tasks[:count], self.pending_tasks[count:]
        if tasks:
            self.set_state("已接收新要求，继续当前会话")
        return tasks

    def receive_input(self):
        if self._unmounted:
            tasks, self.pending_tasks = self.pending_tasks, []
            return tasks
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
        if str(text).startswith("["):
            self.ui_call(self.event, text)

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
            if self.pending_tasks:
                done.set()
                return

            def decide(approved):
                answer[0] = approved
                self.approval = None
                if approved and self.local_action in {"run", "worktree-create", "worktree-list"}:
                    # Even a viewing command can invoke Git helpers. Treat its
                    # workspace effects conservatively, including cancellation.
                    self.invalidate_task()
                    self.local_command_approved = True
                done.set()
                self.set_state("处理中")
            self.approval = ApprovalScreen(self.safe(description), allow_steering=True)
            self.set_state("等待确认")
            self.push_screen(self.approval, decide)

        self.ui_call(show)
        while not done.wait(0.05):
            self.check_cancelled()
        self.check_cancelled()
        return answer[0]

    def start(self, task=None, local=None):
        if self.busy:
            return
        if not local and not task and not self.messages and not self.pending_tasks:
            self.notify("请先输入任务", severity="warning")
            return
        self.cancel_event.clear()
        self.streaming = False
        self.local_action = local[0] if local else None
        self.local_command_approved = False
        if not local:
            self.set_task_result(2, "任务进行中，等待本轮结果")
        self.set_busy(True)
        self.set_state("处理中")
        if task:
            self.append("\n你> " + task + "\n")
            if self.pending_tasks:
                self.pending_tasks.append(task)
                task = None
        self.execute(task, local)

    @work(thread=True, group="agent", exclusive=True, exit_on_error=False)
    def execute(self, task, local):
        # Finishing and submission are serialized on the UI thread. A late
        # request keeps this worker alive instead of racing a second worker.
        with self._lifecycle_lock:
            if self._closed:
                return
            while True:
                status, message = self.execute_turn(task, local)
                try:
                    again = self.ui_call(self.finished, status, message)
                except RuntimeError:
                    return  # The terminal/app may already have closed during teardown.
                if not again:
                    return
                task, local = None, None

    def execute_turn(self, task, local):
        status, message = 0, "完成"
        try:
            if local:
                name, arguments = local
                if name == "sessions":
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
                elif name in {"worktree-create", "worktree-list"}:
                    from .worktrees import worktree_action
                    result = worktree_action(self.config, self.confirm, cancel_event=self.cancel_event, **arguments)
                else:
                    result = self.tools.execute(name, arguments)
                self.emit("[本地查看，不自动上传]\n" + json.dumps(result, ensure_ascii=False, indent=2))
                if "error" in result or result.get("approved") is False or result.get("returncode", 0) != 0:
                    status, message = 2, "本地操作未完成"
            else:
                self.messages, complete = self.agent.run(task, self.messages)
                status = 0 if complete else 2
                message = "验收通过" if complete and self.config.checks else ("本轮结束" if complete else "未完成 / 未验收")
        except KeyboardInterrupt:
            status, message = 130, "已取消；待定操作不会重放"
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
            # Keep the in-memory conversation usable after a failed checkpoint or action.
            try:
                if self.messages:
                    self.messages = validate_history(self.messages)
                    self.agent._steer(self.messages)
            except (OSError, SessionError, RuntimeError) as recovery_error:
                message += "；会话恢复或保存失败: " + self.safe(recovery_error)
        if local and self.local_command_approved:
            try:
                # Keep only the invalidation notice in model history; command
                # output from the local operation is still not auto-uploaded.
                self.agent.record_workspace_change(self.messages,
                                                   {"action": "command", "path": ".", "outcome": "uncertain"})
            except (OSError, SessionError) as error:
                status, message = 1, "本地命令后会话保存失败: " + self.safe(error)
        return status, message

    def on_unmount(self):
        self._unmounted = True
        self.cancel_event.set()
        with self._output_condition:
            self._output_condition.notify_all()

    def finished(self, status, message):
        self.flush_output()
        if self.local_action is not None:
            self.last_operation_status = status
        if self.pending_tasks and status in (0, 2) and not self.cancel_event.is_set():
            self.local_action = None
            self.set_task_result(2, "新要求待处理，等待更新后的结果")
            self.set_state(f"继续处理 {len(self.pending_tasks)} 条新要求")
            return True
        if self.local_action is None:
            self.set_task_result(status, message)
        self.local_action = None
        self.set_busy(False)
        if self.pending_tasks:
            message += f"；{len(self.pending_tasks)} 条要求待处理，点击继续"
        self.set_state(message)
        self.event(message)
        self.append("\n[" + message + "]\n")
        if self.quit_when_idle:
            self.exit(self.last_status)
        return False

    def action_focus_task(self):
        if isinstance(self.screen, ApprovalScreen) and self.screen.allow_steering:
            self.screen.query_one("#approval-task", TextArea).focus()
        elif not isinstance(self.screen, ModalScreen):
            self.query_one("#task", TextArea).focus()

    def action_submit(self):
        if isinstance(self.screen, ApprovalScreen) and self.screen.allow_steering:
            self.screen.query_one("#approval-task", TaskInput).action_send()
        elif not isinstance(self.screen, ModalScreen):
            self.query_one("#task", TaskInput).action_send()

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
        try:
            if len(task) > limit or len(task.encode("utf-8")) > limit:
                raise ValueError(f"单条输入超过 {limit} UTF-8 字节；请缩短或保存到工作区文件后按范围读取。")
        except UnicodeEncodeError:
            self.reject_input("输入包含无效 Unicode。")
            return
        except ValueError as error:
            self.reject_input(str(error))
            return
        if task.startswith("//"):
            task = task[1:]  # A literal task starting with a slash.
        elif task.startswith("/"):
            if event.editor.text == event.draft:
                event.editor.load_text("")
            self.slash_command(task)
            return
        if (self.busy and self._draft_submission is not None
                and self._draft_submission == (event.editor.id, event.draft, task)):
            self.set_state("请求仍在准备；可编辑输入发送新要求")
            return
        if ((self.busy or self.pending_tasks) and (len(self.pending_tasks) >= self.MAX_PENDING_TASKS
                or sum(len(item.encode("utf-8")) for item in self.pending_tasks) + len(task.encode("utf-8")) > limit)):
            self.reject_input(f"待处理引导最多 {self.MAX_PENDING_TASKS} 条、合计 {limit} UTF-8 字节；请等待处理后重发。")
            return
        if self.busy and event.editor.text == event.draft:
            event.editor.load_text("")
        if self.busy:
            self.pending_tasks.append(task)
            self.set_task_result(2, "新要求待处理，等待更新后的结果")
            self.streaming = False
            self.append("\n你> " + task + "\n")
            self.append("[已接收新要求，将在下一个安全节点调整当前任务]\n")
            self.event(f"收到执行中引导，待处理 {len(self.pending_tasks)} 条")
            self.set_state(f"已接收 {len(self.pending_tasks)} 条新要求，等待安全节点")
            if self.approval:
                self.approval.dismiss(False)
        else:
            previous = self._draft_submission
            latest_user = next((message for message in reversed(self.messages or []) if message.get("role") == "user"), {})
            retry = previous is not None and previous[2] == task and latest_user.get("content") == redact(task, self.config.secrets)
            self._draft_submission = (event.editor.id, event.draft, task)
            self.start(None if retry else task)

    def action_cancel(self):
        if not self.busy:
            return
        self.cancel_event.set()
        with self._output_condition:
            self._output_condition.notify_all()
        if self.approval:
            self.approval.dismiss(False)
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
        self.append("".join("\n" + message["role"] + "> " + message["content"] + "\n"
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
                store.save_new(name, self.messages, **self.agent.reference_save_options())
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
            if not approved:
                return
            try:
                history = self.get_store().load(name)
                if self.session_name and name != self.session_name:
                    self.store.release(self.session_name)
                self.messages, self.session_name = history, name
                self.agent.restore_plan(history)
                selected = getattr(self.store, "loaded_references", None)
                self.agent.restore_references(selected if isinstance(selected, dict) else None)
                self._draft_submission = None
                self.refresh_plan()
                self.pending_tasks = []
                self.transcript = ""
                self._transcript_trimmed = False
                self.query_one("#transcript", TextArea).load_text("")
                self.append("已恢复本地会话。输入新任务或点击继续。\n")
                self.show_history()
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
            if not approved or self.busy:
                return
            # Detach autosave before replacing history: never overwrite the old
            # session on the next turn, and release its interprocess lease.
            if self.store and self.session_name:
                self.store.release(self.session_name)
            self.session_name = None
            self.messages = None
            self.agent.restore_plan(None)
            self.agent.restore_references()
            self._draft_submission = None
            self.refresh_plan()
            self.pending_tasks = []
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
        if command == "/help":
            self.append("\n本地命令（不发送给模型）：\n"
                        "/help /keys /plan /status /config /models /context /usage /cancel /exit\n"
                        "/continue /new /add 文件路径 /ref 文件路径 /unref 文件路径 /refs [clear]\n"
                        "/map [名称或路径查询] /git /diff /compact\n"
                        "/changes /undo /redo /discard-history /sessions /prune [--dry-run]\n"
                        "/save [名称] /restore [名称] /delete [名称]\n"
                        "/worktrees /worktree 新目录路径（完整剩余文本作为路径；不会切换当前对话）\n"
                        "执行中可输入普通消息调整引导。// 开头发送以 / 开头的普通消息。\n"
                        "提供商和模型通过 config.toml 或启动参数选择；恢复时仍校验绑定。\n")
            return
        if command == "/keys":
            self.append("\n[快捷键] Enter 发送；Shift+Enter / Ctrl+J 换行；Ctrl+C 取消；Ctrl+Q 退出；Ctrl+L 输入。审批默认拒绝。\n"
                        + json.dumps(dict(self.config.tui_keybindings), ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/plan":
            self.append("\n[任务计划；模型报告，不代表审批或验收]\n"
                        + json.dumps(self.agent.plan_snapshot(), ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/status":
            self.append(f"\n[本地状态] {self.task_message} | 执行中: {self.busy} | 消息: {len(self.messages or [])} | 本次基线: {self.agent.baseline_status} | 编辑后检查: {self.agent.edit_check_status} | 待处理引导: {len(self.pending_tasks)}\n")
            return
        if command == "/refs" and not argument:
            self.append("\n[当前文件引用；准备记录不是已发送证明]\n" + json.dumps(
                redact(self.agent.reference_state(), self.config.secrets), ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/config":
            details = {"provider": self.config.provider_name,
                       "profile": self.config.profile_name,
                       "config_file": str(self.config.config_path) if self.config.config_path else None}
            self.append("\n[生效配置；密钥值不显示]\n" + json.dumps(public_config(self.config, details), ensure_ascii=False, indent=2) + "\n")
            return
        if command == "/models":
            self.append("\n[配置中的模型；未联网]\n" + json.dumps(self.config.model_catalog, ensure_ascii=False, indent=2) + "\n")
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
        elif identity == "cancel":
            self.action_cancel()
        elif not self.busy:
            if identity == "continue":
                self.start()
            elif identity == "new":
                self.new_conversation()
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
                    if approved:
                        self.agent.compact_context = True
                        self.event("已启用请求上下文压缩；保存的完整对话保留")

                self.push_screen(ApprovalScreen(f"按配置阈值压缩旧成功工具输出及已批准完成的旧编辑正文；限额内可保留少量完整源码快照，仍超限时折叠已完成的旧只读调用。用户要求、失败、拒绝和最近 {self.config.context_keep_recent_messages} 条消息保留。完整会话文件保留；下方为强制触发预览。\n"
                                               + json.dumps(preview, ensure_ascii=False, indent=2)), compact)
            elif identity == "delete":
                name = self.query_one("#session-name", Input).value.strip()

                def delete(approved):
                    if approved:
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


def run_tui(config, task=None, session_dir=None, save_session=None, resume=None, change_dir=None, show_metrics=False, files=()):
    app = SprigApp(config, task, session_dir, save_session, resume, change_dir=change_dir, files=files)
    try:
        return app.run() or 0
    finally:
        app.close()
        if show_metrics:
            print("[usage] " + json.dumps(app.agent.usage_report(), ensure_ascii=False), file=sys.stderr, flush=True)
