"""配置保存和 TUI 密钥遮挡测试，所有密钥均为临时假值。"""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dotenv import dotenv_values
from textual.widgets import Button, Input, ListView, Select, Static
from rich.cells import cell_len

from dianstar.config.models import create_key_profile, cycle_model, delete_key_profile, load_model_settings, save_model_settings, set_default_key, validate_shortcut
from dianstar.config.credentials import save_api_key
from dianstar.app.runtime import run_turn
from dianstar.ui.tui.screens.config import ConfigScreen
from dianstar.sessions.conversations import Conversation
from dianstar.ui.tui.widgets import ChatInput
from dianstar.ui.tui.app import DianStarApp


class ConfigTests(unittest.TestCase):
    def test_cycle_models_across_keys_keeps_duplicates_and_wraps(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, env = root / '.local/config.json', root / '.env'
            save_api_key('fake-primary', env)
            create_key_profile('第二个 Key', 'fake-second', path=config, env_file=env)
            save_model_settings(['deepseek-flash'], 'deepseek-flash', config, key_id='key2')
            create_key_profile('空列表', 'fake-empty', path=config, env_file=env)
            create_key_profile('未设置密钥', 'your-key-here', path=config, env_file=env)
            save_model_settings(['skip-me'], 'skip-me', config, key_id='key4')
            pairs = []
            for _ in range(3):
                settings = cycle_model(config, env_file=env)
                pairs.append((settings.default_key_id, settings.default_model))
            self.assertEqual(pairs, [('primary', 'deepseek-v4-pro'),
                                     ('key2', 'deepseek-flash'), ('primary', 'deepseek-flash')])
            self.assertEqual(load_model_settings(config), settings)
            self.assertNotIn('fake-', config.read_text(encoding='utf-8'))

    def test_cycle_handles_no_choice_and_single_choice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, env = root / 'config.json', root / '.env'
            with self.assertRaisesRegex(ValueError, '没有可切换'):
                cycle_model(config, env_file=env)
            self.assertFalse(config.exists())
            save_api_key('fake-primary', env)
            save_model_settings(['only-model'], 'only-model', config)
            before = config.read_bytes()
            settings = cycle_model(config, env_file=env)
            self.assertEqual(settings.default_model, 'only-model')
            self.assertEqual(config.read_bytes(), before)

    def test_first_added_key_becomes_default_when_legacy_key_is_unset(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = create_key_profile("新 Key", "new-fake-key",
                                          path=root / ".local" / "config.json",
                                          env_file=root / ".env")
            self.assertEqual(settings.default_key_id, "key2")
            self.assertEqual(settings.default_model, "")
            self.assertEqual(settings.profile("key2").models, ())

    def test_delete_key_removes_credential_and_reassigns_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / ".local" / "config.json"
            env_file = root / ".env"
            save_api_key("first-fake-key", env_file)
            settings = create_key_profile("第二个 Key", "second-fake-key",
                                          path=config_file, env_file=env_file)
            self.assertEqual(settings.profile("key2").models, ())
            save_model_settings(["second-model"], "second-model", config_file, key_id="key2")
            set_default_key("key2", config_file)
            settings = delete_key_profile("key2", path=config_file, env_file=env_file)
            self.assertEqual(settings.default_key_id, "primary")
            self.assertEqual(len(settings.profiles), 1)
            self.assertIsNone(dotenv_values(env_file).get("DEEPSEEK_API_KEY_KEY2"))
            self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY"], "first-fake-key")
            settings = delete_key_profile("primary", path=config_file, env_file=env_file)
            self.assertEqual(settings.default_model, "")
            self.assertEqual(settings.profile("primary").models, ())
            self.assertIsNone(dotenv_values(env_file).get("DEEPSEEK_API_KEY"))

    def test_empty_default_key_requires_model_before_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            create_key_profile("新 Key", "new-fake-key", path=root / ".local" / "config.json",
                               env_file=root / ".env")
            with self.assertRaisesRegex(ValueError, "尚未添加模型"):
                asyncio.run(run_turn("你好", env_file=root / ".env"))

    def test_multiple_keys_keep_separate_models_and_migrate_old_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / ".local" / "config.json"
            config_file.parent.mkdir()
            config_file.write_text('{"models":["deepseek-flash"],"default_model":"deepseek-flash"}',
                                   encoding="utf-8")
            env_file = root / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=legacy-fake-key\n", encoding="utf-8")
            settings = create_key_profile("第二个 Key", "second-fake-key",
                                          path=config_file, env_file=env_file)
            self.assertEqual(settings.default_key_id, "primary")
            self.assertEqual(settings.profile("primary").models, ("deepseek-flash",))
            settings = save_model_settings(["deepseek-v4-pro", "another-model", "third-model"],
                                           "another-model", config_file, key_id="key2")
            self.assertEqual(settings.profile("primary").models, ("deepseek-flash",))
            self.assertEqual(settings.profile("key2").models,
                             ("deepseek-v4-pro", "another-model", "third-model"))
            settings = set_default_key("key2", config_file)
            self.assertEqual(settings.default_model, "another-model")
            self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY"], "legacy-fake-key")
            self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY_KEY2"], "second-fake-key")
            contents = config_file.read_text(encoding="utf-8")
            self.assertNotIn("fake-key", contents)
            self.assertIn('"profiles"', contents)

    def test_shortcut_accepts_captured_combinations_and_rejects_conflicts(self):
        self.assertEqual(validate_shortcut("Alt + M"), "alt+m")
        self.assertEqual(validate_shortcut("F5"), "f5")
        for shortcut in ("ctrl+l", "ctrl+q", "escape", "a"):
            with self.subTest(shortcut=shortcut), self.assertRaises(ValueError):
                validate_shortcut(shortcut)

    def test_initial_models_include_v4_pro(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = load_model_settings(Path(temporary) / "missing.json")
            self.assertEqual(settings.models, ("deepseek-flash", "deepseek-v4-pro"))
            self.assertEqual(settings.default_model, "deepseek-flash")
            self.assertEqual(settings.switch_model_shortcut, "ctrl+b")

    def test_models_and_key_use_separate_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / ".local" / "config.json"
            env_file = root / ".env"
            env_file.write_text("OTHER_SETTING=keep\nDEEPSEEK_API_KEY=old-fake-key\n", encoding="utf-8")
            save_model_settings(["deepseek-flash", "deepseek-chat"], "deepseek-chat", config_file)
            save_api_key("new-fake-key", env_file)
            self.assertEqual(load_model_settings(config_file).default_model, "deepseek-chat")
            self.assertEqual(dotenv_values(env_file)["OTHER_SETTING"], "keep")
            self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY"], "new-fake-key")
            self.assertNotIn("fake-key", config_file.read_text(encoding="utf-8"))
            with self.assertRaises(ValueError):
                save_model_settings(["deepseek-flash", "deepseek-flash"], "deepseek-flash", config_file)


class ConfigTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_shortcut_cycles_same_model_on_two_keys_without_dialog_or_draft_loss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, env = root / '.local/config.json', root / '.env'
            save_api_key('fake-primary', env)
            save_model_settings(['deepseek-flash'], 'deepseek-flash', config)
            create_key_profile('工作 Key', 'fake-second', path=config, env_file=env)
            save_model_settings(['deepseek-flash'], 'deepseek-flash', config, key_id='key2')
            app = DianStarApp(Conversation.create(root / 'conversations'), config_file=config, env_file=env)
            async with app.run_test() as pilot:
                field = app.query_one('#input', ChatInput)
                field.text = '未发送\n草稿'
                await pilot.press('ctrl+b')
                self.assertNotIsInstance(app.screen, ConfigScreen)
                self.assertEqual(load_model_settings(config).default_key_id, 'key2')
                footer = str(app.query_one('#key-help', Static).render())
                self.assertIn('deepseek-flash', footer)
                self.assertNotIn('工作 Key', footer)
                self.assertEqual(field.text, '未发送\n草稿')
                app.busy = True
                await pilot.press('ctrl+b')
                self.assertEqual(load_model_settings(config).default_key_id, 'key2')
                app.busy = False
                await pilot.press('ctrl+b')
                self.assertEqual(load_model_settings(config).default_key_id, 'primary')
                self.assertEqual(app.conversation.messages, [])

    @patch('dianstar.providers.deepseek.AsyncOpenAI')
    async def test_cycled_key_and_model_both_reach_next_sdk_request(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, env = root / '.local/config.json', root / '.env'
            save_api_key('fake-primary', env)
            save_model_settings(['deepseek-flash'], 'deepseek-flash', config)
            create_key_profile('工作 Key', 'fake-second', path=config, env_file=env)
            save_model_settings(['deepseek-flash'], 'deepseek-flash', config, key_id='key2')
            cycle_model(config, env_file=env)
            client_class.return_value.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='你好'))]))
            await run_turn('你好', env_file=env)
            self.assertEqual(client_class.call_args.kwargs['api_key'], 'fake-second')
            self.assertEqual(client_class.return_value.chat.completions.create.await_args.kwargs['model'], 'deepseek-flash')

    async def test_key_editor_loads_masks_toggles_and_cancels_without_saving(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file, env_file = root / "config.json", root / ".env"
            save_api_key("first-fake-key", env_file)
            create_key_profile("第二个 Key", "second-fake-key", path=config_file, env_file=env_file)
            app = DianStarApp(Conversation.create(root / "conversations"),
                              config_file=config_file, env_file=env_file)
            async with app.run_test(size=(100, 35)) as pilot:
                app.query_one("#input", ChatInput).text = "/config"
                await pilot.press("enter", "enter", "enter")
                screen = app.screen
                key = screen.query_one("#config-key", Input)
                toggle = screen.query_one("#key-visibility", Button)
                self.assertEqual(key.value, "first-fake-key")
                self.assertTrue(key.password)
                self.assertNotIn(key.value, str(key.render()))
                self.assertLessEqual(key.region.right, toggle.region.x)
                self.assertLessEqual(toggle.region.right, toggle.parent.content_region.right)
                await pilot.click("#key-visibility")
                await pilot.pause()
                self.assertFalse(key.password)
                self.assertEqual(str(toggle.label), "隐藏")
                await pilot.pause(0.3)  # 两次独立单击，避免被 Textual 识别为双击。
                await pilot.click("#key-visibility")
                await pilot.pause()
                self.assertTrue(key.password)
                self.assertEqual(str(toggle.label), "显示")
                key.value = ""
                key.focus()
                await pilot.press("enter")
                self.assertEqual(screen.page, "key-edit")
                self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY"], "first-fake-key")
                key.value = "unsaved-fake-key"
                await pilot.press("escape")
                self.assertEqual(key.value, "")
                await pilot.press("down", "enter")
                self.assertEqual(key.value, "second-fake-key")
                self.assertTrue(key.password)
                await pilot.click("#key-visibility")
                await pilot.pause()
                await pilot.click("#key-edit-back")
                await pilot.pause()
                self.assertEqual(key.value, "")
                self.assertTrue(key.password)
                screen.query_one("#key-list", ListView).index = len(screen.settings.profiles)
                await pilot.press("enter")
                self.assertEqual(key.value, "")
                self.assertTrue(key.password)
                self.assertEqual(str(toggle.label), "显示")
                self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY"], "first-fake-key")
                self.assertEqual(app.conversation.messages, [])

    async def test_edit_model_validates_and_preserves_default_and_other_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file, env_file = root / "config.json", root / ".env"
            save_api_key("fake-key", env_file)
            create_key_profile("第二个 Key", "other-fake-key", path=config_file, env_file=env_file)
            save_model_settings(["other-model"], "other-model", config_file, key_id="key2")
            app = DianStarApp(Conversation.create(root / "conversations"),
                              config_file=config_file, env_file=env_file)
            async with app.run_test(size=(100, 35)) as pilot:
                app.query_one("#input", ChatInput).text = "/config"
                await pilot.press("enter", "down", "enter")
                screen = app.screen
                for button in screen.query("#models-page Button"):
                    self.assertGreaterEqual(button.content_size.width, cell_len(str(button.label)))
                    self.assertLessEqual(button.region.right, button.parent.content_region.right)
                await pilot.click("#model-edit")
                self.assertEqual(screen.page, "model-edit")
                field = screen.query_one("#model-edit-input", Input)
                self.assertEqual(field.value, "deepseek-flash")
                for invalid in ("", "deepseek-v4-pro"):
                    field.value = invalid
                    await pilot.press("enter")
                    self.assertEqual(screen.page, "model-edit")
                    self.assertEqual(load_model_settings(config_file).default_model, "deepseek-flash")
                field.value = "renamed-model"
                await pilot.pause()
                await pilot.click("#model-edit-save")
                await pilot.pause()
                self.assertEqual(screen.page, "models")
                settings = load_model_settings(config_file)
                self.assertEqual(settings.models, ("renamed-model", "deepseek-v4-pro"))
                self.assertEqual(settings.default_model, "renamed-model")
                self.assertEqual(settings.profile("key2").models, ("other-model",))
                await pilot.press("down", "enter")
                self.assertEqual(screen.page, "model-edit")
                field.value = "unsaved-model"
                await pilot.press("escape")
                self.assertEqual(screen.page, "models")
                self.assertEqual(load_model_settings(config_file).models, settings.models)
                await pilot.press("enter")
                field.value = "renamed-secondary"
                await pilot.press("enter")
                self.assertEqual(load_model_settings(config_file).default_model, "renamed-model")
                self.assertEqual(load_model_settings(config_file).models,
                                 ("renamed-model", "renamed-secondary"))

    async def test_edit_key_name_preserves_secret_and_allows_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file, env_file = root / "config.json", root / ".env"
            save_api_key("old-fake-key", env_file)
            app = DianStarApp(Conversation.create(root / "conversations"),
                              config_file=config_file, env_file=env_file)
            async with app.run_test() as pilot:
                app.query_one("#input", ChatInput).text = "/config"
                await pilot.press("enter", "enter")
                await pilot.pause()
                await pilot.click("#key-edit")
                await pilot.pause()
                screen = app.screen
                self.assertEqual(screen.page, "key-edit")
                self.assertEqual(str(screen.query_one("#key-edit-title", Static).render()), "编辑 Key")
                screen.query_one("#key-name", Input).value = "工作 Key"
                key = screen.query_one("#config-key", Input)
                self.assertTrue(key.password)
                self.assertEqual(key.value, "old-fake-key")
                await pilot.click("#key-save")
                await pilot.pause()
                self.assertEqual(load_model_settings(config_file).active_profile.name, "工作 Key")
                self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY"], "old-fake-key")
                await pilot.click("#key-edit")
                await pilot.pause()
                key.value = "new-fake-key"
                await pilot.click("#key-save")
                await pilot.pause()
                self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY"], "new-fake-key")
                self.assertNotIn("fake-key", config_file.read_text(encoding="utf-8"))
                self.assertEqual(app.conversation.messages, [])

    async def test_tui_confirms_key_deletion_and_clears_its_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            config_file = root / ".local" / "config.json"
            save_api_key("first-fake-key", env_file)
            create_key_profile("第二个 Key", "second-fake-key",
                               path=config_file, env_file=env_file)
            set_default_key("key2", config_file)
            app = DianStarApp(Conversation.create(root / "conversations"),
                              config_file=config_file, env_file=env_file)
            async with app.run_test() as pilot:
                app.query_one("#input", ChatInput).text = "/config"
                await pilot.press("enter", "enter", "enter")
                screen = app.screen
                self.assertEqual(screen.page, "key-edit")
                await pilot.click("#key-delete")
                await pilot.pause(0.4)
                self.assertEqual(load_model_settings(config_file).default_key_id, "key2")
                await pilot.click("#key-delete")
                await pilot.pause()
                self.assertEqual(screen.page, "key")
                self.assertEqual(load_model_settings(config_file).default_key_id, "primary")
                self.assertIsNone(dotenv_values(env_file).get("DEEPSEEK_API_KEY_KEY2"))

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_request_uses_default_keys_own_model(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=first-fake-key\n", encoding="utf-8")
            config_file = root / ".local" / "config.json"
            create_key_profile("第二个 Key", "second-fake-key", path=config_file, env_file=env_file)
            save_model_settings(["deepseek-v4-pro", "my-model"], "my-model", config_file,
                                key_id="key2")
            set_default_key("key2", config_file)
            client = client_class.return_value
            client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop",
                                         message=SimpleNamespace(content="你好"))]))
            self.assertEqual(await run_turn("你好", env_file=env_file), "你好")
            self.assertEqual(client_class.call_args.kwargs["api_key"], "second-fake-key")
            self.assertEqual(client.chat.completions.create.await_args.kwargs["model"], "my-model")

    async def test_tui_adds_key_and_edits_only_its_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=first-fake-key\n", encoding="utf-8")
            config_file = root / ".local" / "config.json"
            app = DianStarApp(Conversation.create(root / "conversations"),
                              config_file=config_file, env_file=env_file)
            async with app.run_test() as pilot:
                app.query_one("#input", ChatInput).text = "/config"
                await pilot.press("enter", "enter", "down", "enter")
                screen = app.screen
                self.assertEqual(screen.page, "key-edit")
                screen.query_one("#key-name", Input).value = "第二个 Key"
                secret = screen.query_one("#config-key", Input)
                self.assertTrue(secret.password)
                secret.value = "second-fake-key"
                await pilot.pause()
                await pilot.click("#key-save-default")
                await pilot.pause()
                self.assertEqual(screen.page, "key")
                self.assertEqual(load_model_settings(config_file).default_key_id, "key2")
                await pilot.press("escape", "down", "enter")
                self.assertEqual(screen.page, "models")
                self.assertEqual(len(screen.query_one("#model-list", ListView).children), 0)
                editor = screen.query_one("#model-add-input", Input)
                for model in ("deepseek-flash", "deepseek-v4-pro", "third-model", "fourth-model"):
                    editor.value = model
                    await pilot.press("enter")
                self.assertEqual(screen.page, "models")
                settings = load_model_settings(config_file)
                self.assertEqual(len(settings.profile("key2").models), 4)
                self.assertEqual(len(settings.profile("primary").models), 2)
                selector = screen.query_one("#model-key-select", Select)
                selector.value = "primary"
                for _ in range(5):
                    await pilot.pause()
                    if screen.selected_model_key_id == "primary" and len(
                            screen.query_one("#model-list", ListView).children) == 2:
                        break
                self.assertEqual(screen.selected_model_key_id, "primary")
                self.assertEqual(len(screen.query_one("#model-list", ListView).children), 2)
                editor.value = "fifth-model"
                await pilot.press("enter")
                settings = load_model_settings(config_file)
                self.assertEqual(len(settings.profile("primary").models), 3)
                self.assertEqual(len(settings.profile("key2").models), 4)
                self.assertEqual(settings.default_key_id, "key2")
                selector.value = "key2"
                for _ in range(5):
                    await pilot.pause()
                    if screen.selected_model_key_id == "key2" and len(
                            screen.query_one("#model-list", ListView).children) == 4:
                        break
                screen.query_one("#model-list", ListView).index = 0
                await pilot.click("#model-delete")
                await pilot.pause()
                self.assertEqual(load_model_settings(config_file).profile("key2").models,
                                 ("deepseek-v4-pro", "third-model", "fourth-model"))
                self.assertEqual(app.conversation.messages, [])

    async def test_escape_returns_from_detail_without_saving(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / ".local" / "config.json"
            env_file = root / ".env"
            app = DianStarApp(Conversation.create(root / "conversations"),
                              config_file=config_file, env_file=env_file)
            async with app.run_test() as pilot:
                app.query_one("#input", ChatInput).text = "/config"
                await pilot.press("enter", "down", "enter")
                screen = app.screen
                self.assertEqual(screen.page, "models")
                screen.query_one("#model-add-input", Input).value = "unsaved-model"
                await pilot.press("escape")
                self.assertEqual(screen.page, "menu")
                await pilot.press("escape")
                self.assertNotIsInstance(app.screen, ConfigScreen)
                self.assertFalse(config_file.exists())

    @patch("dianstar.providers.deepseek.AsyncOpenAI")
    async def test_selected_model_reaches_sdk_request(self, client_class):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".env").write_text("DEEPSEEK_API_KEY=fake-key\n", encoding="utf-8")
            save_model_settings(["deepseek-flash", "deepseek-chat"], "deepseek-chat",
                                root / ".local" / "config.json")
            client = client_class.return_value
            client.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="stop",
                                         message=SimpleNamespace(content="你好"))]))
            self.assertEqual(await run_turn("你好", env_file=root / ".env"), "你好")
            self.assertEqual(client.chat.completions.create.await_args.kwargs["model"],
                             "deepseek-chat")

    async def test_config_command_masks_existing_key_and_saves_default_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            env_file.write_text("DEEPSEEK_API_KEY=old-fake-key\n", encoding="utf-8")
            config_file = root / ".local" / "config.json"
            app = DianStarApp(Conversation.create(root / "conversations"),
                              config_file=config_file, env_file=env_file)
            async with app.run_test() as pilot:
                app.query_one("#input", ChatInput).text = "/config"
                await pilot.press("enter")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, ConfigScreen)
                menu = screen.query_one("#config-menu", ListView)
                self.assertEqual(menu.index, 0)
                icons = [str(screen.query_one(f"#{name} Static").render()).split()[0]
                         for name in ("menu-key", "menu-models", "menu-default", "menu-shortcuts")]
                self.assertEqual(icons, ["▣"] * 4)
                await pilot.press("enter")
                self.assertEqual(screen.page, "key")
                await pilot.press("enter")
                self.assertEqual(screen.page, "key-edit")
                key = screen.query_one("#config-key", Input)
                self.assertTrue(key.password)
                self.assertEqual(key.value, "old-fake-key")
                key.value = "new-fake-key"
                await pilot.press("enter")
                self.assertEqual(screen.page, "key")
                await pilot.press("escape")
                self.assertEqual(screen.page, "menu")
                await pilot.press("down", "enter")
                self.assertEqual(screen.page, "models")
                screen.query_one("#model-add-input", Input).value = "deepseek-chat"
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(screen.page, "menu")
                self.assertIs(app.focused, menu)
                await pilot.press("down", "enter")
                self.assertEqual(screen.page, "default")
                screen.query_one("#default-list", ListView).index = 2
                await pilot.press("enter")
                self.assertEqual(screen.page, "menu")
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, ConfigScreen)
                self.assertEqual(load_model_settings(config_file).default_model, "deepseek-chat")
                self.assertEqual(load_model_settings(config_file).models,
                                 ("deepseek-flash", "deepseek-v4-pro", "deepseek-chat"))
                self.assertEqual(dotenv_values(env_file)["DEEPSEEK_API_KEY"], "new-fake-key")
                self.assertEqual(app.conversation.messages, [])

    async def test_shortcut_setting_changes_model_cycle_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_file = root / ".local" / "config.json"
            save_api_key("fake-key", root / ".env")
            app = DianStarApp(Conversation.create(root / "conversations"),
                              config_file=config_file, env_file=root / ".env")
            async with app.run_test() as pilot:
                app.query_one("#input", ChatInput).text = "/config"
                await pilot.press("enter", "down", "down", "down", "enter")
                screen = app.screen
                self.assertEqual(screen.page, "shortcuts")
                field = screen.query_one("#shortcut-model-input", Input)
                self.assertEqual(field.value, "CTRL+B")
                field.value = "Alt + M"
                await pilot.press("enter")
                self.assertEqual(load_model_settings(config_file).switch_model_shortcut, "alt+m")
                self.assertEqual(screen.page, "menu")
                self.assertEqual(app.conversation.messages, [])
                await pilot.press("escape")
                await pilot.pause()
                self.assertEqual(app._installed_model_shortcut, "alt+m")
                self.assertFalse(app.busy, "busy before old shortcut")
                await pilot.press("ctrl+b")
                self.assertFalse(app.busy, "busy after old shortcut")
                self.assertNotIsInstance(app.screen, ConfigScreen)
                await pilot.press("alt+m")
                self.assertEqual(load_model_settings(config_file).default_model, "deepseek-v4-pro")
                self.assertNotIsInstance(app.screen, ConfigScreen)
                footer = str(app.query_one("#key-help", Static).render())
                self.assertIn("▣ 模型: deepseek-v4-pro", footer)
                self.assertNotIn("ALT+M", footer)
