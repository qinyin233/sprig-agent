"""Local pending-input review. Never starts or replaces the agent worker."""
import json

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static, TextArea

from .config import terminal_text
from .input_inbox import MAX_INPUT_BYTES, UserInput, input_revision


class PendingEditor(TextArea):
    async def _on_paste(self, event):
        event.stop()
        event.prevent_default()
        if self.read_only:
            return
        limit = MAX_INPUT_BYTES * 6 + 2 if self.screen.json_edit else MAX_INPUT_BYTES
        try:
            # TextArea's selected_text is exactly the range replaced by a paste.
            size = len(self.text.encode("utf-8")) - len(self.selected_text.encode("utf-8")) + len(event.text.encode("utf-8"))
            if size > limit:
                raise ValueError
        except (ValueError, UnicodeError):
            self.screen.status("粘贴超过编辑上限或包含无效 Unicode；原草稿保留。")
            return
        # Textual dispatches inherited handlers itself. Calling the base handler
        # here as well would insert the same paste more than once.
        if result := self._replace_via_keyboard(event.text, *self.selection):
            self.move_cursor(result.end_location)
            self.focus()


class PendingScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close_manager", "关闭", priority=True),
                Binding("ctrl+s", "save_input", "保存编辑", priority=True)]
    CSS = """
    PendingScreen { align: center middle; background: $background 70%; }
    #pending-form { width: 94%; height: 94%; border: thick $accent; padding: 1; }
    #pending-notice, #pending-status { height: auto; max-height: 2; }
    #pending-list { height: 25%; min-height: 3; max-height: 7; }
    #pending-editor { height: 1fr; min-height: 4; }
    #pending-buttons, #pending-edit-buttons { height: 3; }
    #pending-buttons Button, #pending-edit-buttons Button { width: 1fr; min-width: 6; }
    """

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.state = host.pending_state()
        self.selected = None
        self.editing = False
        self.json_edit = False
        self.original_editor = ""
        self._signature = None
        self._stale = False

    def compose(self):
        with Vertical(id="pending-form"):
            yield Static("待处理要求 · 本地管理，不请求模型；递送中的要求不能修改。刷新前内容是快照。", id="pending-notice", markup=False)
            yield OptionList(id="pending-list", markup=False, compact=True)
            yield PendingEditor(read_only=True, id="pending-editor", soft_wrap=True)
            yield Static("", id="pending-status", markup=False)
            with Horizontal(id="pending-buttons"):
                for label, name in (("刷新", "refresh"), ("编辑", "edit"), ("模式", "delivery"), ("移除", "drop"), ("上移", "up"), ("下移", "down"), ("关闭", "close")):
                    yield Button(label, id="pending-" + name)
            with Horizontal(id="pending-edit-buttons"):
                yield Button("保存编辑 / Ctrl+S", id="pending-save", variant="primary")
                yield Button("取消编辑", id="pending-cancel-edit")

    def on_mount(self):
        self.refresh_inputs()
        self.set_interval(.25, self.refresh_status)

    def status(self, text):
        self.query_one("#pending-status", Static).update(self.host.safe(text))

    def selected_row(self):
        return next((row for row in self.state["pending"] if row["id"] == self.selected), None)

    def refresh_inputs(self, identity=None):
        if self.editing:
            self.status("编辑草稿仍保留；请先保存或取消编辑，再刷新。")
            return
        self.state = self.host.pending_state()
        rows = self.state["pending"]
        self._signature = None
        selected = identity or self.selected
        self.selected = next((row["id"] for row in rows if row["id"] == selected), rows[0]["id"] if rows else None)
        options = self.query_one("#pending-list", OptionList)
        options.clear_options()
        options.add_options([f"{index}. {'排队' if row.get('delivery') == 'queue' else '引导'} · {row['id']} · " + terminal_text(row["text"][:80]).replace("\n", " ")
                             for index, row in enumerate(rows, 1)])
        options.highlighted = next((index for index, row in enumerate(rows) if row["id"] == self.selected), None)
        self.show_selected()
        self.status(f"{len(rows)} 条待处理要求；正文可滚动查看。编辑 Enter 换行，Ctrl+S 保存时确认；Esc 关闭。")
        self.refresh_status()

    def show_selected(self):
        row = self.selected_row()
        editor = self.query_one("#pending-editor", TextArea)
        editor.read_only = True
        editor.load_text(terminal_text(row["text"]) if row else "暂无待处理要求。")

    def refresh_status(self):
        source = self.host._input_inbox
        signature = (id(source), source.version(), id(self.host.recovered_inputs), id(self.host._input_retry))
        if signature != self._signature:
            self._stale = input_revision(self.state) != input_revision(self.host.pending_state())
            self._signature = signature
        stale = self._stale
        locked = bool(self.host._input_inbox.claimed()) or self.host._input_inbox.blocked or bool(self.host._input_retry)
        for name in ("edit", "delivery", "drop", "up", "down"):
            self.query_one("#pending-" + name, Button).disabled = self.editing or not self.selected or stale or locked
        self.query_one("#pending-save", Button).disabled = not self.editing or stale or locked
        self.query_one("#pending-cancel-edit", Button).disabled = not self.editing
        if self.host._input_inbox.blocked:
            self.status("队列修改保存未确认；内存快照可能不同于磁盘，请关闭并恢复此会话或明确丢弃全部待处理输入。")
        elif stale:
            self.status("队列已变化；当前显示为旧快照。草稿保留，请取消编辑后刷新。")
        elif locked:
            self.status("要求正在递送或有未确认的保存；可查看，暂不能修改。")

    @on(OptionList.OptionHighlighted, "#pending-list")
    def select_input(self, event):
        event.stop()
        rows = self.state["pending"]
        if event.option_index >= len(rows):
            return
        identity = rows[event.option_index]["id"]
        if self.editing and identity != self.selected:
            self.status("编辑草稿仍保留；请先保存或取消编辑后再选择。")
            self.query_one("#pending-list", OptionList).highlighted = next(
                index for index, row in enumerate(rows) if row["id"] == self.selected)
            return
        if identity != self.selected:
            self.selected = identity
            self.show_selected()

    def begin_edit(self):
        row = self.selected_row()
        if not row or self.editing:
            return
        self.json_edit = terminal_text(row["text"]) != row["text"] or "\r" in row["text"]
        self.original_editor = json.dumps(row["text"], ensure_ascii=True) if self.json_edit else row["text"]
        self.editing = True
        editor = self.query_one("#pending-editor", TextArea)
        editor.read_only = False
        editor.load_text(self.original_editor)
        editor.focus()
        self.status("含控制字符或 CR 的正文以 JSON 字符串编辑，保存时还原转义。" if self.json_edit else "编辑仅改变待处理要求；Enter 换行，Ctrl+S 后需确认，不发送任务。")
        self.refresh_status()

    def changed(self, result):
        if result.get("approved") is True:
            self.editing = False
            self.refresh_inputs(result.get("replacement_id"))
            self.status("已保存本地队列修改；没有发起模型请求。")
        else:
            self.status(result.get("error", "修改未批准；原要求与编辑草稿保留。"))

    def action_save_input(self):
        if not self.editing:
            return
        draft = self.query_one("#pending-editor", TextArea).text
        try:
            if len(draft) > MAX_INPUT_BYTES * 6 + 2:
                raise ValueError("编辑草稿超过上限；原要求保留")
            text = json.loads(draft) if self.json_edit else draft
            UserInput(self.selected, text)
            if text == self.selected_row()["text"]:
                self.editing = False
                self.show_selected()
                self.refresh_status()
                self.status("正文未变；没有修改队列或发送请求。")
                return
            self.host.change_pending({"action": "edit", "identity": self.selected, "text": text},
                                     expected=self.state, completed=self.changed)
        except (ValueError, TypeError, RecursionError) as error:
            self.status("编辑未保存：" + str(error))

    @on(Button.Pressed)
    def buttons(self, event):
        event.stop()
        action = event.button.id.removeprefix("pending-")
        if action == "close":
            self.action_close_manager()
        elif action == "refresh":
            self.refresh_inputs()
        elif action == "edit":
            self.begin_edit()
        elif action == "save":
            self.action_save_input()
        elif action == "cancel-edit":
            self.editing = False
            self.show_selected()
            self.refresh_status()
            self.status("编辑草稿已取消；原要求保留。")
        elif action == "delivery" and self.selected:
            self.host.change_pending({"action": "delivery", "identity": self.selected,
                "delivery": "steer" if self.selected_row().get("delivery") == "queue" else "queue"},
                expected=self.state, completed=self.changed)
        elif action in {"drop", "up", "down"} and self.selected:
            request = {"action": "drop" if action == "drop" else "move", "identity": self.selected}
            if action != "drop":
                index = next(index for index, row in enumerate(self.state["pending"], 1) if row["id"] == self.selected)
                request["position"] = max(1, min(len(self.state["pending"]), index + (-1 if action == "up" else 1)))
            self.host.change_pending(request, expected=self.state, completed=self.changed)

    def action_close_manager(self):
        if self.editing and self.query_one("#pending-editor", TextArea).text != self.original_editor:
            from .tui import ApprovalScreen
            self.host.push_screen(ApprovalScreen("放弃本窗口尚未保存的编辑草稿并关闭？已保存要求不变。"),
                                  lambda approved: self.dismiss() if approved is True else None)
        else:
            self.dismiss()
