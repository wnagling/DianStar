"""User-only account UI; credentials never enter the chat input or history."""
import asyncio
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from dianstar.app.github_account import GitHubAccount
from dianstar.config.paths import ENV_FILE
from dianstar.platform.github_credentials import GitHubLoginError


class GitHubAccountScreen(ModalScreen[bool]):
    CSS = """
    GitHubAccountScreen { align: center middle; background: $background 35%; }
    #github-account-panel { width: 70; max-width: 96%; height: auto; max-height: 95%;
                            border: round $primary; padding: 0 1; background: $surface; }
    #github-account-title { height: 1; text-style: bold; }
    #github-account-status, #github-account-note { height: auto; margin-bottom: 1; }
    #github-account-note { color: $text-muted; }
    #github-account-token-form { display: none; height: auto; }
    #github-account-token { height: 1; }
    .github-account-buttons { height: 3; align-horizontal: right; }
    .github-account-buttons Button { width: auto; min-width: 8; margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "close", show=False, priority=True)]

    def __init__(self, env_file=ENV_FILE, *, service=None):
        super().__init__()
        self.service = service or GitHubAccount(env_file)
        self.worker = None
        self.busy = False
        self.changed = False
        self.cancel_requested = False
        self.token_field = None

    def compose(self) -> ComposeResult:
        with Vertical(id="github-account-panel"):
            yield Static("GitHub 账号", id="github-account-title")
            yield Static("正在检查账号…", markup=False, id="github-account-status")
            yield Static("登录一次，后续自动连接。登录不改变项目范围和操作审批。\n凭据只保存在系统凭据库，不写入聊天或项目配置。", id="github-account-note")
            with Horizontal(classes="github-account-buttons"):
                yield Button("浏览器登录", variant="primary", id="github-account-browser")
                yield Button("输入令牌", id="github-account-token-mode")
                yield Button("检查连接", id="github-account-check")
                yield Button("退出登录", id="github-account-logout")
            with Vertical(id="github-account-token-form"):
                yield Static("输入在 GitHub 创建的访问令牌；将先验证账号。")
                self.token_field = Input(password=True, compact=True, placeholder="访问令牌（输入隐藏）", id="github-account-token")
                yield self.token_field
                with Horizontal(classes="github-account-buttons"):
                    yield Button("保存并登录", variant="primary", id="github-account-save")
            with Horizontal(classes="github-account-buttons"):
                yield Button("关闭", id="github-account-close")

    def on_mount(self):
        self.start("check")

    def start(self, operation, token=None):
        if self.busy:
            return
        self.busy = True
        self.cancel_requested = False
        for button in self.query(Button):
            if button.id != "github-account-close":
                button.disabled = True
        self.query_one("#github-account-token", Input).value = ""
        self.query_one("#github-account-token-form").display = False
        self.query_one("#github-account-status", Static).update(
            "请在浏览器中完成 GitHub 授权（应用名：Git Credential Manager）。关闭此页面可取消等待。" if operation == "browser" else
            "正在验证并保存账号…" if operation == "token" else "正在退出登录…" if operation == "logout" else "正在检查账号…")
        self.worker = self.run_worker(self.operate(operation, token), exclusive=True)

    async def operate(self, operation, token):
        try:
            if operation == "browser":
                status = await self.service.login()
            elif operation == "token":
                status = await self.service.login(token)
            elif operation == "logout":
                status = await self.service.logout()
            else:
                status = await self.service.check()
            if operation != "check":
                self.changed = True
            if self.is_mounted:
                text = ("账号：" + status.login + "\n" if status.login else "") + status.message
                self.query_one("#github-account-status", Static).update(text)
        except asyncio.CancelledError:
            raise
        except (ValueError, OSError) as error:
            if self.is_mounted:
                self.query_one("#github-account-status", Static).update(
                    str(error) if isinstance(error, GitHubLoginError) else "账号操作未完成，请检查网络或系统凭据库后重试。")
        finally:
            self.busy = False
            if self.is_mounted:
                self.query_one("#github-account-token", Input).value = ""
                for button in self.query(Button):
                    button.disabled = False

    def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "github-account-token":
            event.stop()
            self.save_token()

    def save_token(self):
        token = self.query_one("#github-account-token", Input).value.strip()
        if not token:
            self.query_one("#github-account-status", Static).update("请输入访问令牌。")
            return
        self.start("token", token)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "github-account-close":
            self.action_close()
        elif event.button.id == "github-account-browser":
            self.start("browser")
        elif event.button.id == "github-account-check":
            self.start("check")
        elif event.button.id == "github-account-logout":
            self.start("logout")
        elif event.button.id == "github-account-save":
            self.save_token()
        elif event.button.id == "github-account-token-mode":
            self.query_one("#github-account-token-form").display = True
            self.query_one("#github-account-token", Input).focus()

    def action_close(self):
        self.query_one("#github-account-token", Input).value = ""
        if self.worker and not self.cancel_requested:
            self.cancel_requested = True
            self.worker.cancel()
        self.dismiss(self.changed)

    def on_unmount(self):
        if self.token_field is not None:
            self.token_field.value = ""
        if self.worker and not self.cancel_requested:
            self.cancel_requested = True
            self.worker.cancel()
