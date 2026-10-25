"""Application-generated permission proposal; never renders model-supplied labels."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class PermissionApproval(ModalScreen[bool]):
    CSS = """
    PermissionApproval { align: center middle; background: $background 75%; }
    #permission-panel { width: 66; max-width: 95%; height: auto; padding: 1 2;
                        border: round $primary; background: $surface; }
    #permission-description { height: auto; margin: 1 0; }
    #permission-buttons { height: 3; align-horizontal: right; }
    #permission-buttons Button { width: auto; min-width: 10; margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False, priority=True)]

    def __init__(self, request: dict):
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        with Vertical(id="permission-panel"):
            yield Static("是否执行这项权限变更？")
            yield Static(self.request["description"], markup=False, id="permission-description")
            yield Static("仅确认上面这一项；取消后保持原权限。")
            with Horizontal(id="permission-buttons"):
                yield Button("取消", id="permission-cancel")
                yield Button("确认", id="permission-confirm", variant="primary")

    def on_mount(self):
        self.query_one("#permission-cancel", Button).focus()

    def action_cancel(self):
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed):
        self.dismiss(event.button.id == "permission-confirm")
