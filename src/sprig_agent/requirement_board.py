"""Local TUI entry point for named requirements, evidence and retained edits."""

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, OptionList, Static, TextArea

from .requirement_checks import command as verification_command
from .requirements import state

PAGE_SIZE = 16
CHECK_STATUS = {"not_run": "未检查", "passed": "所选检查通过", "failed": "所选检查失败", "blocked": "检查被拒绝",
                "incomplete": "检查未完成", "steered": "检查因新要求停止", "unfinished": "检查结果未知"}
FRESHNESS = {"current": "当前有效", "stale": "已过期", "historical": "历史记录", "unscoped": "未跟踪文件"}
FILE_STATE = {"matches_recorded_state": "与记录一致", "changed_since_recording": "有记录外改动",
              "unavailable": "当前不可读", "uncertain_pending_change": "有待结算改动"}


class RequirementBoard(ModalScreen):
    BINDINGS = [Binding("escape", "close_board", "关闭需求面板", priority=True)]
    AUTO_FOCUS = "#board-requirements"
    CSS = """
    RequirementBoard { align: center middle; background: $background 70%; }
    #board { width: 96%; height: 96%; border: thick $accent; padding: 0 1; }
    #board-raised, #board-status { height: 1; }
    #board-raised Static { width: 1fr; }
    #board-lists { height: 6; }
    #board-lists OptionList { width: 1fr; height: 1fr; }
    #board-details { height: 1fr; min-height: 3; }
    #board-actions, #board-navigation { height: 3; }
    #board-actions Input { width: 1fr; min-width: 14; }
    #board-actions Button, #board-navigation Button { min-width: 8; width: 1fr; }
    #board-merge { width: auto; }
    """

    def __init__(self, host):
        super().__init__()
        self.host = host
        self.page_offset = 0
        self.rows = []
        self.selected = None
        self.files = []
        self.details = None

    def compose(self):
        with Vertical(id="board"):
            with Horizontal(id="board-raised"):
                yield Static("需求与版本", markup=False)
                yield Static("保留的文件备份", markup=False)
            with Horizontal(id="board-lists"):
                yield OptionList(id="board-requirements", markup=False, compact=True)
                yield OptionList(id="board-files", markup=False, compact=True)
            yield TextArea("", id="board-details", read_only=True, soft_wrap=True, show_cursor=False)
            yield Static("", id="board-status", markup=False)
            with Horizontal(id="board-actions"):
                yield Input(placeholder="检查编号", id="board-checks")
                yield Button("验收", id="board-verify")
                yield Button("差异", id="board-diff")
                yield Button("撤回文件", id="board-revert")
                yield Checkbox("三方合并", id="board-merge")
            with Horizontal(id="board-navigation"):
                yield Button("上页", id="board-previous")
                yield Button("下页", id="board-next")
                yield Button("刷新", id="board-refresh")
                yield Button("关闭", id="board-close")

    def on_mount(self):
        self.show_requirements()

    def status(self, message):
        self.query_one("#board-status", Static).update(self.host.safe(message))

    def show_requirements(self):
        self.rows = list(state(self.host.messages).values())
        self.page_offset = min(self.page_offset, (len(self.rows) - 1) // PAGE_SIZE * PAGE_SIZE) if self.rows else 0
        page = self.rows[self.page_offset:self.page_offset + PAGE_SIZE]
        options = self.query_one("#board-requirements", OptionList)
        options.clear_options()
        options.add_options([f"{row['name']} · v{row['revision']}" + (" · 已撤回" if row["text"] is None else "")
                             for row in page])
        self.selected = next((row for row in page if self.selected and row["name"] == self.selected["name"]), None)
        options.highlighted = page.index(self.selected) if self.selected is not None else (0 if page else None)
        self.clear_details()
        self.show_description()
        self.status(f"{len(self.rows)} 项需求 · 方向键选择，Enter 读取" if self.rows else
                    "暂无需求。用 /requirements set 名称 正文建立需求。")
        self.buttons()
        self.set_focus(options)

    def clear_details(self):
        self.files, self.details = [], None
        self.query_one("#board-files", OptionList).clear_options()
        self.query_one("#board-merge", Checkbox).value = False

    def selected_file(self):
        index = self.query_one("#board-files", OptionList).highlighted
        return self.files[index] if index is not None and 0 <= index < len(self.files) else None

    def show_description(self):
        selected = self.selected
        lines = []
        if selected:
            lines += [f"{selected['name']} · v{selected['revision']} · 原始消息 {selected['source_message']}", "",
                      selected["text"] if selected["text"] is not None else "该需求已撤回。"]
        if self.details:
            evidence, changes = self.details
            if error := evidence.get("error"):
                lines += ["", "验收记录读取失败", error]
            else:
                label = CHECK_STATUS.get(evidence["verification"], evidence["verification"])
                freshness = FRESHNESS.get(evidence.get("freshness"))
                if freshness:
                    label += f" · {freshness} · v{evidence['checked_revision']}"
                lines += ["", label]
                for row in evidence["checks"]:
                    check = f"检查 {row['number']}"
                    if row.get("result_message"):
                        check += f" · 命令消息 {row['command_message']} · 结果消息 {row['result_message']}"
                        if "returncode" in row:
                            check += f" · 退出码 {row['returncode']}"
                        elif not row["approved"]:
                            check += " · 未获批准"
                    else:
                        check += " · 未取得结果"
                    lines.append(check)
            if error := changes.get("error"):
                lines += ["", "修改备份读取失败", error]
            elif not self.files:
                lines += ["", "没有可见的保留备份。"]
            if file := self.selected_file():
                lines += ["", f"{file['path']} · v{file['requirement_revision']}", f"变更 ID {file['id']}",
                          FILE_STATE.get(file["current"], file["current"])]
                if file["later_recorded_change"]:
                    lines.append("仍有后续的同文件记录。")
        self.query_one("#board-details", TextArea).load_text(self.host.safe("\n".join(lines)))

    def load_selected(self):
        if self.host.busy or self.selected is None:
            return
        self.clear_details()
        self.show_description()
        self.host.start(local=("requirement-board", {"name": self.selected["name"], "screen": self}))
        self.status("正在读取本地验收记录和文件备份…")
        self.buttons()

    def show_details(self, name, evidence, changes):
        if not self.is_mounted or self.selected is None or name != self.selected["name"]:
            return
        self.files = changes.get("records", [])
        self.details = evidence, changes
        options = self.query_one("#board-files", OptionList)
        options.clear_options()
        options.add_options([f"v{row['requirement_revision']} · {row['path']}" +
                             (" · 已撤回" if row["reverted"] else " · 未应用" if row["applied"] is False else "")
                             for row in self.files])
        options.highlighted = 0 if self.files else None
        self.show_description()
        self.status("已读取本地记录。选择文件后可查看差异。")
        self.buttons()

    def buttons(self):
        busy = self.host.busy
        file = self.selected_file()
        self.query_one("#board-requirements", OptionList).disabled = busy
        self.query_one("#board-files", OptionList).disabled = busy or not self.files
        self.query_one("#board-previous", Button).disabled = busy or self.page_offset == 0
        self.query_one("#board-next", Button).disabled = busy or self.page_offset + PAGE_SIZE >= len(self.rows)
        self.query_one("#board-refresh", Button).disabled = busy
        self.query_one("#board-diff", Button).disabled = busy or file is None
        can_revert = (file is not None and file["applied"] is True and file["reverted"] is False
                      and not self.host.has_pending_requirements() and not self.host.config.read_only
                      and self.host.config.tool_enabled("edit_file"))
        self.query_one("#board-revert", Button).disabled = busy or not can_revert
        self.query_one("#board-merge", Checkbox).disabled = busy or not can_revert
        can_verify = self.selected is not None and self.selected["text"] is not None and bool(self.host.config.checks)
        self.query_one("#board-verify", Button).disabled = busy or not can_verify
        self.query_one("#board-checks", Input).disabled = busy or not can_verify

    def operation_finished(self, action, success, message):
        if not self.is_mounted:
            return
        if action != "requirement-board":
            self.clear_details()
            self.show_description()
            self.status(message + "。刷新后查看。")
        elif not success and self.details is None:
            self.status(message)
        self.buttons()
        if action == "requirement-board":
            self.set_focus(self.query_one("#board-requirements", OptionList))

    @on(OptionList.OptionSelected, "#board-requirements")
    def choose_requirement(self, event):
        event.stop()
        if not self.host.busy and 0 <= self.page_offset + event.option_index < len(self.rows):
            self.selected = self.rows[self.page_offset + event.option_index]
            self.load_selected()

    @on(OptionList.OptionHighlighted, "#board-files")
    def highlight_file(self, event):
        event.stop()
        self.query_one("#board-merge", Checkbox).value = False
        self.show_description()
        self.buttons()

    @on(OptionList.OptionSelected, "#board-files")
    def choose_file(self, event):
        event.stop()
        self.open_diff()

    def open_diff(self):
        if not self.host.busy and (file := self.selected_file()) and self.selected:
            self.host.choose_change_review(file["id"], requirement=self.selected["name"])

    def verify(self):
        if self.host.busy or self.selected is None:
            return
        numbers = self.query_one("#board-checks", Input).value.strip()
        try:
            options = verification_command(self.selected["name"] + " " + numbers)
        except ValueError:
            self.status("输入配置中的检查编号。多个编号用空格分开。")
            return
        self.host.start(local=("requirement-verify", options))
        self.buttons()

    @on(Input.Submitted, "#board-checks")
    def checks_entered(self, event):
        event.stop()
        self.verify()

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        button = event.button.id
        if button == "board-close":
            self.action_close_board()
            return
        if self.host.busy:
            return
        if button in {"board-previous", "board-next"}:
            self.page_offset += -PAGE_SIZE if button == "board-previous" else PAGE_SIZE
            self.show_requirements()
        elif button == "board-refresh":
            self.show_requirements()
            self.load_selected()
        elif button == "board-diff":
            self.open_diff()
        elif button == "board-verify":
            self.verify()
        elif button == "board-revert" and (file := self.selected_file()) and self.selected:
            if self.host.has_pending_requirements():
                self.status("请先处理待发送的要求。")
                return
            options = {"name": self.selected["name"], "revision": file["requirement_revision"],
                       "identity": file["id"], "merge": self.query_one("#board-merge", Checkbox).value}
            self.host.start(local=("requirement-revert", options))
            self.buttons()

    def action_close_board(self):
        if self.host.busy:
            self.host.cancel_event.set()
            self.status("等待当前操作取消…")
        else:
            self.dismiss()
