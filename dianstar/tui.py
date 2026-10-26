"""DianStar 终端界面：展示会话历史、键盘切换会话并处理聊天与斜杆命令。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import webbrowser
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError
from textual.actions import SkipAction
from rich.console import Group
from rich.text import Text
from rich.style import Style
from rich.panel import Panel
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Click, MouseDown, MouseUp, Resize
from textual.geometry import Offset, Size
from textual.message import Message
from textual.selection import Selection
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widgets import Button, Label, ListItem, ListView, RichLog, Static, TextArea

from .agent import ask_deepseek
from .permission_approval import PermissionApproval
from .chat_rendering import ChatMarkdown, StreamLines, is_web_link
from .clipboard import copy_windows_text
from .context_menu import WindowsCopyMenuClient, cursor_position
from .config import CONFIG_FILE, ENV_FILE, cycle_model, load_model_settings
from .config_ui import ConfigScreen
from .conversations import Conversation, list_conversations
from .welcome_art import welcome_art_lines
from .web_approval import PaidWebApproval
from .search_config import load_search_settings, save_search_settings


COMMANDS = {
    "/help": "显示命令帮助",
    "/config": "配置 API Key、模型、默认模型和快捷键",
    "/new": "创建新会话",
    "/rename": "重命名当前会话：/rename 名称",
    "/resume": "按名称或 ID 恢复会话：/resume 名称",
    "/switch": "按最近使用时间选择会话（方向键、回车）",
    "/compress": "清空后续请求的上下文，保留聊天记录",
    "/exit": "退出",
}


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


class DianStarApp(App[None]):
    """使用 Textual 绘制的本地聊天界面。"""

    TITLE = "DianStar"
    CSS = """
    Screen { layout: vertical; background: $surface; layers: base context-menu; }
    #toolbar { height: 1; padding: 0 1; }
    #toggle-sessions { height: 1; width: auto; min-width: 12; border: none; padding: 0 1; }
    #body { height: 1fr; }
    #sessions { display: none; width: 25; min-width: 18; border-right: solid $primary; }
    #sessions-title { height: 1; padding: 0 1; color: $text-muted; }
    #session-list { height: 1fr; }
    #chat-pane { width: 1fr; }
    #chat { height: 1fr; padding: 1 2; scrollbar-size-vertical: 1; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #input { height: auto; min-height: 1; max-height: 6; margin: 0 1; background: $boost; scrollbar-size-vertical: 1; }
    #hints { height: auto; max-height: 2; margin: 0 1; color: $text-muted; }
    #bottom { height: 1; margin: 0 1; }
    #key-help { width: 1fr; color: $text-muted; }
    #session-name { width: auto; max-width: 30; text-align: right; color: $text-muted; }
    """
    BINDINGS = [
        Binding("tab", "complete_command", "补全命令", priority=True, show=False),
        Binding("ctrl+l", "toggle_sessions", "收起或展开会话", priority=True, show=False),
        Binding("ctrl+q", "quit", "退出", show=False),
    ]

    def __init__(self, conversation: Conversation, *, config_file=CONFIG_FILE, env_file=ENV_FILE) -> None:
        super().__init__()
        self.conversation = conversation
        self.config_file = config_file
        self.env_file = env_file
        self.busy = False
        self._welcome_only = False
        self._installed_model_shortcut: str | None = None
        self._copy_menu_text = ""
        self._copy_menu_focus = None
        self._native_copy_menu_open = False
        self._native_menu = WindowsCopyMenuClient()
        self._chat_blocks = []
        self._expanded_thinking = set()
        self._active_thinking = None
        self._thinking_timer = None
        self._thinking_tail_start = None
        self._thinking_rendered = None
        self._thinking_parts = []
        self._new_thinking_part = True
        self._active_stream = None
        self._stream_timer = None
        self._stream_start = None
        self._stream_tail_start = None
        self._stream_lines = None
        self._stream_width = None
        self._stream_rendered = ""

    def on_chat_log_link_requested(self, event: ChatLog.LinkRequested) -> None:
        event.stop()
        if is_web_link(event.url):
            self.run_worker(self._open_web_link(event.url), group="browser")

    async def _open_web_link(self, url: str) -> None:
        try:
            opened = await asyncio.to_thread(webbrowser.open, url, new=2)
            if not opened:
                self._status("未能打开浏览器，可复制链接后访问。")
        except (OSError, webbrowser.Error):
            self._status("未能打开浏览器，可复制链接后访问。")

    def get_default_screen(self) -> Screen:
        return ChatScreen(id="_default")

    def copy_to_clipboard(self, text: str) -> None:
        if os.name != "nt" or self.is_headless:
            super().copy_to_clipboard(text)
            return
        try:
            copy_windows_text(text)
        except (OSError, subprocess.SubprocessError):
            self._status("复制失败：系统剪贴板暂不可用，请重试。")
            return
        self._clipboard = text

    def on_chat_log_copy_requested(self, event: ChatLog.CopyRequested) -> None:
        event.stop()
        if not event.text:
            return
        if os.name == "nt" and not self.is_headless:
            if not self._native_copy_menu_open:
                try:
                    position = cursor_position()
                except OSError:
                    self._status("无法打开系统菜单，请使用 Ctrl+C 复制。")
                    return
                self._native_copy_menu_open = True
                self.run_worker(self._show_native_copy_menu(event.text, position))
            return
        menu = self.query_one("#copy-menu", CopyMenu)
        self._copy_menu_text = event.text
        if not menu.display:
            self._copy_menu_focus = self.focused
        menu.styles.offset = (
            max(0, min(event.position.x, self.screen.size.width - 12)),
            max(0, min(event.position.y + 1, self.screen.size.height - 3)),
        )
        menu.display = True
        menu.query_one(Button).focus(scroll_visible=False)

    async def _show_native_copy_menu(self, text: str, position: tuple[int, int]) -> None:
        screen = self.screen
        original_selection = dict(screen.selections)
        try:
            choice = await self._native_menu.show(position)
            if choice == "copy":
                self.copy_to_clipboard(text)
            elif choice == "outside" and screen.is_attached and screen.selections == original_selection:
                screen.clear_selection()
        except (OSError, subprocess.SubprocessError, ValueError, TimeoutError):
            self._status("系统菜单暂不可用，请使用 Ctrl+C 复制。")
        finally:
            self._native_copy_menu_open = False

    def _close_copy_menu(self, *, restore_focus: bool = True) -> None:
        menu = self.query_one("#copy-menu", CopyMenu)
        if not menu.display:
            return
        menu.display = False
        self._copy_menu_text = ""
        if restore_focus and self._copy_menu_focus is not None and self._copy_menu_focus.is_attached:
            self._copy_menu_focus.focus(scroll_visible=False)
        self._copy_menu_focus = None

    def on_copy_menu_closed(self, event: CopyMenu.Closed) -> None:
        event.stop()
        self._close_copy_menu()

    def on_mouse_down(self, event: MouseDown) -> None:
        menu = self.query_one("#copy-menu", CopyMenu)
        if menu.display and not menu.region.contains(event.screen_x, event.screen_y):
            self._close_copy_menu(restore_focus=False)

    def compose(self) -> ComposeResult:
        with Horizontal(id="toolbar"):
            yield Button("显示会话", id="toggle-sessions")
        with Horizontal(id="body"):
            with Vertical(id="sessions"):
                yield Static("会话 · 最近使用", id="sessions-title")
                yield ListView(id="session-list")
            with Vertical(id="chat-pane"):
                yield ChatLog(id="chat", wrap=True, markup=False, auto_scroll=True)
                yield Static("", id="status")
                yield ChatInput(placeholder="输入消息；Enter 发送，Shift+Enter / Ctrl+J 换行",
                                id="input", compact=True, highlight_cursor_line=False)
                yield Static("", id="hints")
                with Horizontal(id="bottom"):
                    yield Static("", id="key-help")
                    yield Label("", id="session-name")
        yield CopyMenu(id="copy-menu")

    async def on_mount(self) -> None:
        if os.name == "nt" and not self.is_headless:
            self.run_worker(self._warm_copy_menu())
        self._install_model_shortcut()
        self._refresh_key_help()
        await self._refresh_sessions()
        self._render_history()
        if self._welcome_only:
            self.call_after_refresh(self._refresh_welcome)
        self.query_one("#input", ChatInput).focus()

    async def _warm_copy_menu(self) -> None:
        try:
            await self._native_menu.start()
        except (OSError, subprocess.SubprocessError, ValueError, TimeoutError):
            pass

    async def on_unmount(self) -> None:
        await self._native_menu.close()

    def on_resize(self, event: Resize) -> None:
        if self.is_mounted:
            self._close_copy_menu()
        if self._welcome_only:
            self.call_after_refresh(self._refresh_welcome)

    def _refresh_welcome(self) -> None:
        # 延迟布局回调执行时，用户可能已经开始或完成了一轮对话。
        if self._welcome_only and not self.busy:
            self._render_history()

    def _render_history(self) -> None:
        if self.busy:
            return
        self._close_copy_menu()
        self._clear_stream()
        self._reset_active_thinking()
        self._expanded_thinking.clear()
        self._chat_blocks = []
        # 旧会话把思考保存在模型消息中，按用户轮次对应，避免重复问题串轮。
        reasoning_turns = []
        for message in self.conversation.model_messages:
            if message.get("role") == "user":
                reasoning_turns.append([])
            elif message.get("role") == "assistant" and reasoning_turns:
                content = message.get("reasoning_content")
                if isinstance(content, str) and content:
                    reasoning_turns[-1].append(content)
        compatible = (len(self.conversation.messages) % 2 == 0
                      and len(reasoning_turns) == len(self.conversation.messages) // 2)
        for index, message in enumerate(self.conversation.messages):
            reasoning = message.get("reasoning_content", "")
            if message["role"] == "assistant" and not reasoning and compatible:
                reasoning = "\n\n".join(reasoning_turns[index // 2])
            if message["role"] == "assistant" and reasoning:
                self._chat_blocks.append({"kind": "thinking", "content": reasoning})
            self._chat_blocks.append({"kind": "message", **message})
        self._redraw_chat()

    def _redraw_chat(self) -> None:
        chat = self.query_one("#chat", RichLog)
        chat.clear()
        if self._chat_blocks:
            self._welcome_only = False
            for index, block in enumerate(self._chat_blocks):
                if block["kind"] == "thinking":
                    if index == self._active_thinking:
                        self._thinking_tail_start = len(chat.lines)
                    chat.write(self._thinking_panel(index), width=max(1, chat.scrollable_content_region.width))
                elif block["kind"] == "stream":
                    self._begin_stream_render()
                    self._paint_stream()
                else:
                    self._write_message(block["role"], block["content"])
        else:
            self._welcome_only = True
            art = welcome_art_lines(max(0, chat.size.width - 4), max(1, chat.size.height - 2))
            content_height = max(0, chat.size.height - 2)
            for _ in range(max(0, (content_height - len(art)) // 2)):
                chat.write(Text(" "))
            for line in art:
                chat.write(line)
        self.query_one("#session-name", Label).update(self.conversation.name or "未命名会话")

    def _show_message(self, role: str, content: str) -> None:
        self._chat_blocks.append({"kind": "message", "role": role, "content": content})
        self._write_message(role, content)

    def _write_message(self, role: str, content: str) -> None:
        self._welcome_only = False
        chat = self.query_one("#chat", RichLog)
        chat.write(Text("主包" if role == "user" else "DianStar", style="bold cyan" if role == "user" else "bold green"))
        chat.write(Text(content) if role == "user" else ChatMarkdown(content))
        chat.write("")

    def _status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _stream_answer(self, content: str) -> None:
        """Append the unfinished answer inside the log, with bounded repaint frequency."""
        if not content:
            self._clear_stream(remove=True)
            return
        if self._active_stream is None:
            self._paint_thinking()
            self._active_stream = len(self._chat_blocks)
            self._chat_blocks.append({"kind":"stream", "content":content})
            self._begin_stream_render()
            self._paint_stream()
            self._status("正在回答…")
        else:
            self._chat_blocks[self._active_stream]["content"] = content
            if self._stream_timer is None:
                self._stream_timer = self.set_timer(1 / 30, self._paint_stream)

    def _begin_stream_render(self) -> None:
        chat = self.query_one("#chat", ChatLog)
        self._stream_start = len(chat.lines)
        chat.write(Text("DianStar", style="bold green"))
        self._stream_tail_start = len(chat.lines)
        self._stream_width = chat.scrollable_content_region.width
        self._stream_lines = StreamLines(self._stream_width)
        self._stream_rendered = ""

    def _paint_stream(self) -> None:
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        if self._active_stream is None:
            return
        chat = self.query_one("#chat", ChatLog)
        content = self._chat_blocks[self._active_stream]["content"]
        # 已完成的显示行只写一次；长段落也只排版新收到的字符。
        if (self._stream_width != chat.scrollable_content_region.width
                or not content.startswith(self._stream_rendered)):
            self._redraw_chat()
            return
        if content == self._stream_rendered:
            return
        follow = chat.is_vertical_scroll_end
        chat.truncate_tail(self._stream_tail_start)
        width = max(1, chat.scrollable_content_region.width)
        completed = self._stream_lines.append(content[len(self._stream_rendered):])
        if completed:
            chat.write(Text("\n".join(completed), no_wrap=True, overflow="crop"),
                       width=width, scroll_end=False)
        self._stream_tail_start = len(chat.lines)
        if self._stream_lines.tail:
            chat.write(Text(self._stream_lines.tail, no_wrap=True, overflow="crop"),
                       width=width, scroll_end=False)
        self._stream_rendered = content
        chat.refresh()
        if follow:
            chat.call_after_refresh(chat.scroll_end, animate=False, immediate=True, x_axis=False)

    def _clear_stream(self, *, remove: bool = False) -> None:
        if self._stream_timer is not None:
            self._stream_timer.stop()
            self._stream_timer = None
        if remove and self._active_stream is not None:
            chat = self.query_one("#chat", ChatLog)
            self._chat_blocks.pop(self._active_stream)
            if self._stream_start is not None:
                chat.truncate_tail(self._stream_start)
        self._active_stream = None
        self._stream_start = self._stream_tail_start = None
        self._stream_lines = None
        self._stream_width = None
        self._stream_rendered = ""

    def _finish_answer(self, answer: str) -> None:
        if self._active_stream is None:
            self._show_message("assistant", answer)
            return
        chat = self.query_one("#chat", ChatLog)
        self._chat_blocks[self._active_stream] = {"kind":"message", "role":"assistant", "content":answer}
        chat.replace_tail(self._stream_start, Group(Text("DianStar", style="bold green"),
                                                  ChatMarkdown(answer), Text("")))
        self._clear_stream()

    def _receive_thinking(self, content: str) -> None:
        if not content:
            self._new_thinking_part = True
            return
        if self._new_thinking_part:
            self._thinking_parts.append(content)
            self._new_thinking_part = False
        else:
            self._thinking_parts[-1] = content
        self._stream_thinking("\n\n".join(self._thinking_parts))

    def _stream_thinking(self, content: str) -> None:
        if not content:
            self._reset_active_thinking()
            return
        if self._active_thinking is None:
            self._active_thinking = len(self._chat_blocks)
            self._chat_blocks.append({"kind": "thinking", "content": content})
            self._thinking_tail_start = len(self.query_one("#chat", RichLog).lines)
            self._paint_thinking()
        else:
            self._chat_blocks[self._active_thinking]["content"] = content
            if self._thinking_timer is None:
                self._thinking_timer = self.set_timer(0.1, self._paint_thinking)

    def _reset_active_thinking(self) -> None:
        if self._thinking_timer is not None:
            self._thinking_timer.stop()
            self._thinking_timer = None
        self._active_thinking = None
        self._thinking_tail_start = None
        self._thinking_rendered = None
        self._thinking_parts = []
        self._new_thinking_part = True

    def _thinking_panel(self, index: int) -> Panel:
        expanded = index in self._expanded_thinking
        content = self._chat_blocks[index]["content"]
        title = Text("模型思考 · 收起" if expanded else "模型思考 · 展开完整内容",
                     style=Style(color="#6b9cbb", meta={"@click": f"app.toggle_thinking({index})"}))
        visible = content if expanded else "\n".join(content.splitlines()[-4:])
        return Panel(Text(visible, style="#aeb7c6", no_wrap=not expanded, overflow="ellipsis"),
                     title=title, title_align="left", border_style="#365f7d", padding=(0, 1))

    def _paint_thinking(self) -> None:
        if self._thinking_timer is not None:
            self._thinking_timer.stop()
            self._thinking_timer = None
        if self._active_thinking is None:
            return
        signature = (self._active_thinking, self._chat_blocks[self._active_thinking]["content"],
                     self._active_thinking in self._expanded_thinking)
        if signature == self._thinking_rendered:
            return
        chat = self.query_one("#chat", ChatLog)
        if self._active_thinking == len(self._chat_blocks) - 1 and self._thinking_tail_start is not None:
            chat.replace_tail(self._thinking_tail_start, self._thinking_panel(self._active_thinking))
        else:
            self._redraw_chat()
        self._thinking_rendered = signature

    def action_toggle_thinking(self, index: int) -> None:
        if not 0 <= index < len(self._chat_blocks) or self._chat_blocks[index]["kind"] != "thinking":
            return
        if index in self._expanded_thinking:
            self._expanded_thinking.remove(index)
        else:
            self._expanded_thinking.add(index)
        chat = self.query_one("#chat", RichLog)
        scroll_y = chat.scroll_y
        self._redraw_chat()
        chat.call_after_refresh(chat.scroll_to, y=scroll_y, animate=False)

    def _set_sessions_visible(self, visible: bool) -> None:
        self.query_one("#sessions", Vertical).display = visible
        self.query_one("#toggle-sessions", Button).label = "收起会话" if visible else "显示会话"
        if self._welcome_only:
            self.call_after_refresh(self._refresh_welcome)
        if not visible:
            self.query_one("#input", ChatInput).focus()

    def action_toggle_sessions(self) -> None:
        if isinstance(self.screen, ModalScreen):
            raise SkipAction()
        self._close_copy_menu()
        sidebar = self.query_one("#sessions", Vertical)
        self._set_sessions_visible(not sidebar.display)

    def _refresh_key_help(self) -> None:
        settings = load_model_settings(self.config_file)
        self.query_one("#key-help", Static).update(
            f"▣ 模型: {settings.default_model or '未设置'}"
            "  ·  Ctrl+L 会话栏  ·  /help 命令")

    def _install_model_shortcut(self) -> None:
        """Replace the active Textual binding when the configured key changes."""
        shortcut = load_model_settings(self.config_file).switch_model_shortcut
        previous = self._installed_model_shortcut
        if previous == shortcut:
            return
        if previous is not None:
            bindings = self._bindings.key_to_bindings.get(previous, [])
            bindings[:] = [binding for binding in bindings
                           if binding.action != "cycle_model"]
            if not bindings:
                self._bindings.key_to_bindings.pop(previous, None)
        self._bindings.bind(shortcut, "cycle_model", "下一个模型",
                            show=False, priority=True)
        self._installed_model_shortcut = shortcut
        self.refresh_bindings()

    def action_cycle_model(self) -> None:
        if isinstance(self.screen, ModalScreen):
            raise SkipAction()
        self._close_copy_menu()
        if self.busy:
            return
        try:
            cycle_model(self.config_file, env_file=self.env_file)
            self._refresh_key_help()
        except (ValueError, OSError) as error:
            self._status(f"无法切换模型：{error}")
        self.query_one("#input", ChatInput).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "copy-selection":
            text = self._copy_menu_text
            self._close_copy_menu()
            if text:
                self.copy_to_clipboard(text)
        elif event.button.id == "toggle-sessions":
            self.action_toggle_sessions()
            self.query_one("#input", ChatInput).focus()

    async def _refresh_sessions(self) -> None:
        view = self.query_one("#session-list", ListView)
        await view.remove_children()
        entries = list_conversations(self.conversation.directory)
        if entries:
            await view.mount(*[
                ListItem(Label(f"{entry.name or '未命名会话'}  {entry.updated_at:%m-%d %H:%M}"), id=f"session-{entry.id}")
                for entry in entries
            ])
            view.index = next((i for i, entry in enumerate(entries) if entry.id == self.conversation.id), 0)

    async def _open_conversation(self, conversation_id: str) -> None:
        if self.busy:
            self._status("请等待当前回答完成。")
            return
        try:
            self.conversation = Conversation.load(conversation_id, self.conversation.directory)
            self._render_history()
            await self._refresh_sessions()
            self._status(f"已切换到 {self.conversation.name or '未命名会话'}")
            self.query_one("#input", ChatInput).focus()
        except (ValueError, OSError) as error:
            self._status(f"无法读取会话：{error}")

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id and event.item.id.startswith("session-"):
            await self._open_conversation(event.item.id.removeprefix("session-"))

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "input":
            return
        value = event.text_area.text
        if value.startswith("/") and not any(char.isspace() for char in value):
            matches = [command for command in COMMANDS if command.startswith(value)]
            self.query_one("#hints", Static).update("  ".join(matches))
        else:
            self.query_one("#hints", Static).update("")

    def action_complete_command(self) -> None:
        if isinstance(self.screen, ModalScreen) or self.query_one("#copy-menu", CopyMenu).display:
            raise SkipAction()
        field = self.query_one("#input", ChatInput)
        value = field.text
        if not value.startswith("/") or any(char.isspace() for char in value):
            return
        matches = [command for command in COMMANDS if command.startswith(value)]
        if matches:
            field.text = matches[0] + (" " if matches[0] in {"/rename", "/resume"} else "")
            field.move_cursor(field.document.end)

    async def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        value = event.value
        single_line = "\n" not in value and "\r" not in value
        if single_line:
            value = value.strip()
        field = self.query_one("#input", ChatInput)
        if self.busy and not (single_line and value in {"/exit", "/help"}):
            self._status("请等待当前回答完成。")
            return
        field.text = ""
        if not value.strip():
            return
        if single_line and value.startswith("/"):
            await self._command(value)
            return
        self.busy = True
        self._show_message("user", value)
        self._status("正在思考…")
        self.run_worker(self._ask(value), exclusive=True)

    async def _ask(self, question: str) -> None:
        self._stream_thinking("")
        try:
            history = self.conversation.model_context_messages()
            history_length = len(history)
            answer = await ask_deepseek(
                question, history=history,
                on_stream=self._stream_answer, on_thinking=self._receive_thinking,
                env_file=self.env_file, on_paid_web=self._approve_paid_web,
                on_permission=self._approve_permission,
            )
            self._paint_thinking()
            reasoning = (self._chat_blocks[self._active_thinking]["content"]
                         if self._active_thinking is not None else None)
            self.conversation.add_exchange(question, answer, reasoning_content=reasoning,
                                           model_turn_messages=history[history_length:] or None)
            self._finish_answer(answer)
            self._stream_answer("")
            self.query_one("#session-name", Label).update(self.conversation.name or "未命名会话")
            await self._refresh_sessions()
            self._status("")
        except (APITimeoutError, TimeoutError):
            self._status("DeepSeek 在 60 秒内未响应，请重试。")
        except APIConnectionError:
            self._status("无法连接 DeepSeek，请检查网络后重试。")
        except APIStatusError as error:
            self._status("API Key 被拒绝（HTTP 401）。" if error.status_code == 401 else f"DeepSeek 返回 HTTP {error.status_code}。")
        except (ValueError, OpenAIError, OSError) as error:
            self._status(f"请求失败：{error}")
        finally:
            self._paint_thinking()
            self._reset_active_thinking()
            self._stream_answer("")
            self.busy = False
            self.query_one("#input", ChatInput).focus()

    async def _approve_paid_web(self, request: dict) -> str:
        choice = await self.push_screen_wait(PaidWebApproval(request))
        if choice == "always" and not request.get("settings_change"):
            try:
                settings = load_search_settings(self.config_file.with_name("search.json"))
                save_search_settings("auto", settings.key_id, paid_policy="allow",
                                     path=self.config_file.with_name("search.json"),
                                     config_file=self.config_file, env_file=self.env_file)
            except (ValueError, OSError):
                self._status("本轮已允许；未能保存长期授权，下次仍会询问。")
        return choice

    async def _approve_permission(self, request: dict) -> bool:
        return await self.push_screen_wait(PermissionApproval(request))

    async def _command(self, command_line: str) -> None:
        command, _, argument = command_line.partition(" ")
        argument = argument.strip()
        if self.busy and command not in {"/help", "/exit"}:
            self._status("请等待当前回答完成。")
            return
        try:
            if command == "/help":
                self._show_message("assistant", "\n".join(f"{name}：{description}" for name, description in COMMANDS.items()))
            elif command == "/config":
                self.push_screen(ConfigScreen(config_file=self.config_file, env_file=self.env_file),
                                 self._config_closed)
            elif command == "/exit":
                self.exit()
            elif command == "/switch":
                self._set_sessions_visible(True)
                self.query_one("#session-list", ListView).focus()
                self._status("用 ↑/↓ 选择会话，按 Enter 恢复。")
            elif command == "/new":
                self.conversation = Conversation.create(self.conversation.directory)
                self._render_history()
                await self._refresh_sessions()
                self._status("已创建新会话。")
            elif command == "/rename":
                if not argument:
                    self._status("输入 /rename 新名称")
                    self.query_one("#input", ChatInput).text = "/rename "
                    return
                self.conversation.rename(argument)
                self.query_one("#session-name", Label).update(self.conversation.name or "未命名会话")
                await self._refresh_sessions()
                self._status("会话已重命名。")
            elif command == "/resume":
                if not argument:
                    self._status("输入 /resume 会话名，或用 /switch 选择。")
                    self.query_one("#input", ChatInput).text = "/resume "
                    return
                matches = [entry for entry in list_conversations(self.conversation.directory) if entry.name == argument]
                if len(matches) > 1:
                    self._status("有多个同名会话，请用 /switch 选择。")
                else:
                    await self._open_conversation(matches[0].id if matches else argument)
            elif command == "/compress":
                self.conversation.compress()
                self._status("后续请求的上下文已清空；聊天记录仍可查看。")
            else:
                self._status("未知命令。输入 /help 查看命令。")
        except (ValueError, OSError) as error:
            self._status(f"操作失败：{error}")

    def _config_closed(self, saved: bool) -> None:
        self._install_model_shortcut()
        self._refresh_key_help()
        if saved:
            self._status("配置已保存，下次提问将使用所选默认模型。")
        self.query_one("#input", ChatInput).focus()
