"""Clarification form, separate from operation approval and the main draft."""

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, Static, TextArea

from .config import terminal_text
from .questions import MAX_ANSWER, answer_result


class QuestionInput(Input):
    """Reject a paste in full when a single-line field cannot preserve it."""

    def _on_paste(self, event):
        event.stop()
        event.prevent_default()
        start, end = sorted(self.selection)
        candidate = self.value[:start] + event.text + self.value[end:]
        if (any(char in event.text for char in "\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029")
                or self.max_length and len(candidate) > self.max_length):
            self.screen.query_one("#question-status", Static).update(
                "粘贴未插入，原输入已保留：此处仅接受有界单行文字；多行或大文本请 Esc 关闭后在主输入发送。")
            return
        super()._on_paste(event)


class QuestionOptions(OptionList):
    BINDINGS = [Binding("enter", "confirm_answer", "确认本题", priority=True),
                Binding("space", "toggle_answer", "选取", priority=True)]

    class Activated(Message):
        def __init__(self, toggle):
            super().__init__()
            self.toggle = toggle

    def action_confirm_answer(self):
        self.post_message(self.Activated(False))

    def action_toggle_answer(self):
        self.post_message(self.Activated(True))


class QuestionScreen(ModalScreen[list[list[str]] | None]):
    BINDINGS = [Binding("escape", "close_question", "关闭，等待新输入", priority=True)]
    CSS = """
    QuestionScreen { align: center middle; background: $background 70%; }
    #question-form { width: 90%; height: 90%; border: thick $accent; padding: 1 2; }
    #question-title { height: 2; text-style: bold; color: $accent; }
    #question-prompt { height: 5; }
    #question-notice { height: auto; max-height: 3; color: $text-muted; }
    #question-options { height: 1fr; min-height: 3; }
    #question-status { height: auto; min-height: 1; color: $warning; }
    #question-buttons { height: 3; }
    #question-buttons Button { width: 1fr; min-width: 8; }
    """

    def __init__(self, questions, budget, secrets, steer, guidance_limit):
        super().__init__()
        self.questions, self.budget, self.secrets, self.steer = questions, budget, secrets, steer
        self.guidance_limit, self.index = guidance_limit, 0
        self.settled = False
        self.selected = [set() for _ in questions]
        self.custom = ["" for _ in questions]
        self.answers = [None for _ in questions]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="question-form"):
            yield Static("", id="question-title", markup=False)
            yield TextArea("", read_only=True, id="question-prompt")
            yield Static("答案会发送给当前模型并进入启用保存的会话；它不能批准操作。请勿填写密钥。\n问题区可滚动；↑↓ 高亮，Space 选取，Enter 确认本题；也可自填。", id="question-notice", markup=False)
            yield QuestionOptions(id="question-options", markup=False, compact=True)
            yield QuestionInput(max_length=MAX_ANSWER, placeholder="单行自填答案（可与多选组合）；Enter 确认本题", id="question-answer")
            yield Static("", id="question-status", markup=False)
            with Horizontal(id="question-buttons"):
                yield Button("上一题", id="question-back")
                yield Button("下一题", id="question-next", variant="primary")
                yield Button("关闭 / Esc", id="question-close")
            yield QuestionInput(max_length=self.guidance_limit, placeholder="另发单行新要求，停止旧问题；Ctrl+L 聚焦此处，Enter 发送", id="question-guidance")

    def on_mount(self):
        self.render_question()

    def render_options(self):
        options = self.query_one("#question-options", QuestionOptions)
        previous = options.highlighted
        options.clear_options()
        question = self.questions[self.index]
        options.add_options([("[x] " if index in self.selected[self.index] else "[ ] ")
                             + terminal_text(option["label"] + " — " + option["description"])
                             for index, option in enumerate(question["options"])])
        options.highlighted = min(previous or 0, len(question["options"]) - 1) if question["options"] else None

    def render_question(self):
        question = self.questions[self.index]
        self.query_one("#question-title", Static).update(f"澄清 {self.index + 1}/{len(self.questions)} · " + terminal_text(question["header"]) + ("（多选）" if question["multiple"] else "（单选或自填）"))
        self.query_one("#question-prompt", TextArea).load_text(terminal_text(question["question"]))
        self.render_options()
        if not question["multiple"] and self.selected[self.index]:
            self.query_one("#question-options", QuestionOptions).highlighted = min(self.selected[self.index])
        self.query_one("#question-answer", Input).value = self.custom[self.index]
        self.query_one("#question-back", Button).disabled = self.index == 0
        self.query_one("#question-next", Button).label = "提交答案" if self.index == len(self.questions) - 1 else "下一题"
        self.query_one("#question-status", Static).update("")
        self.query_one("#question-options" if question["options"] else "#question-answer").focus()

    @on(QuestionOptions.Activated)
    def activated(self, event):
        event.stop()
        if self.settled:
            return
        if not event.toggle:
            self.advance()
            return
        index = self.query_one("#question-options", QuestionOptions).highlighted
        if index is not None:
            values = self.selected[self.index]
            if self.questions[self.index]["multiple"]:
                values.symmetric_difference_update({index})
            else:
                self.selected[self.index] = {index}
            self.render_options()

    @on(Input.Submitted, "#question-answer")
    def submitted(self, event):
        event.stop()
        self.advance()

    @on(Input.Submitted, "#question-guidance")
    def guidance_submitted(self, event):
        event.stop()
        self.send_guidance()

    def send_guidance(self):
        if self.settled:
            return
        text = self.query_one("#question-guidance", Input).value
        if not text.strip():
            self.query_one("#question-status", Static).update("请填写新要求；不会将空输入当作答案或任务。")
            return
        self.steer(text)

    @on(Button.Pressed)
    def button(self, event):
        event.stop()
        if self.settled:
            return
        if event.button.id == "question-close":
            self.action_close_question()
        elif event.button.id == "question-back":
            self.custom[self.index] = self.query_one("#question-answer", Input).value
            self.index -= 1
            self.render_question()
        elif event.button.id == "question-next":
            self.advance()

    def advance(self):
        if self.settled:
            return
        question = self.questions[self.index]
        self.custom[self.index] = custom = self.query_one("#question-answer", Input).value
        selected = self.selected[self.index]
        if custom.strip() and not question["multiple"]:
            values = [custom]
            self.selected[self.index] = set()
        else:
            if not question["multiple"] and question["options"]:
                highlighted = self.query_one("#question-options", QuestionOptions).highlighted
                selected = {highlighted} if highlighted is not None else set()
                self.selected[self.index] = set(selected)
            values = [question["options"][index]["label"] for index in sorted(selected)]
            if custom.strip():
                values.append(custom)
        try:
            answer_result([question], [values], self.budget, self.secrets)
            self.answers[self.index] = values
            if self.index == len(self.questions) - 1:
                self.settle(answer_result(self.questions, self.answers, self.budget, self.secrets)["answers"])
                return
        except (ValueError, UnicodeError):
            self.query_one("#question-status", Static).update("答案未发送：请明确选取或自填，避免重复，并缩短到输出预算内；可返回上一题调整。")
            return
        self.index += 1
        self.render_question()

    def action_close_question(self):
        self.settle(None)

    def settle(self, answers):
        if not self.settled:
            self.settled = True
            self.dismiss(answers)
