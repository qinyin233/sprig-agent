"""Independent history editor; Enter sends only after fresh branch confirmation."""
import json
import unicodedata

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static, TextArea

from .composer import MAX_PASTE_BYTES, paste_text


def editor_text(body, limit):
    if not isinstance(body, str) or len(body) > limit:
        raise ValueError("历史正文超过本编辑区上限；原会话保留，请用 /history 分页查看后另写新请求")
    escaped = any(char != "\n" and unicodedata.category(char).startswith("C") for char in body)
    if not escaped:
        if len(body.encode("utf-8")) > limit:
            raise ValueError("历史正文超过编辑区 UTF-8 字节上限；原会话保留")
        return body, False
    chunks, size = ['"'], 2
    for index in range(0, len(body), 4096):
        encoded = json.dumps(body[index:index + 4096], ensure_ascii=False)[1:-1]
        chunk = "".join(json.dumps(char, ensure_ascii=True)[1:-1] if unicodedata.category(char).startswith("C") else char
                        for char in encoded)
        size += len(chunk.encode("utf-8"))
        if size > limit:
            raise ValueError("历史正文的安全 JSON 视图超过编辑区上限；原文未截断")
        chunks.append(chunk)
    return "".join([*chunks, '"']), True


class RevisionInput(TextArea):
    BINDINGS = [Binding("enter", "send_revision", "确认并重发", priority=True),
                Binding("shift+enter,ctrl+j", "newline", "换行", priority=True),
                Binding("f4", "expand_revision", "展开全文", priority=True)]

    def __init__(self, text, **options):
        self.collapsed, self._full = False, None
        super().__init__("", **options)
        self.load_text(text)

    def load_text(self, text):
        self.collapsed = len(text) > 32768 or text.count("\n") > 2048
        self._full = text if self.collapsed else None
        visible = (f"[编辑区的完整内容已收存：{len(text.encode('utf-8'))} 字节；Enter 使用全文，F4 展开编辑，或选择替换全文]"
                   if self.collapsed else text)
        super().load_text(visible)
        self.read_only = self.collapsed

    def value_text(self):
        return self._full if self.collapsed else self.text

    def action_expand_revision(self):
        self.screen.expand_editor()

    def expand(self):
        if self.collapsed:
            body, self._full = self._full, None
            self.collapsed = False
            super().load_text(body)
            self.read_only = False

    def undo(self):
        if not self.collapsed:
            return super().undo()

    def redo(self):
        if not self.collapsed:
            return super().redo()

    def action_send_revision(self):
        self.screen.submit()

    def action_newline(self):
        if not self.read_only:
            self.replace("\n", *self.selection, maintain_selection_offset=False)

    async def _on_paste(self, event):
        event.stop()
        event.prevent_default()
        if self.read_only:
            return
        try:
            body = paste_text(event.text)
            size = len(self.text.encode("utf-8")) - len(self.selected_text.encode("utf-8")) + len(body.encode("utf-8"))
            if size > self.screen.display_limit:
                raise ValueError("编辑区超过显示字节上限；粘贴未插入")
        except ValueError as error:
            self.screen.status(str(error))
            return
        if len(body) > 32768 or body.count("\n") > 2048:
            if self.text:
                self.screen.status("大段粘贴请先用“替换全文”清空本区，或作为工作区文件使用；原草稿未修改。")
                return
            self.load_text(body)
            self.focus()
            self.screen.status("大段内容已完整收存；Enter 使用全文，F4 展开。主草稿与原历史保留。")
            return
        if result := self._replace_via_keyboard(body, *self.selection):
            self.move_cursor(result.end_location)
            self.focus()


class RevisionScreen(ModalScreen):
    BINDINGS = [Binding("escape", "close_editor", "关闭", priority=True)]
    AUTO_FOCUS = "#revision-editor"
    CSS = """
    RevisionScreen { align: center middle; background: $background 70%; }
    #revision-form { width: 94%; height: 94%; border: thick $accent; padding: 1; }
    #revision-notice, #revision-status { height: auto; max-height: 3; }
    #revision-editor { height: 1fr; min-height: 4; }
    #revision-buttons { height: 3; }
    #revision-buttons Button { width: 1fr; }
    """

    def __init__(self, host, source, text=None):
        super().__init__()
        self.host, self.source = host, source
        self.body = source.body if text is None else text
        self.display_limit = min(MAX_PASTE_BYTES, host.config.max_context_bytes * 6 + 2)
        self.initial, self.json_edit = editor_text(self.body, self.display_limit)
        self.submitting = False

    def compose(self):
        with Vertical(id="revision-form"):
            yield Static("编辑旧请求：Enter 后确认新分支并发送；Shift+Enter 换行。原文件与主草稿保留，工作区不回滚。", id="revision-notice", markup=False)
            yield RevisionInput(self.initial, id="revision-editor", soft_wrap=True)
            yield Static("原文含控制字符，当前编辑一个 JSON 字符串。" if self.json_edit else "只发送本区文字；未保存的旧本轮附件不重建，主草稿文件不附加。", id="revision-status", markup=False)
            with Horizontal(id="revision-buttons"):
                yield Button("重发 / Enter", id="revision-send", variant="primary")
                yield Button("展开 / F4", id="revision-expand")
                yield Button("替换全文", id="revision-replace")
                yield Button("关闭（放弃草稿）", id="revision-close")

    def status(self, text):
        self.query_one("#revision-status", Static).update(self.host.safe(text))

    def submit(self):
        if self.submitting or self.host.busy:
            return
        try:
            if self.host.pending_tasks or self.host.recovered_inputs["pending"] or self.host._input_retry or self.host._input_inbox.blocked:
                raise ValueError("请先处理待发送要求；编辑草稿保留，尚未请求模型")
            self.host.cancel_event.clear()
            self.source.fresh()
            text = self.query_one("#revision-editor", RevisionInput).value_text()
            if len(text) > self.display_limit or len(text.encode("utf-8")) > self.display_limit:
                raise ValueError("编辑区超过显示字节上限；草稿保留")
            body = json.loads(text) if self.json_edit else text
            limit = self.host.config.max_context_bytes
            if not isinstance(body, str) or not body.strip() or len(body) > limit or len(body.encode("utf-8")) > limit:
                raise ValueError("新请求为空、类型错误或超过 UTF-8 字节上限；草稿保留")
            self.submitting = True
            self.query_one("#revision-editor", TextArea).read_only = True
            self.query_one("#revision-send", Button).disabled = True
            self.host.start(local=("conversation-revise", {"source": self.source, "text": body, "editor": self}))
        except (ValueError, OSError, RecursionError) as error:
            self.status(str(error))

    def operation_finished(self, success):
        if not self.is_mounted:
            return
        if success:
            self.dismiss()
            return
        self.submitting = False
        editor = self.query_one("#revision-editor", RevisionInput)
        editor.read_only = editor.collapsed
        editor.focus()
        self.query_one("#revision-send", Button).disabled = False
        self.status("分支未确认完成；编辑草稿保留，请核对当前会话/本地分支文件再操作。")

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == "revision-send":
            self.submit()
        elif event.button.id == "revision-expand":
            self.expand_editor()
        elif event.button.id == "revision-replace":
            if not self.submitting and not self.host.busy:
                self.json_edit = False
                self.query_one("#revision-editor", RevisionInput).load_text("")
                self.query_one("#revision-editor", RevisionInput).focus()
                self.status("替换整条请求：当前为普通文本；原历史和主草稿保持。")
        else:
            self.action_close_editor()

    def expand_editor(self):
        if not self.submitting and not self.host.busy:
            self.query_one("#revision-editor", RevisionInput).expand()
            self.query_one("#revision-editor", RevisionInput).focus()
            self.status("全文已展开，Enter 后预检并确认；大段内容的显示可能较慢。")

    def action_close_editor(self):
        if self.submitting or self.host.busy:
            self.host.cancel_event.set()
            self.status("正在取消；请等待分支操作结束，草稿保留。")
            return
        self.dismiss()
