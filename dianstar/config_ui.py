"""TUI 配置菜单：键盘选择配置项，再进入各项编辑界面。"""

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, ListItem, ListView, Select, Static

from .config import (CONFIG_FILE, ENV_FILE, api_key_configured, create_key_profile,
                     delete_key_profile, load_model_settings, read_api_key, save_model_settings, save_switch_model_shortcut,
                     set_default_key, update_key_profile, validate_shortcut)

class ConfigScreen(ModalScreen[bool]):
    """Keep the API Key masked and outside model configuration or chat history."""

    CSS = """
    ConfigScreen { align: center middle; background: $background 75%; }
    #config-panel { width: 68; max-width: 95%; height: auto; max-height: 90%;
                    padding: 1 2; border: round $primary; background: $surface; }
    #config-title { height: 2; text-style: bold; }
    #config-menu { height: 12; }
    #config-menu ListItem, #default-list ListItem, #key-list ListItem,
    #model-list ListItem { height: 2; }
    #config-hint, #config-status, #detail-hint { height: auto; color: $text-muted; }
    #config-status { color: $error; }
    .config-detail { display: none; height: auto; }
    .config-detail Label { height: 1; }
    .config-detail Input, .config-detail Select { width: 100%; }
    #shortcut-row { height: 1; }
    #shortcut-row Label { width: 14; }
    #shortcut-row Input { width: 1fr; }
    #model-key-row, #default-key-row { height: 1; }
    #model-key-row Label, #default-key-row Label { width: 12; }
    #model-key-row Select, #default-key-row Select { width: 1fr; }
    #model-list { height: 8; }
    #default-list, #key-list { height: auto; max-height: 10; }
    .detail-buttons { height: 3; margin-top: 1; align-horizontal: right; }
    .detail-buttons Button { margin-left: 1; }
    #key-edit-page .detail-buttons Button { width: auto; min-width: 8; }
    #key-edit-page #key-delete { min-width: 14; }
    #models-page .detail-buttons Button { width: auto; min-width: 8; }
    #key-input-row { height: 1; }
    #key-input-row Input { width: 1fr; }
    #key-visibility { width: 8; min-width: 8; height: 1; border: none; margin-left: 1; }
    """
    BINDINGS = [Binding("escape", "back", "返回", priority=True, show=False)]

    def __init__(self, *, config_file: Path = CONFIG_FILE, env_file: Path = ENV_FILE,
                 quick_switch: bool = False) -> None:
        super().__init__()
        self.config_file = config_file
        self.env_file = env_file
        self.settings = load_model_settings(config_file)
        self.page = "menu"
        self.changed = False
        self.quick_switch = quick_switch
        self.editing_key_id: str | None = None
        self.editing_model: tuple[str, str] | None = None
        self.confirm_key_delete = False
        self.selected_model_key_id = self.settings.default_key_id
        self.selected_default_key_id = self.settings.default_key_id

    def compose(self) -> ComposeResult:
        with Vertical(id="config-panel"):
            yield Static("配置", id="config-title")
            with Vertical(id="menu-page"):
                with ListView(id="config-menu"):
                    yield ListItem(Static("", id="menu-key-label", markup=False), id="menu-key")
                    yield ListItem(Static("", id="menu-models-label", markup=False),
                                   id="menu-models")
                    yield ListItem(Static("", id="menu-default-label", markup=False),
                                   id="menu-default")
                    yield ListItem(Static("▣  快捷键    ·    1 项功能"),
                                   id="menu-shortcuts")
                yield Static("↑/↓ 选择  ·  Enter 进入  ·  Esc 关闭", id="config-hint")

            with Vertical(classes="config-detail", id="key-page"):
                yield Label("▣  api_key · 选择或新增 Key")
                yield ListView(id="key-list")
                yield Static("↑/↓ 选择，Enter 编辑；末项可新增 Key。", id="detail-hint")
                with Horizontal(classes="detail-buttons"):
                    yield Button("返回", id="key-back")
                    yield Button("编辑选中 Key", variant="primary", id="key-edit")

            with Vertical(classes="config-detail", id="key-edit-page"):
                yield Static("编辑 Key", id="key-edit-title")
                yield Label("▣  Key 名称")
                yield Input(placeholder="例如：工作 Key", compact=True, id="key-name")
                yield Label("API Key")
                with Horizontal(id="key-input-row"):
                    yield Input(placeholder="输入 API Key", password=True,
                                compact=True, id="config-key")
                    yield Button("显示", id="key-visibility")
                yield Static("密钥默认遮挡，可显示或直接编辑；保存后生效。", id="key-state")
                with Horizontal(classes="detail-buttons"):
                    yield Button("返回", id="key-edit-back")
                    yield Button("删除 Key", variant="error", id="key-delete")
                    yield Button("保存", id="key-save")
                    yield Button("保存并设为默认", variant="primary", id="key-save-default")

            with Vertical(classes="config-detail", id="models-page"):
                yield Label("▣  model · 可用模型列表")
                with Horizontal(id="model-key-row"):
                    yield Label("所属 Key")
                    yield Select([(item.name, item.id) for item in self.settings.profiles],
                                 value=self.settings.default_key_id, allow_blank=False,
                                 compact=True, id="model-key-select")
                yield Static("选中后按 Enter 编辑；下方输入新模型 ID 可添加。",
                             classes="config-hint")
                yield ListView(id="model-list")
                yield Input(placeholder="输入要添加的模型 ID", compact=True, id="model-add-input")
                with Horizontal(classes="detail-buttons"):
                    yield Button("返回", id="models-back")
                    yield Button("编辑选中模型", id="model-edit")
                    yield Button("删除选中模型", variant="error", id="model-delete")
                    yield Button("添加模型", variant="primary", id="model-add")

            with Vertical(classes="config-detail", id="model-edit-page"):
                yield Label("▣  编辑模型 ID")
                yield Input(compact=True, id="model-edit-input")
                yield Static("修改默认模型的 ID 后，默认选择会同步更新。", classes="config-hint")
                with Horizontal(classes="detail-buttons"):
                    yield Button("取消", id="model-edit-cancel")
                    yield Button("保存修改", variant="primary", id="model-edit-save")

            with Vertical(classes="config-detail", id="default-page"):
                yield Label("▣  default model · 默认模型")
                with Horizontal(id="default-key-row"):
                    yield Label("所属 Key")
                    yield Select([(item.name, item.id) for item in self.settings.profiles],
                                 value=self.settings.default_key_id, allow_blank=False,
                                 compact=True, id="default-key-select")
                yield Static("↑/↓ 选择模型，Enter 保存；Shift+Tab 可选 Key。",
                             classes="config-hint", id="default-hint")
                yield ListView(id="default-list")
                with Horizontal(classes="detail-buttons"):
                    yield Button("返回", id="default-back")

            with Vertical(classes="config-detail", id="shortcuts-page"):
                yield Label("▣  快捷键")
                with Horizontal(id="shortcut-row"):
                    yield Label("模型切换")
                    yield Input(value=self.settings.switch_model_shortcut.upper(), compact=True,
                                placeholder="例如 Ctrl+B", id="shortcut-model-input")
                yield Static("在方框中输入快捷键，按 Enter 保存。", classes="config-hint")
                with Horizontal(classes="detail-buttons"):
                    yield Button("返回", id="shortcuts-back")
                    yield Button("保存快捷键", variant="primary", id="shortcuts-save")

            yield Static("", id="config-status")

    async def on_mount(self) -> None:
        menu = self.query_one("#config-menu", ListView)
        menu.index = 0
        menu.focus()
        self._refresh_menu()
        await self._refresh_key_list()
        await self._refresh_model_list()
        await self._refresh_default_list()
        if self.quick_switch:
            self._show_page("default")

    def _refresh_menu(self) -> None:
        active = self.settings.active_profile
        self.query_one("#menu-key-label", Static).update(
            f"▣  api_key    ·    {len(self.settings.profiles)} 个（默认：{active.name}）")
        self.query_one("#menu-models-label", Static).update(
            f"▣  model      ·    {active.name}：{len(active.models)} 个模型")
        self.query_one("#menu-default-label", Static).update(
            f"▣  default model    ·    {active.default_model or '未设置'}")

    async def _refresh_key_list(self) -> None:
        view = self.query_one("#key-list", ListView)
        await view.remove_children()
        await view.mount(*[
            ListItem(Static(f"{'●' if item.id == self.settings.default_key_id else '○'}  "
                            f"{item.name}  ·  {len(item.models)} 个模型  ·  "
                            f"{'已配置' if api_key_configured(self.env_file, key_id=item.id) else '未配置'}",
                            markup=False),
                     id=f"key-profile-{item.id}")
            for item in self.settings.profiles
        ], ListItem(Static("＋  添加 Key"), id="key-add"))
        view.index = next(i for i, item in enumerate(self.settings.profiles)
                          if item.id == self.settings.default_key_id)

    async def _refresh_default_list(self) -> None:
        view = self.query_one("#default-list", ListView)
        await view.remove_children()
        profile = self.settings.profile(self.selected_default_key_id)
        self.query_one("#default-hint", Static).update(
            "↑/↓ 选择模型，Enter 保存；Shift+Tab 可选 Key。" if profile.models
            else "该 Key 尚无模型，请先到 model 添加。")
        await view.mount(*[
            ListItem(Static(f"{'●' if model == profile.default_model else '○'}  {model}"),
                     id=f"model-option-{index}")
            for index, model in enumerate(profile.models)
        ])
        view.index = profile.models.index(profile.default_model) if profile.models else None

    async def _refresh_model_list(self) -> None:
        view = self.query_one("#model-list", ListView)
        await view.remove_children()
        profile = self.settings.profile(self.selected_model_key_id)
        await view.mount(*[
            ListItem(Static(f"{'●' if model == profile.default_model else '○'}  {model}",
                            markup=False), id=f"configured-model-{index}")
            for index, model in enumerate(profile.models)
        ])
        view.index = 0 if profile.models else None

    def _refresh_key_selects(self) -> None:
        options = [(item.name, item.id) for item in self.settings.profiles]
        self.query_one("#model-key-select", Select).set_options(options)
        self.query_one("#default-key-select", Select).set_options(options)
        self.query_one("#model-key-select", Select).value = self.selected_model_key_id
        self.query_one("#default-key-select", Select).value = self.selected_default_key_id

    def _show_page(self, page: str) -> None:
        if page != "key-edit":
            self.query_one("#config-key", Input).value = ""
            self.query_one("#config-key", Input).password = True
            self.query_one("#key-visibility", Button).label = "显示"
        self.page = page
        for name in ("menu", "key", "key-edit", "models", "model-edit", "default", "shortcuts"):
            self.query_one(f"#{name}-page", Vertical).display = name == page
        self.query_one("#config-status", Static).update("")
        focus = {"menu": "#config-menu", "key": "#key-list", "key-edit": "#key-name",
                 "models": "#model-add-input",
                 "model-edit": "#model-edit-input",
                 "default": "#default-list",
                 "shortcuts": "#shortcut-model-input"}[page]
        if page == "default" and self.quick_switch:
            self.query_one("#default-key-row", Horizontal).display = False
        self.query_one(focus).focus()

    def _show_key_editor(self, key_id: str | None) -> None:
        self.editing_key_id = key_id
        self.query_one("#key-edit-title", Static).update("编辑 Key" if key_id else "添加 Key")
        self.confirm_key_delete = False
        self.query_one("#key-delete", Button).label = "删除 Key"
        self.query_one("#key-delete", Button).display = key_id is not None
        self.query_one("#key-name", Input).value = (
            self.settings.profile(key_id).name if key_id else f"Key {len(self.settings.profiles) + 1}")
        secret = self.query_one("#config-key", Input)
        secret.password = True
        self.query_one("#key-visibility", Button).label = "显示"
        try:
            secret.value = read_api_key(self.env_file, key_id=key_id) if key_id else ""
        except OSError:
            secret.value = ""
            self._show_page("key-edit")
            self.query_one("#config-status", Static).update("读取 API Key 失败，请检查文件权限。")
            return
        self.query_one("#key-state", Static).update(
            "已加载密钥，默认遮挡；可直接编辑，保存后生效。" if key_id
            else "请输入新 Key；默认遮挡，可点击显示查看。")
        self._show_page("key-edit")

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "config-menu" and event.item.id:
            page = {"menu-key": "key", "menu-models": "models",
                    "menu-default": "default", "menu-shortcuts": "shortcuts"}.get(event.item.id)
            if page:
                self._show_page(page)
        elif event.list_view.id == "default-list" and event.item.id:
            index = int(event.item.id.removeprefix("model-option-"))
            profile = self.settings.profile(self.selected_default_key_id)
            await self._save_default(profile.models[index])
        elif event.list_view.id == "key-list" and event.item.id:
            self._show_key_editor(None if event.item.id == "key-add" else
                                  event.item.id.removeprefix("key-profile-"))
        elif event.list_view.id == "model-list":
            self._show_model_editor()

    def _edit_selected_key(self) -> None:
        view = self.query_one("#key-list", ListView)
        if view.index is not None:
            self._show_key_editor(self.settings.profiles[view.index].id
                                  if view.index < len(self.settings.profiles) else None)

    def _show_model_editor(self) -> None:
        view = self.query_one("#model-list", ListView)
        profile = self.settings.profile(self.selected_model_key_id)
        if view.index is None or view.index >= len(profile.models):
            self.query_one("#config-status", Static).update("请先选择要编辑的模型。")
            return
        model = profile.models[view.index]
        self.editing_model = (profile.id, model)
        self.query_one("#model-edit-input", Input).value = model
        self._show_page("model-edit")

    def _cancel_model_edit(self) -> None:
        self.editing_model = None
        self._show_page("models")
        self.query_one("#model-list", ListView).focus()

    async def on_select_changed(self, event: Select.Changed) -> None:
        if event.value is Select.NULL:
            return
        if event.select.id == "model-key-select":
            self.selected_model_key_id = str(event.value)
            await self._refresh_model_list()
        elif event.select.id == "default-key-select":
            self.selected_default_key_id = str(event.value)
            await self._refresh_default_list()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id in {"key-name", "config-key"}:
            await self._save_key(make_default=False)
        elif event.input.id == "model-add-input":
            await self._add_model()
        elif event.input.id == "model-edit-input":
            await self._save_model_edit()
        elif event.input.id == "shortcut-model-input":
            await self._save_shortcut()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id in {"key-back", "models-back", "default-back", "shortcuts-back"}:
            self._show_page("menu")
        elif event.button.id == "key-edit-back":
            self._show_page("key")
        elif event.button.id == "key-edit":
            self._edit_selected_key()
        elif event.button.id == "key-visibility":
            secret = self.query_one("#config-key", Input)
            secret.password = not secret.password
            event.button.label = "显示" if secret.password else "隐藏"
        elif event.button.id == "model-edit":
            self._show_model_editor()
        elif event.button.id == "model-edit-cancel":
            self._cancel_model_edit()
        elif event.button.id == "model-edit-save":
            await self._save_model_edit()
        elif event.button.id == "shortcuts-save":
            await self._save_shortcut()
        elif event.button.id == "key-save":
            await self._save_key(make_default=False)
        elif event.button.id == "key-save-default":
            await self._save_key(make_default=True)
        elif event.button.id == "key-delete":
            await self._delete_key()
        elif event.button.id == "model-add":
            await self._add_model()
        elif event.button.id == "model-delete":
            await self._delete_model()

    async def _save_key(self, *, make_default: bool) -> None:
        name = self.query_one("#key-name", Input).value.strip()
        key = self.query_one("#config-key", Input).value
        if not key.strip():
            self.query_one("#config-status", Static).update("请输入 API Key。")
            return
        try:
            if self.editing_key_id is None:
                self.settings = create_key_profile(name, key, path=self.config_file,
                                                   env_file=self.env_file)
                new_id = self.settings.profiles[-1].id
                if make_default and self.settings.default_key_id != new_id:
                    self.settings = set_default_key(new_id, self.config_file)
            else:
                self.settings = update_key_profile(self.editing_key_id, name, key,
                                                   path=self.config_file, env_file=self.env_file,
                                                   make_default=make_default)
        except ValueError as error:
            self.query_one("#config-status", Static).update(str(error))
            return
        except OSError:
            self.query_one("#config-status", Static).update("保存 API Key 失败，请检查文件权限。")
            return
        self.query_one("#config-key", Input).value = ""
        self.selected_model_key_id = (new_id if self.editing_key_id is None
                                      else self.editing_key_id)
        self.selected_default_key_id = self.settings.default_key_id
        self._refresh_key_selects()
        self._refresh_menu()
        await self._refresh_key_list()
        await self._refresh_model_list()
        await self._refresh_default_list()
        self.changed = True
        self._show_page("key")

    async def _save_model_edit(self) -> None:
        if self.editing_model is None:
            return
        key_id, original = self.editing_model
        model = self.query_one("#model-edit-input", Input).value.strip()
        if not model:
            self.query_one("#config-status", Static).update("请输入模型 ID。")
            return
        profile = self.settings.profile(key_id)
        models = [model if item == original else item for item in profile.models]
        default = model if profile.default_model == original else profile.default_model
        try:
            self.settings = save_model_settings(models, default, self.config_file, key_id=key_id)
        except ValueError as error:
            self.query_one("#config-status", Static).update(str(error))
            return
        except OSError:
            self.query_one("#config-status", Static).update("保存模型失败，请检查文件权限。")
            return
        self.editing_model = None
        await self._models_changed()
        self._show_page("models")
        self.query_one("#model-list", ListView).index = models.index(model)
        self.query_one("#model-list", ListView).focus()

    async def _add_model(self) -> None:
        model = self.query_one("#model-add-input", Input).value.strip()
        if not model:
            self.query_one("#config-status", Static).update("请输入模型 ID。")
            return
        profile = self.settings.profile(self.selected_model_key_id)
        try:
            self.settings = save_model_settings([*profile.models, model],
                                                profile.default_model or model,
                                                self.config_file, key_id=profile.id)
        except ValueError as error:
            self.query_one("#config-status", Static).update(str(error))
            return
        except OSError:
            self.query_one("#config-status", Static).update("添加模型失败，请检查文件权限。")
            return
        self.query_one("#model-add-input", Input).value = ""
        await self._models_changed()
        self.query_one("#model-add-input", Input).focus()

    async def _delete_model(self) -> None:
        view = self.query_one("#model-list", ListView)
        profile = self.settings.profile(self.selected_model_key_id)
        if view.index is None or view.index >= len(profile.models):
            self.query_one("#config-status", Static).update("请先选择要删除的模型。")
            return
        models = [model for index, model in enumerate(profile.models) if index != view.index]
        default = profile.default_model if profile.default_model in models else (models[0] if models else "")
        try:
            self.settings = save_model_settings(models, default, self.config_file,
                                                key_id=profile.id)
        except (OSError, ValueError) as error:
            self.query_one("#config-status", Static).update(f"删除模型失败：{error}")
            return
        await self._models_changed()
        self.query_one("#model-list", ListView).focus()

    async def _models_changed(self) -> None:
        self._refresh_menu()
        await self._refresh_key_list()
        await self._refresh_model_list()
        await self._refresh_default_list()
        self.changed = True
        self.query_one("#config-status", Static).update("")

    async def _delete_key(self) -> None:
        if self.editing_key_id is None:
            return
        if not self.confirm_key_delete:
            self.confirm_key_delete = True
            self.query_one("#key-delete", Button).label = "确认删除 Key"
            self.query_one("#config-status", Static).update("再次点击确认删除此 Key 及其模型和本地密钥。")
            return
        try:
            self.settings = delete_key_profile(self.editing_key_id,
                                               path=self.config_file, env_file=self.env_file)
        except (ValueError, OSError) as error:
            self.query_one("#config-status", Static).update(str(error))
            self.confirm_key_delete = False
            self.query_one("#key-delete", Button).label = "删除 Key"
            return
        self.editing_key_id = None
        self.selected_model_key_id = self.settings.default_key_id
        self.selected_default_key_id = self.settings.default_key_id
        self._refresh_key_selects()
        self._refresh_menu()
        await self._refresh_key_list()
        await self._refresh_model_list()
        await self._refresh_default_list()
        self.changed = True
        self._show_page("key")

    async def _save_default(self, model: str) -> None:
        try:
            profile = self.settings.profile(self.selected_default_key_id)
            self.settings = save_model_settings(list(profile.models), model, self.config_file,
                                                key_id=profile.id)
        except OSError:
            self.query_one("#config-status", Static).update("保存默认模型失败，请检查文件权限。")
            return
        self._refresh_menu()
        await self._refresh_default_list()
        self.changed = True
        if self.quick_switch:
            self.dismiss(True)
        else:
            self._show_page("menu")

    async def _save_shortcut(self) -> None:
        value = self.query_one("#shortcut-model-input", Input).value
        try:
            shortcut = validate_shortcut(value)
        except ValueError as error:
            self.query_one("#config-status", Static).update(str(error))
            return
        try:
            self.settings = save_switch_model_shortcut(shortcut, self.config_file)
        except OSError:
            self.query_one("#config-status", Static).update("保存快捷键失败，请检查文件权限。")
            return
        self.query_one("#shortcut-model-input", Input).value = shortcut.upper()
        self.changed = True
        self._show_page("menu")

    def action_back(self) -> None:
        if self.page == "menu" or self.quick_switch:
            self.dismiss(self.changed)
        elif self.page == "key-edit":
            self._show_page("key")
        elif self.page == "model-edit":
            self._cancel_model_edit()
        else:
            self._show_page("menu")
