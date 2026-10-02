"""Textual terminal UI; the existing agent and approval boundaries stay in charge."""

import json
from pathlib import Path
import threading

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, RichLog, Static, TabbedContent, TabPane, TextArea
from textual.worker import get_current_worker

from .agent import Agent
from .config import redact, terminal_text
from .provider import Provider
from .session import SessionError, SessionStore, validate_history
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
        def __init__(self, editor, text):
            super().__init__()
            self.editor, self.text = editor, text

    def action_send(self):
        text = self.text.strip()
        if text:
            self.post_message(self.Submitted(self, text))
            self.load_text("")

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
    TITLE = "Sprig Agent"
    BINDINGS = [
        Binding("ctrl+enter", "submit", "发送", show=False, priority=True),
        Binding("ctrl+c", "cancel", "取消", priority=True),
        Binding("ctrl+q", "quit", "退出", priority=True),
        Binding("ctrl+l", "focus_task", "任务输入", priority=True),
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
    #side-tabs { height: 1fr; }
    #operations Button { width: 1fr; margin-bottom: 1; }
    #session-name { margin-top: 1; }
    #session-buttons { height: 4; }
    #session-buttons Button { min-width: 8; width: 1fr; }
    #events { height: 1fr; }
    """

    def __init__(self, config, task=None, session_dir=None, save_session=None, resume=None, provider=None):
        super().__init__()
        self.config = config
        self.initial_task = task
        self.session_dir = Path(session_dir or Path.home() / ".local/state/sprig-agent/sessions")
        self.session_name = save_session or resume
        self.store = None
        self.messages = None
        self.busy = False
        self.last_status = 0
        self.cancel_event = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._closed = False
        self._unmounted = False
        self._ui_thread_id = threading.get_ident()
        self.quit_when_idle = False
        self.transcript = ""
        self.streaming = False
        self.approval = None
        # UI-thread-owned queue. Only the single agent worker takes its contents.
        self.pending_tasks = []
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
                    result = {**result, "skipped": True,
                              "error": "Not executed: superseded by new user guidance. Follow the updated request and request fresh approval for any new action."}
                summary = {key: result[key] for key in ("approved", "changed", "returncode", "timed_out", "output_limited", "error") if key in result}
                app.ui_call(app.event, name + ": " + json.dumps(summary, ensure_ascii=False))
                return result

        try:
            self.tools = UiTools(config, self.confirm, self.cancel_event, self.emit_command)
        except BaseException:
            if self.store:
                self.store.close()
            raise
        self.agent = Agent(config, provider or Provider(config), self.tools, self.emit,
                           checkpoint=self.checkpoint, emit_delta=self.emit_delta, cancel_event=self.cancel_event,
                           receive_input=self.receive_input)

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
                with TabbedContent(id="side-tabs"):
                    with TabPane("操作", id="actions-tab"):
                        with VerticalScroll(id="operations"):
                            yield Button("继续当前对话", id="continue")
                            yield Button("查看 Git diff", id="diff")
                            yield Button("查看 Git 状态", id="git")
                            yield Button("查看项目地图", id="map")
                            yield Static("本地会话（不加密）", markup=False)
                            yield Input(value=self.session_name or "", placeholder="会话名称", id="session-name")
                            with Horizontal(id="session-buttons"):
                                yield Button("保存", id="save")
                                yield Button("恢复", id="restore")
                            yield Button("列出本地会话", id="sessions")
                            yield Button("删除指定会话", id="delete")
                    with TabPane("事件", id="events-tab"):
                        yield RichLog(id="events", wrap=True, markup=False, highlight=False, max_lines=1000)
        yield Footer()

    def on_mount(self):
        self._ui_thread_id = threading.get_ident()
        self.action_focus_task()
        self.append("Sprig 就绪。任务、读取内容和工具结果会发送到所示服务。\n写入和命令逐次确认；命令没有沙箱。\n")
        if self.messages:
            self.show_history()
        if self.initial_task:
            self.start(self.initial_task)

    def safe(self, text):
        return terminal_text(redact(str(text), self.config.api_key))

    def append(self, text):
        text = self.safe(text)
        combined = self.transcript + text
        self.transcript = combined[-64000:]
        view = self.query_one("#transcript", TextArea)
        if len(combined) <= 64000:
            view.insert(text, location=view.document.end)
        else:
            view.load_text(self.transcript)
        view.scroll_end(animate=False)

    def event(self, text):
        self.query_one("#events", RichLog).write(self.safe(text))

    def set_state(self, text):
        self.query_one("#state", Static).update(self.safe(text))

    def set_busy(self, busy):
        self.busy = busy
        for identity in ("continue", "diff", "git", "map", "save", "restore", "sessions", "delete", "session-name"):
            self.query_one("#" + identity).disabled = busy
        self.query_one("#cancel", Button).disabled = not busy
        if not busy:
            self.action_focus_task()

    def checkpoint(self, history):
        self.messages = history
        if self.store and self.session_name:
            self.store.save(self.session_name, history)

    def take_pending(self):
        tasks, self.pending_tasks = self.pending_tasks, []
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
            self.ui_call(self.append, "\nSprig> ")
            self.streaming = True
        self.ui_call(self.append, text)

    def emit_command(self, text):
        self.check_cancelled()
        self.streaming = False
        self.ui_call(self.append, text)

    def confirm(self, description):
        self.check_cancelled()
        answer, done = [False], threading.Event()

        def show():
            # Guidance could arrive between the agent boundary and this approval.
            if self.pending_tasks:
                done.set()
                return

            def decide(approved):
                answer[0] = approved
                self.approval = None
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
                result = {"sessions": self.get_store().list()} if name == "sessions" else self.tools.execute(name, arguments)
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
                        self.store.save(self.session_name, self.messages)
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
        return status, message

    def on_unmount(self):
        self._unmounted = True
        self.cancel_event.set()

    def finished(self, status, message):
        if self.pending_tasks and status in (0, 2) and not self.cancel_event.is_set():
            self.set_state(f"继续处理 {len(self.pending_tasks)} 条新要求")
            return True
        self.last_status = status
        self.set_busy(False)
        if self.pending_tasks:
            message += f"；{len(self.pending_tasks)} 条要求待处理，点击继续"
        self.set_state(message)
        self.event(message)
        self.append("\n[" + message + "]\n")
        if self.quit_when_idle:
            self.exit(status)
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
        task = event.text
        if isinstance(self.screen, ModalScreen) and not (
                isinstance(self.screen, ApprovalScreen) and self.screen.allow_steering):
            return
        if self.busy:
            self.pending_tasks.append(task)
            self.streaming = False
            self.append("\n你> " + task + "\n")
            self.append("[已接收新要求，将在下一个安全节点调整当前任务]\n")
            self.event(f"收到执行中引导，待处理 {len(self.pending_tasks)} 条")
            self.set_state(f"已接收 {len(self.pending_tasks)} 条新要求，等待安全节点")
            if self.approval:
                self.approval.dismiss(False)
        else:
            self.start(task)

    def action_cancel(self):
        if not self.busy:
            return
        self.cancel_event.set()
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
        self.event("已恢复历史；未发起模型请求，未重放工具")

    def save(self):
        try:
            if not self.messages:
                raise ValueError("还没有可保存的对话")
            name = self.query_one("#session-name", Input).value.strip()
            store = self.get_store()
            if name != self.session_name:
                store.ensure_new(name)
            store.save(name, self.messages)
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
                self.pending_tasks = []
                self.transcript = ""
                self.query_one("#transcript", TextArea).load_text("")
                self.append("已恢复本地会话。输入新任务或点击继续。\n")
                self.show_history()
                self.set_state("已恢复，等待任务")
            except (OSError, ValueError, SessionError) as error:
                self.notify(self.safe(error), severity="error")

        if self.messages or self.pending_tasks:
            self.push_screen(ApprovalScreen("恢复会话 " + self.safe(name) + " 将替换当前内存对话和待处理要求。\n未保存的对话将丢失；已保存的会话文件不变。"), load)
        else:
            load()

    @on(Button.Pressed)
    def buttons(self, event):
        identity = event.button.id
        if identity == "send":
            self.action_submit()
        elif identity == "cancel":
            self.action_cancel()
        elif not self.busy:
            if identity == "continue":
                self.start()
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
        if not self._lifecycle_lock.acquire(timeout=min(60, self.config.timeout + 5)):
            raise TimeoutError("worker cleanup is still pending; filesystem handles remain open")
        try:
            self._closed = True
            self.tools.close()
            if self.store:
                self.store.close()
        finally:
            self._lifecycle_lock.release()


def run_tui(config, task=None, session_dir=None, save_session=None, resume=None):
    app = SprigApp(config, task, session_dir, save_session, resume)
    try:
        return app.run() or 0
    finally:
        app.close()
