"""Local MCP setup; configuration is edited by the user, never by model tools."""

from dataclasses import asdict
from pathlib import Path

from dotenv import set_key
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, TextArea

from dianstar.tools.mcp.client import MCPTools
from dianstar.config.mcp import MCPSettings, load_mcp_settings, normalize, save_mcp_settings
from dianstar.security.authorization import Authorization


class MCPConfigScreen(ModalScreen[bool]):
    CSS = """
    MCPConfigScreen { align: center middle; background: $background 75%; }
    #mcp-panel { width: 76; max-width: 95%; height: auto; max-height: 90%;
                 border: round $primary; padding: 1 2; background: $surface; }
    #mcp-panel Label, #mcp-panel Static { height: auto; }
    #mcp-panel Checkbox { height: 1; padding: 0; border: none; margin: 1 0 0 0; }
    #mcp-panel Input { height: 1; margin: 0; }
    #mcp-panel TextArea { height: 3; border: solid $foreground 20%; }
    #mcp-actions { height: 3; margin-top: 1; align-horizontal: right; }
    #mcp-actions Button { width: auto; min-width: 8; margin-left: 1; }
    #mcp-info { color: $text-muted; }
    """
    BINDINGS = [Binding("escape", "close", "关闭", priority=True, show=False)]

    def __init__(self, path: Path, env_file: Path):
        super().__init__()
        self.path, self.env_file = path, env_file
        self.changed = False
        self.testing = False
        self.error = ""
        try:
            self.settings = load_mcp_settings(path)
        except ValueError:
            self.settings = MCPSettings()
            self.error = "原 MCP 配置无效；保存将替换该配置。"

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="mcp-panel"):
            yield Label("MCP · Git 工程操作 / GitHub 查询")
            yield Static("仅启动你信任的本地程序。测试连接会启动服务；启用查询后，查询内容会发送给当前模型，凭据不发送。", id="mcp-info")
            yield Checkbox("启用本地 Git 查询", value=self.settings.git_enabled, id="mcp-git-enabled")
            yield Checkbox("允许申请 Git 写操作（每次单独确认）", value=self.settings.git_write_enabled, id="mcp-git-write-enabled")
            yield Label("允许访问的本地仓库：每行一个绝对路径")
            yield TextArea("\n".join(self.settings.git_repositories), id="mcp-git-repos")
            yield Checkbox("启用 GitHub 公开仓库查询", value=self.settings.github_enabled, id="mcp-github-enabled")
            yield Label("GitHub 官方 MCP 程序的绝对路径")
            yield Input(self.settings.github_executable, compact=True, id="mcp-github-exe")
            yield Label("允许查询的公开仓库：每行一个 owner/repo")
            yield TextArea("\n".join(self.settings.github_repositories), id="mcp-github-repos")
            yield Label("GitHub Token：留空保留；填写后保存到 .env")
            yield Input(password=True, compact=True, id="mcp-github-token")
            yield Static("Token 使用 .env 或环境变量中的 GITHUB_PERSONAL_ACCESS_TOKEN。建议最小只读权限。")
            yield Static(self.error, id="mcp-state", markup=False)
            with Horizontal(id="mcp-actions"):
                yield Button("返回", id="mcp-back")
                yield Button("测试连接", id="mcp-test")
                yield Button("保存", variant="primary", id="mcp-save")

    def read_settings(self):
        data = asdict(self.settings)
        data.update(git_enabled=self.query_one("#mcp-git-enabled", Checkbox).value,
                    git_write_enabled=self.query_one("#mcp-git-write-enabled", Checkbox).value,
                    github_enabled=self.query_one("#mcp-github-enabled", Checkbox).value,
                    github_executable=self.query_one("#mcp-github-exe", Input).value.strip(),
                    git_repositories=[line.strip() for line in self.query_one("#mcp-git-repos", TextArea).text.splitlines() if line.strip()],
                    github_repositories=[line.strip() for line in self.query_one("#mcp-github-repos", TextArea).text.splitlines() if line.strip()])
        return normalize(data)

    def action_close(self):
        self.query_one("#mcp-github-token", Input).value = ""
        self.dismiss(self.changed)

    async def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "mcp-back":
            self.action_close()
        elif event.button.id == "mcp-save":
            try:
                settings = self.read_settings()
                token = self.query_one("#mcp-github-token", Input)
                if token.value.strip():
                    self.env_file.parent.mkdir(parents=True, exist_ok=True)
                    set_key(str(self.env_file), "GITHUB_PERSONAL_ACCESS_TOKEN", token.value.strip())
                    token.value = ""
                self.settings = save_mcp_settings(settings, self.path)
                self.changed = True
                self.query_one("#mcp-state", Static).update("已保存，下次提问使用新配置。")
            except (OSError, ValueError):
                self.query_one("#mcp-state", Static).update("保存失败，请检查路径、仓库格式、启用范围及文件权限。")
        elif event.button.id == "mcp-test" and not self.testing:
            if self.query_one("#mcp-github-token", Input).value:
                self.query_one("#mcp-state", Static).update("请先保存 Token，再测试连接。")
                return
            self.testing = True
            self.query_one("#mcp-test", Button).disabled = True
            self.run_worker(self._test(), exclusive=True, group="mcp-test")

    async def _test(self):
        authorization = Authorization()
        try:
            settings = self.read_settings()
            self.query_one("#mcp-state", Static).update("正在测试连接…")
            async with MCPTools(settings, self.env_file, authorization) as tools:
                lines = [f"{server}：{'已连接' if item['state'] == 'connected' else '连接失败'}"
                         + (f"，{item['tools']} 个可用工具" if "tools" in item else "")
                         for server, item in tools.status.items()]
                self.query_one("#mcp-state", Static).update("\n".join(lines) or "尚未启用任何服务。")
        except (OSError, ValueError):
            self.query_one("#mcp-state", Static).update("连接失败，请检查程序路径、仓库配置和凭据。")
        finally:
            authorization.close()
            self.testing = False
            if self.is_attached:
                self.query_one("#mcp-test", Button).disabled = False
