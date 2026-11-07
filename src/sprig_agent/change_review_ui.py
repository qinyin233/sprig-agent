"""Read-only file-change viewer; selected record pages never submit a model task."""
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, OptionList, Static, TextArea


class ChangeReviewScreen(ModalScreen):
    BINDINGS = [Binding('escape', 'close_review', '关闭', priority=True),
                Binding('alt+left', 'previous_page', '上一页'), Binding('alt+right', 'next_page', '下一页')]
    AUTO_FOCUS = '#review-text'
    CSS = '''
    ChangeReviewScreen { align: center middle; background: $background 70%; }
    #change-review { width: 96%; height: 94%; border: thick $accent; padding: 1; }
    #review-notice, #review-status { height: auto; max-height: 2; }
    #review-records { height: 4; }
    #review-text { height: 1fr; min-height: 4; }
    #review-buttons { height: 3; }
    #review-buttons Button { width: 1fr; }
    '''

    def __init__(self, host, payload):
        super().__init__()
        self.host, self.initial = host, payload
        self.rows, self.view, self.selected = [], None, None
        self.back, self.navigation = [], None
        self._payload_applied = False

    def compose(self):
        with Vertical(id='change-review'):
            yield Static('文件工具保留的历史差异；不含全部命令/Git 改动，不代表验收。选择记录后 Enter；主草稿保持。',
                         id='review-notice', markup=False)
            yield OptionList(id='review-records', markup=False, compact=True)
            yield Static('', id='review-status', markup=False)
            yield TextArea('', id='review-text', read_only=True, soft_wrap=False)
            with Horizontal(id='review-buttons'):
                yield Button('上一页', id='review-previous')
                yield Button('下一页', id='review-next')
                yield Button('重新读取', id='review-refresh')
                yield Button('关闭', id='review-close')

    def on_mount(self):
        self.show_payload(self.initial)

    def show_payload(self, payload):
        self._payload_applied = True
        catalog, page = payload['catalog'], payload['page']
        self.rows = catalog.get('records', [])
        options = self.query_one('#review-records', OptionList)
        options.clear_options()
        options.add_options([(('[?] ' if row['applied'] is None else '[已应用] ' if row['applied'] else '[已撤销] ')
                              + row['path'] + ('…' if row['path_truncated'] else '')
                              + (f" [批次 {row['batch']['id'][:8]} #{row['batch']['index']}]" if 'batch' in row else '')) for row in self.rows])
        if page and 'error' not in page:
            if self.navigation == 'next' and self.view:
                self.back.append(self.view['offset'])
            elif self.navigation == 'previous' and self.back:
                self.back.pop()
            else:
                self.back = []
            self.view, self.selected = page, page['id']
            options.highlighted = next((index for index, row in enumerate(self.rows) if row['id'] == self.selected), None)
            state = {'matches_recorded_state': '当前内容与身份符合最近记录（不是验收）',
                     'changed_since_recording': '当前文件有记录外变化', 'unavailable': '当前父目录不可用',
                     'uncertain_pending_change': '有未结算改动，实际结果不确定'}[page['current']]
            self.query_one('#review-status', Static).update(self.host.safe(
                state + f"；字符位置 {page['offset']}；" + ('本记录已读完' if page['complete'] else '还有后续页')
                + '；控制字符已转义，差异可能不是最小形式'))
            self.query_one('#review-text', TextArea).load_text(self.host.safe(f"记录 ID {self.selected}\n\n{page['text']}"))
        else:
            self.view, self.back = None, []
            self.query_one('#review-text', TextArea).load_text('')
            error = (catalog.get('error') or (page or {}).get('error')
                     or f"暂无可审阅记录；{catalog.get('hidden_records', 0)} 条被隐私规则隐藏")
            self.query_one('#review-status', Static).update(self.host.safe(error))
        self.navigation = None
        self.buttons()

    def buttons(self):
        busy = self.host.busy
        self.query_one('#review-previous', Button).disabled = busy or not self.back or self.view is None
        self.query_one('#review-next', Button).disabled = busy or self.view is None or self.view['next_offset'] is None
        self.query_one('#review-refresh', Button).disabled = busy
        self.query_one('#review-records', OptionList).disabled = busy

    def load(self, identity=None, offset=0, snapshot=None, navigation='select'):
        if self.host.busy:
            return
        self.navigation = navigation
        self._payload_applied = False
        self.query_one('#review-text', TextArea).load_text('')
        self.query_one('#review-status', Static).update('正在重新检查来源、隐私及当前文件…')
        self.host.start(local=('change-review', {'identity': identity, 'offset': offset, 'snapshot': snapshot, 'screen': self}))
        self.buttons()

    def operation_finished(self, success):
        if not self.is_mounted:
            return
        if not success and not self._payload_applied:
            self.view, self.back = None, []
            self.query_one('#review-text', TextArea).load_text('')
            self.query_one('#review-status', Static).update('审阅已取消或失败；旧页面已清空，请重新读取。')
        self.buttons()

    @on(OptionList.OptionSelected)
    def selected_record(self, event):
        event.stop()
        if 0 <= event.option_index < len(self.rows):
            self.load(self.rows[event.option_index]['id'])

    @on(Button.Pressed)
    def pressed(self, event):
        event.stop()
        if event.button.id == 'review-next': self.action_next_page()
        elif event.button.id == 'review-previous': self.action_previous_page()
        elif event.button.id == 'review-refresh': self.load(self.selected, navigation='refresh')
        else: self.action_close_review()

    def action_next_page(self):
        if self.view and self.view['next_offset'] is not None:
            self.load(self.selected, self.view['next_offset'], self.view['snapshot'], 'next')

    def action_previous_page(self):
        if self.view and self.back:
            self.load(self.selected, self.back[-1], self.view['snapshot'], 'previous')

    def action_close_review(self):
        if self.host.busy:
            self.host.cancel_event.set()
            self.query_one('#review-status', Static).update('正在取消，请等待本地读取结束。')
        else:
            self.dismiss()
