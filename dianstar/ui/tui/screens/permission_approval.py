"""Application-generated permission proposal; never renders model-supplied labels."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Static
from rich.cells import cell_len
from dianstar.security.approval_summary import approval_details


class PermissionApproval(ModalScreen[bool]):
    CSS = """
    PermissionApproval { align: center bottom; background: $background 20%; }
    #permission-panel { width: 72; max-width: 94%; height: 16; max-height: 85%; padding: 0 1; margin-bottom: 1;
                        border: round $primary; background: $surface; }
    #permission-scroll { height: 1fr; min-height: 1; }
    #permission-description, #permission-raw-details { height: auto; margin: 0; }
    #permission-raw-details { display: none; }
    #permission-details-toggle { width: auto; min-width: 10; height: 1; border: none; background: transparent; color: $text-muted; padding: 0; }
    #permission-note { height: 1; color: $text-muted; }
    #permission-buttons { height: 3; align-horizontal: right; }
    #permission-buttons Button { width: auto; min-width: 8; margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", show=False, priority=True)]

    def __init__(self, request: dict):
        super().__init__()
        self.request = request
        self.showing_details = False

    def compose(self) -> ComposeResult:
        with Vertical(id="permission-panel"):
            yield Static("需要你的确认")
            with VerticalScroll(id="permission-scroll"):
                summary = self.request.get("summary", self.request["description"])
                yield Static(summary, markup=False, id="permission-description")
                if summary != self.request["description"]:
                    yield Static(approval_details(self.request["description"]), markup=False, id="permission-raw-details")
            if self.request.get("summary", self.request["description"]) != self.request["description"]:
                yield Button("查看详情", id="permission-details-toggle")
            yield Static("确认后按以上范围执行；Esc 取消。", id="permission-note")
            with Horizontal(id="permission-buttons"):
                yield Button("取消", id="permission-cancel")
                yield Button("确认", id="permission-confirm", variant="primary")

    def on_mount(self):
        self._size_card()
        self.query_one("#permission-cancel", Button).focus()

    def on_resize(self):
        if self.is_mounted:
            self._size_card()

    def _size_card(self):
        width = max(12, min(72, int(self.size.width * .94)) - 4)
        text = self.request.get("summary", self.request["description"])
        rows = sum(max(1, (cell_len(line) + width - 1) // width) for line in text.splitlines())
        self.query_one("#permission-panel").styles.height = min(18, max(10, rows + 8))

    def action_cancel(self):
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "permission-details-toggle":
            event.stop()
            self.showing_details = not self.showing_details
            self.query_one("#permission-description").display = not self.showing_details
            self.query_one("#permission-raw-details").display = self.showing_details
            event.button.label = "返回说明" if self.showing_details else "查看详情"
            self.query_one("#permission-scroll", VerticalScroll).scroll_home(animate=False)
        elif event.button.id in {"permission-confirm", "permission-cancel"}:
            self.dismiss(event.button.id == "permission-confirm")
