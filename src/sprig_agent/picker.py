"""Local TUI selectors. Selection never submits a model task or approves a command."""

import threading

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static

from .completion import COMMANDS, file_candidates
from .references import MAX_FILES, reference_paths


class PickerScreen(ModalScreen[tuple[str, str | tuple[str, ...]] | None]):
    BINDINGS = [Binding("escape", "dismiss_picker", "关闭", priority=True),
                Binding("up", "previous", "上一个", priority=True),
                Binding("down", "next", "下一个", priority=True),
                Binding("ctrl+space", "toggle_selection", "勾选文件", priority=True),
                Binding("ctrl+r", "clear_selection", "清空勾选", priority=True)]
    AUTO_FOCUS = "#picker-query"
    CSS = """
    PickerScreen { align: center middle; background: $background 70%; }
    #picker { width: 90%; height: 80%; border: thick $accent; padding: 1 2; }
    #picker-title { height: 2; }
    #picker-options { height: 1fr; }
    #picker-selection { height: auto; max-height: 3; overflow-y: auto; }
    #picker-status { height: auto; min-height: 2; }
    #picker-buttons { height: 3; }
    #picker-buttons Button { width: 1fr; }
    """

    class Found(Message):
        def __init__(self, generation, result):
            super().__init__()
            self.generation, self.result = generation, result

    def __init__(self, *, commands=False, query="", complete_only=False, choices=None, title=None):
        super().__init__()
        self.commands, self.initial_query, self.complete_only = commands, query, complete_only
        self.local_choices, self.picker_title = choices, title
        self.values = []
        self.chosen = {}
        self.generation = 0
        self.cancel_scan = threading.Event()
        self.query_timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Static(self.picker_title or ("补全本地命令；选择后再按 Enter 执行" if self.commands else
                         "选择文件（可多选）；候选不读取正文，不请求模型" if self.multiple else
                         "选择文件补全输入；不会发送任务"), id="picker-title", markup=False)
            yield Input(value=self.initial_query[:256], max_length=256,
                        placeholder="↑↓浏览，Ctrl+Space 勾选，Enter 添加，Esc 关闭" if self.multiple else
                                    "输入关键词；↑↓选择，Enter 选取，Esc 关闭", id="picker-query")
            yield OptionList(id="picker-options", markup=False, compact=True)
            if self.multiple:
                yield Static("未勾选；Enter 添加当前文件，Ctrl+Space 勾选，Ctrl+R 清空", id="picker-selection", markup=False)
            yield Static("", id="picker-status", markup=False)
            with Horizontal(id="picker-buttons"):
                if not self.commands and not self.complete_only:
                    yield Button("勾选/取消", id="picker-toggle")
                    yield Button("添加实时引用", id="picker-ref", variant="primary")
                    yield Button("附加快照", id="picker-add")
                yield Button("关闭 / Esc", id="picker-close")

    @property
    def multiple(self):
        return not self.commands and not self.complete_only

    def render_choices(self):
        options = self.query_one("#picker-options", OptionList)
        index = options.highlighted
        labels = [("[x] " if value in self.chosen else "[ ] ") + value for value in self.values] if self.multiple else self.values
        options.clear_options().add_options(labels)
        options.highlighted = min(index or 0, len(self.values) - 1) if self.values else None
        if self.multiple:
            text = (f"已勾选 {len(self.chosen)}/{MAX_FILES}；Enter 只添加这些文件；Ctrl+R 清空\n"
                    + "\n".join(self.chosen)) if self.chosen else "未勾选；Enter 添加当前文件，Ctrl+Space 勾选，Ctrl+R 清空"
            self.query_one("#picker-selection", Static).update(text)

    def action_toggle_selection(self):
        if not self.multiple:
            return
        index = self.query_one("#picker-options", OptionList).highlighted
        if index is None or not 0 <= index < len(self.values):
            return
        path = self.values[index]
        if path in self.chosen:
            del self.chosen[path]
        else:
            try:
                reference_paths([*self.chosen, path])
            except ValueError as error:
                self.app.notify(str(error), severity="warning")
                return
            self.chosen[path] = None
        self.render_choices()
        self.query_one("#picker-query", Input).focus()

    def action_clear_selection(self):
        if self.multiple:
            self.chosen.clear()
            self.render_choices()
            self.query_one("#picker-query", Input).focus()

    def on_mount(self):
        self.query_one("#picker-options", OptionList).can_focus = False
        self.refresh_candidates()

    @on(Input.Changed, "#picker-query")
    def changed(self, event):
        if self.commands:
            self.refresh_candidates()
        else:
            # Invalidate immediately; debounce filesystem work, never selection validity.
            self.cancel_scan.set()
            self.generation += 1
            self.values = []
            self.query_one("#picker-options", OptionList).clear_options()
            if self.query_timer is not None:
                self.query_timer.stop()
            self.query_timer = self.set_timer(0.15, self.refresh_candidates)

    def refresh_candidates(self):
        self.cancel_scan.set()
        self.cancel_scan = threading.Event()
        self.generation += 1
        query = self.query_one("#picker-query", Input).value
        self.values = []
        self.query_one("#picker-options", OptionList).clear_options()
        if self.commands:
            query = query.removeprefix("/").strip().casefold()
            choices = [("/" + name, "/" + name + "  " + text) for name, text in COMMANDS.items() if query in name] if self.local_choices is None else [
                (key, label) for key, label in self.local_choices if query in label.casefold()]
            limited = len(choices) > 20
            choices = choices[:20]
            self.values = [key for key, _ in choices]
            self.query_one("#picker-options", OptionList).add_options(
                [label for _, label in choices])
            self.query_one("#picker-status", Static).update(("选择后进入明确确认；不请求模型" + ("；最多显示 20 条，请细化查询" if limited else "")) if self.local_choices is not None else
                                                          "只补全输入；选择不会执行命令")
            self.query_one("#picker-options", OptionList).highlighted = 0 if self.values else None
        else:
            self.query_one("#picker-status", Static).update("正在扫描文件名；正文会在明确选择后按隐私和预算规则检查")
            self.scan(self.app, query, self.generation, self.cancel_scan)

    @work(thread=True, group="file-picker", exclusive=True, exit_on_error=False)
    def scan(self, app, query, generation, cancelled):
        # Share the existing lifetime lock: close() cannot release the pinned
        # workspace handle during discovery. Never wait for the UI under it.
        with app._lifecycle_lock:
            if app._closed or cancelled.is_set():
                return
            try:
                result = file_candidates(app.config, app.tools.root, query, cancelled)
            except (OSError, ValueError):
                result = {"files": [], "error": "无法安全列出文件；检查工作区和隐私规则"}
        self.post_message(self.Found(generation, result))

    @on(Found)
    def found(self, event):
        if event.generation != self.generation or not self.is_mounted or self.cancel_scan.is_set():
            return
        self.values = event.result["files"]
        self.render_choices()
        text = event.result.get("error") or (
            f"{len(self.values)} 个候选；已检查 {event.result['entries_scanned']} 个条目"
            + ("；扫描或显示已达上限，请细化查询" if event.result["limited"] else ""))
        self.query_one("#picker-status", Static).update(text)

    def action_previous(self):
        self.query_one("#picker-options", OptionList).action_cursor_up()

    def action_next(self):
        self.query_one("#picker-options", OptionList).action_cursor_down()

    def select(self, mode=None):
        if self.multiple and self.chosen:
            self.cancel_scan.set()
            self.dismiss((mode or "references", tuple(self.chosen)))
            return
        index = self.query_one("#picker-options", OptionList).highlighted
        if index is not None and 0 <= index < len(self.values):
            self.cancel_scan.set()
            self.dismiss((mode or ("command" if self.commands else "complete" if self.complete_only else "references"),
                          self.values[index]))

    @on(Input.Submitted, "#picker-query")
    def submitted(self, event):
        event.stop()
        self.select()

    @on(OptionList.OptionSelected, "#picker-options")
    def selected(self, event):
        event.stop()
        if self.multiple:
            self.action_toggle_selection()
        else:
            self.select()

    @on(Button.Pressed)
    def clicked(self, event):
        event.stop()
        if event.button.id == "picker-close":
            self.action_dismiss_picker()
        elif event.button.id == "picker-toggle":
            self.action_toggle_selection()
        else:
            self.select("attach" if event.button.id == "picker-add" else "references")

    def action_dismiss_picker(self):
        self.cancel_scan.set()
        self.dismiss(None)

    def on_unmount(self):
        self.cancel_scan.set()
        if self.query_timer is not None:
            self.query_timer.stop()
