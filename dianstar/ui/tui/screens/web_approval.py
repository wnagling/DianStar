"""付费网页备用审批：默认不调用，确认后继续当前任务。"""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class PaidWebApproval(ModalScreen[str]):
    CSS = """
    PaidWebApproval { align: center middle; background: $background 75%; }
    #paid-web-panel { width: 66; max-width: 95%; height: auto; padding: 1 2;
                      border: round $primary; background: $surface; }
    #paid-web-target { height: auto; max-height: 4; margin: 1 0; color: $text-muted; }
    #paid-web-buttons { height: 3; margin-top: 1; align-horizontal: right; }
    #paid-web-buttons Button { width: auto; min-width: 10; margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "deny", "不允许", priority=True, show=False)]

    def __init__(self, request: dict):
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="paid-web-panel"):
            settings_change = self.request.get("settings_change", False)
            yield Static("是否始终允许付费网页服务？" if settings_change else "是否允许使用 API 继续？")
            if not settings_change:
                yield Static("免费访问受到页面限制或无法提取内容。")
                if self.request.get("free_message"):
                    yield Static(self.request["free_message"], markup=False)
            yield Static(f"{self.request['operation']}：{self.request['target']}", markup=False,
                         id="paid-web-target")
            if settings_change:
                yield Static("确认后保存长期授权，后续提问也可使用付费 API 备用；仍优先免费访问。\n"
                             "以后可以说“撤销付费搜索的始终允许”恢复询问。")
            else:
                yield Static("将使用 DeepSeek API 备用，可能产生费用。\n"
                             f"本次提问最多再调用 {self.request['remaining_calls']} 次 API，免费访问不占此额度。\n"
                             "仅本次提问的授权不会延续到下一次提问；仍优先免费访问。")
            with Horizontal(id="paid-web-buttons"):
                yield Button("不允许", id="paid-web-deny")
                yield Button("始终允许", id="paid-web-always")
                if not settings_change:
                    yield Button("仅本次提问", variant="primary", id="paid-web-turn")

    def on_mount(self) -> None:
        self.query_one("#paid-web-deny", Button).focus()

    def action_deny(self) -> None:
        self.dismiss("deny")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        choices = {"paid-web-deny": "deny", "paid-web-always": "always", "paid-web-turn": "turn"}
        if event.button.id in choices:
            self.dismiss(choices[event.button.id])
