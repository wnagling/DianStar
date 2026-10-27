"""Textual input, selection, link and copy-menu widgets."""
from rich.text import Text
from rich.style import Style
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.events import Click, MouseDown, MouseUp
from textual.geometry import Offset, Size
from textual.message import Message
from textual.screen import Screen
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Button, RichLog, TextArea
from dianstar.ui.tui.rendering import is_web_link


class ChatLog(RichLog):
    """为富文本聊天记录提供字符坐标、选区高亮和文本提取。"""

    class CopyRequested(Message):
        def __init__(self, text: str, position: Offset = Offset(0, 0)) -> None:
            super().__init__()
            self.text = text
            self.position = position

    class LinkRequested(Message):
        def __init__(self, url: str) -> None:
            super().__init__()
            self.url = url

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        # Extract from display rows directly: splitlines() drops the final blank
        # row and also treats some characters inside a row as line boundaries.
        if selection.start is not None and selection.end is not None:
            selection = Selection.from_offsets(selection.start, selection.end)
        start = max(0, selection.start.y) if selection.start is not None else 0
        end = min(len(self.lines) - 1, selection.end.y) if selection.end is not None else len(self.lines) - 1
        if not self.lines or start > end:
            return None
        selected = []
        for row in range(start, end + 1):
            text = self.lines[row].text.rstrip(" ")
            span = selection.get_span(row)
            if span is not None:
                left, right = span
                selected.append(text[max(0, left):None if right == -1 else max(0, right)])
        return "\n".join(selected), "\n"

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        row = scroll_y + y
        width = self.scrollable_content_region.width
        if row >= len(self.lines):
            return Strip.blank(width, self.rich_style)
        line = self.lines[row]
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(row)) is not None:
            text = Text.assemble(*((segment.text, segment.style or "") for segment in line))
            start, end = span
            text.stylize(Style(color="#ffffff", bgcolor="#245b85"),
                         start, len(text) if end == -1 else end)
            line = Strip(text.render(self.app.console))
        # 在裁剪前附加字符坐标，保持中文宽字符和横向滚动后的选区准确。
        return (line.apply_offsets(0, row)
                .crop_extend(scroll_x, scroll_x + width, self.rich_style)
                .apply_style(self.rich_style))

    def clear(self):
        if self.is_attached:
            self.screen.selections = {widget: selection for widget, selection
                                      in self.screen.selections.items() if widget is not self}
        return super().clear()

    def truncate_tail(self, start: int) -> None:
        if self.is_attached:
            selection = self.screen.selections.get(self)
            if selection is not None and (selection.end is None or selection.end.y >= start):
                self.screen.selections = {widget: value for widget, value
                                          in self.screen.selections.items() if widget is not self}
        del self.lines[start:]
        self._line_cache.clear()
        self.virtual_size = Size(self.virtual_size.width, len(self.lines))
        self.refresh()

    def replace_tail(self, start: int, renderable) -> None:
        follow = self.is_vertical_scroll_end
        self.truncate_tail(start)
        self.write(renderable, width=max(1, self.scrollable_content_region.width), scroll_end=follow)
        self.refresh()



class CopyMenu(Vertical):
    """留在聊天页面、定位到鼠标旁的轻量菜单。"""

    DEFAULT_CSS = """
    CopyMenu { display: none; position: absolute; overlay: screen; layer: context-menu;
               width: 12; height: 3; padding: 0; margin: 0;
               border: solid $foreground 15%; background: $surface; }
    CopyMenu Button { width: 100%; min-width: 0; height: 1; margin: 0; padding: 0 1;
                      border: none; background: $surface; color: $text; text-style: none; }
    CopyMenu Button:hover, CopyMenu Button:focus { background: $boost; text-style: none; }
    CopyMenu Button.-active { background: $boost; }
    """
    BINDINGS = [Binding("escape", "cancel", "取消", show=False)]

    class Closed(Message):
        pass

    def compose(self) -> ComposeResult:
        yield Button("复制", id="copy-selection", compact=True)

    def action_cancel(self) -> None:
        self.post_message(self.Closed())



class ChatScreen(Screen):
    """只允许左键驱动 Textual 选区，右键在屏幕选择逻辑之前处理。"""

    def _forward_event(self, event) -> None:
        if isinstance(event, Click) and event.button == 1 and event.chain == 1:
            target, _ = self.get_widget_at(event.screen_x, event.screen_y)
            if isinstance(target, ChatLog):
                selected = target.get_selection(target.text_selection) if target.text_selection else None
                url = self.get_style_at(event.screen_x, event.screen_y).link
                if url and not (selected and selected[0]) and is_web_link(url):
                    target.post_message(ChatLog.LinkRequested(url))
                    return
        if isinstance(event, Click) and event.button == 1 and event.chain >= 2:
            target, _ = self.get_widget_at(event.screen_x, event.screen_y)
            if isinstance(target, ChatInput):
                target.action_select_all()
                return
            chat = self.query_one("#chat", ChatLog)
            widget, offset = self.get_widget_and_offset_at(event.screen_x, event.screen_y)
            if widget is chat and chat.content_region.contains(event.screen_x, event.screen_y):
                row = (offset.y if offset is not None else
                       int(event.screen_y - chat.content_region.y + chat.scroll_offset.y))
                self.clear_selection()
                if 0 <= row < len(chat.lines):
                    text = chat.lines[row].text.rstrip(" ")
                    if text:
                        self.selections = {chat: Selection(Offset(0, row), Offset(len(text), row))}
                return
        if isinstance(event, MouseDown) and event.button == 1:
            self.clear_selection()
        if isinstance(event, MouseDown) and event.button == 3:
            chat = self.query_one("#chat", ChatLog)
            if chat.region.contains(event.screen_x, event.screen_y):
                selection = self.selections.get(chat)
                selected = chat.get_selection(selection) if selection is not None else None
                chat.post_message(ChatLog.CopyRequested(selected[0] if selected else "", event.screen_offset))
                return
        # Textual 8.2 的屏幕转发会用任意按键重新开始选择；Widget.stop 已经太晚。
        if isinstance(event, (MouseDown, MouseUp, Click)) and event.button != 1:
            return
        super()._forward_event(event)



class ChatInput(TextArea):
    """保留粘贴文本的换行，只有明确按下发送键才提交。"""

    ALLOW_SELECT = False  # 使用编辑器内部选区，避免页面级全选。
    BINDINGS = [
        Binding("ctrl+a", "select_all", "全选输入", priority=True, show=False),
        Binding("enter", "submit", "发送", priority=True, show=False),
        Binding("shift+enter,ctrl+j", "insert_newline", "换行", priority=True, show=False),
    ]

    class Submitted(Message):
        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self.text))

    def action_select_all(self) -> None:
        self.screen.clear_selection()
        super().action_select_all()

    def action_insert_newline(self) -> None:
        result = self.replace("\n", *self.selection, maintain_selection_offset=False)
        self.move_cursor(result.end_location)
