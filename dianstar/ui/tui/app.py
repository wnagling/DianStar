"""DianStar 终端界面：展示会话历史、键盘切换会话并处理聊天与斜杆命令。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import webbrowser
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import MouseDown, Resize
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Label, ListItem, ListView, Static, TextArea

from dianstar.app.runtime import run_turn
from dianstar.app.chat_service import ask_and_save
from dianstar.app import commands
from dianstar.app.commands import COMMANDS
from dianstar.ui.tui.screens.permission_approval import PermissionApproval
from dianstar.ui.tui.rendering import is_web_link
from dianstar.platform.windows.clipboard import copy_windows_text
from dianstar.platform.windows.context_menu import WindowsCopyMenuClient, cursor_position
from dianstar.config.paths import CONFIG_FILE, ENV_FILE
from dianstar.config.models import cycle_model, load_model_settings
from dianstar.ui.tui.screens.config import ConfigScreen
from dianstar.sessions.conversations import Conversation, list_conversations
from dianstar.ui.tui.screens.web_approval import PaidWebApproval
from dianstar.config.search import load_search_settings, save_search_settings


from dianstar.ui.tui.widgets import ChatLog, CopyMenu, ChatScreen, ChatInput
from dianstar.ui.tui.chat_view import ChatView


class DianStarApp(ChatView, App[None]):
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


    def _status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)


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
            answer = await ask_and_save(
                question, self.conversation, runner=run_turn,
                on_stream=self._stream_answer, on_thinking=self._receive_thinking,
                env_file=self.env_file, on_paid_web=self._approve_paid_web,
                on_permission=self._approve_permission,
            )
            self._paint_thinking()
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
                self.conversation = commands.new_conversation(self.conversation)
                self._render_history()
                await self._refresh_sessions()
                self._status("已创建新会话。")
            elif command == "/rename":
                if not argument:
                    self._status("输入 /rename 新名称")
                    self.query_one("#input", ChatInput).text = "/rename "
                    return
                commands.rename_conversation(self.conversation, argument)
                self.query_one("#session-name", Label).update(self.conversation.name or "未命名会话")
                await self._refresh_sessions()
                self._status("会话已重命名。")
            elif command == "/resume":
                if not argument:
                    self._status("输入 /resume 会话名，或用 /switch 选择。")
                    self.query_one("#input", ChatInput).text = "/resume "
                    return
                try:
                    target = commands.resolve_conversation(argument, self.conversation.directory)
                except commands.AmbiguousConversation as error:
                    self._status(str(error))
                else:
                    await self._open_conversation(target)
            elif command == "/compress":
                commands.compress_conversation(self.conversation)
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
