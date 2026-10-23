"""DianStar 终端界面：展示会话历史、键盘切换会话并处理聊天与斜杆命令。"""

from __future__ import annotations

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAIError
from textual.actions import SkipAction
from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.events import Resize
from textual.widgets import Button, Input, Label, ListItem, ListView, RichLog, Static

from .agent import ask_deepseek
from .config import CONFIG_FILE, ENV_FILE, load_model_settings
from .config_ui import ConfigScreen
from .conversations import Conversation, list_conversations
from .welcome_art import welcome_art_lines


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


class DianStarApp(App[None]):
    """使用 Textual 绘制的本地聊天界面。"""

    TITLE = "DianStar"
    CSS = """
    Screen { layout: vertical; background: $surface; }
    #toolbar { height: 1; padding: 0 1; }
    #toggle-sessions { height: 1; width: auto; min-width: 12; border: none; padding: 0 1; }
    #body { height: 1fr; }
    #sessions { display: none; width: 25; min-width: 18; border-right: solid $primary; }
    #sessions-title { height: 1; padding: 0 1; color: $text-muted; }
    #session-list { height: 1fr; }
    #chat-pane { width: 1fr; }
    #chat { height: 1fr; padding: 1 2; scrollbar-size-vertical: 1; }
    #thinking { display: none; height: 6; margin: 0 1; padding: 0 1; border: round $secondary; scrollbar-size-vertical: 0; }
    #stream { display: none; height: auto; max-height: 8; margin: 0 1; padding: 0 1; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #input { height: 1; margin: 0 1; background: $boost; }
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

    def compose(self) -> ComposeResult:
        with Horizontal(id="toolbar"):
            yield Button("显示会话", id="toggle-sessions")
        with Horizontal(id="body"):
            with Vertical(id="sessions"):
                yield Static("会话 · 最近使用", id="sessions-title")
                yield ListView(id="session-list")
            with Vertical(id="chat-pane"):
                yield RichLog(id="chat", wrap=True, markup=False, auto_scroll=True)
                thinking = RichLog(id="thinking", wrap=True, markup=False, auto_scroll=True)
                thinking.border_title = "模型思考"
                yield thinking
                yield Static("", id="stream")
                yield Static("", id="status")
                yield Input(placeholder="输入消息；/ 查看命令", id="input", compact=True)
                yield Static("", id="hints")
                with Horizontal(id="bottom"):
                    yield Static("", id="key-help")
                    yield Label("", id="session-name")

    async def on_mount(self) -> None:
        self._install_model_shortcut()
        self._refresh_key_help()
        await self._refresh_sessions()
        self._render_history()
        self.call_after_refresh(self._render_history)
        self.query_one("#input", Input).focus()

    def on_resize(self, event: Resize) -> None:
        if self._welcome_only:
            self.call_after_refresh(self._render_history)

    def _render_history(self) -> None:
        chat = self.query_one("#chat", RichLog)
        chat.clear()
        if self.conversation.messages:
            for message in self.conversation.messages:
                self._show_message(message["role"], message["content"])
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
        self._welcome_only = False
        chat = self.query_one("#chat", RichLog)
        chat.write(Text("主包" if role == "user" else "DianStar", style="bold cyan" if role == "user" else "bold green"))
        chat.write(Text(content) if role == "user" else Markdown(content))
        chat.write("")

    def _status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)

    def _stream_answer(self, content: str) -> None:
        """Show the unfinished answer below the chat history until it is saved."""
        widget = self.query_one("#stream", Static)
        if not content:
            widget.display = False
            widget.update("")
            return
        visible = content[-max(240, (widget.size.width - 4) * 6):]
        widget.update(Group(Text("DianStar", style="bold green"), Markdown(visible)))
        widget.display = True
        self._status("正在回答…")

    def _stream_thinking(self, content: str) -> None:
        """Show the latest thinking in a four-line boxed viewport."""
        widget = self.query_one("#thinking", RichLog)
        widget.clear()
        widget.display = bool(content)
        if content:
            visible = content[-max(512, widget.size.width * 12):]
            widget.write(Text(visible, style="#aeb7c6"))
            widget.scroll_end(animate=False, immediate=True)

    def _set_sessions_visible(self, visible: bool) -> None:
        self.query_one("#sessions", Vertical).display = visible
        self.query_one("#toggle-sessions", Button).label = "收起会话" if visible else "显示会话"
        if self._welcome_only:
            self.call_after_refresh(self._render_history)
        if not visible:
            self.query_one("#input", Input).focus()

    def action_toggle_sessions(self) -> None:
        if isinstance(self.screen, ConfigScreen):
            raise SkipAction()
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
                           if binding.action != "open_model_switcher"]
            if not bindings:
                self._bindings.key_to_bindings.pop(previous, None)
        self._bindings.bind(shortcut, "open_model_switcher", "切换模型",
                            show=False, priority=True)
        self._installed_model_shortcut = shortcut
        self.refresh_bindings()

    def action_open_model_switcher(self) -> None:
        if isinstance(self.screen, ConfigScreen):
            raise SkipAction()
        if self.busy:
            return
        try:
            self.push_screen(ConfigScreen(config_file=self.config_file, env_file=self.env_file,
                                          quick_switch=True), self._model_switch_closed)
        except (ValueError, OSError) as error:
            self._status(f"无法打开模型选择：{error}")

    def _model_switch_closed(self, saved: bool) -> None:
        if saved:
            model = load_model_settings(self.config_file).default_model
            self._status(f"已切换到 {model}，下次提问生效。")
            self._refresh_key_help()
        self.query_one("#input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle-sessions":
            self.action_toggle_sessions()
            self.query_one("#input", Input).focus()

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
            self.query_one("#input", Input).focus()
        except (ValueError, OSError) as error:
            self._status(f"无法读取会话：{error}")

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.item.id and event.item.id.startswith("session-"):
            await self._open_conversation(event.item.id.removeprefix("session-"))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "input":
            return
        value = event.value
        if value.startswith("/") and " " not in value:
            matches = [command for command in COMMANDS if command.startswith(value)]
            self.query_one("#hints", Static).update("  ".join(matches))
        else:
            self.query_one("#hints", Static).update("")

    def action_complete_command(self) -> None:
        field = self.query_one("#input", Input)
        value = field.value
        if not value.startswith("/") or " " in value:
            return
        matches = [command for command in COMMANDS if command.startswith(value)]
        if matches:
            field.value = matches[0] + (" " if matches[0] in {"/rename", "/resume"} else "")
            field.cursor_position = len(field.value)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        field = self.query_one("#input", Input)
        if self.busy and value != "/exit" and value != "/help":
            self._status("请等待当前回答完成。")
            return
        field.value = ""
        if not value:
            return
        if value.startswith("/"):
            await self._command(value)
            return
        self.busy = True
        self._show_message("user", value)
        self._status("正在思考…")
        self.run_worker(self._ask(value), exclusive=True)

    async def _ask(self, question: str) -> None:
        try:
            history = self.conversation.model_context_messages()
            history_length = len(history)
            answer = await ask_deepseek(
                question, history=history,
                on_stream=self._stream_answer, on_thinking=self._stream_thinking,
            )
            self.conversation.add_exchange(question, answer, model_turn_messages=history[history_length:] or None)
            self._show_message("assistant", answer)
            self._stream_answer("")
            self._stream_thinking("")
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
            self._stream_answer("")
            self._stream_thinking("")
            self.busy = False
            self.query_one("#input", Input).focus()

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
                    self.query_one("#input", Input).value = "/rename "
                    return
                self.conversation.rename(argument)
                self.query_one("#session-name", Label).update(self.conversation.name or "未命名会话")
                await self._refresh_sessions()
                self._status("会话已重命名。")
            elif command == "/resume":
                if not argument:
                    self._status("输入 /resume 会话名，或用 /switch 选择。")
                    self.query_one("#input", Input).value = "/resume "
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
        self.query_one("#input", Input).focus()
