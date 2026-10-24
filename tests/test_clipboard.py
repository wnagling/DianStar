import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, PropertyMock, patch

from dianstar.clipboard import copy_windows_text
from dianstar.context_menu import WindowsCopyMenuClient
from dianstar.conversations import Conversation
from dianstar.tui import ChatLog, CopyMenu, DianStarApp


class ClipboardTests(unittest.TestCase):
    @patch("dianstar.clipboard.subprocess.run")
    def test_unicode_and_shell_characters_are_passed_as_data(self, run):
        text = '中文答案\n$env:PATH; $(exit); "quoted"\n'
        copy_windows_text(text)
        args, options = run.call_args
        self.assertEqual(options["input"], text)
        self.assertEqual(options["encoding"], "utf-8")
        self.assertNotIn(text, args[0][-1])
        self.assertIn("ReadToEnd()", args[0][-1])
        self.assertTrue(options["check"])
        self.assertEqual(options["timeout"], 5)
        self.assertTrue(options["capture_output"])


class ClipboardUiTests(unittest.IsolatedAsyncioTestCase):
    @patch("dianstar.context_menu.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    async def test_native_menu_receives_only_coordinates_and_reports_choice(self, create):
        from unittest.mock import Mock
        process = create.return_value
        process.returncode = None
        process.stdin.write = Mock()
        process.stdin.drain = AsyncMock()
        process.stdout.readline = AsyncMock(side_effect=[
            b"ready\n", b"shown\n", b"copy\n", b"shown\n", b"cancel\n",
            b"shown\n", b"outside\n", b"shown\n", b"bad response\n"])
        process.kill = Mock()
        process.wait = AsyncMock()
        client = WindowsCopyMenuClient()
        await client.start()
        self.assertEqual(await client.show((420, 250)), "copy")
        self.assertIn(b'"x": 420', process.stdin.write.call_args.args[0])
        self.assertIn("-STA", create.await_args.args)
        self.assertIn("-EncodedCommand", create.await_args.args)
        self.assertEqual(await client.show((420, 250)), "cancel")
        self.assertEqual(await client.show((420, 250)), "outside")
        create.assert_awaited_once()
        process.kill.assert_not_called()
        with self.assertRaises(ValueError):
            await client.show((420, 250))
        process.kill.assert_called_once()

    @patch("dianstar.context_menu.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    async def test_cancel_closes_native_menu_process(self, create):
        import asyncio
        from unittest.mock import Mock
        process = create.return_value
        process.returncode = None
        process.stdout.readline = AsyncMock(side_effect=[b"ready\n", b"shown\n", asyncio.CancelledError])
        process.stdin.write = Mock()
        process.stdin.drain = AsyncMock()
        process.kill = Mock()
        process.wait = AsyncMock()
        with self.assertRaises(asyncio.CancelledError):
            await WindowsCopyMenuClient().show((420, 250))
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    @patch("dianstar.context_menu.asyncio.create_subprocess_exec", new_callable=AsyncMock)
    async def test_failed_warmup_can_retry_without_reusing_broken_protocol(self, create):
        from unittest.mock import Mock
        first, second = Mock(), Mock()
        for process in (first, second):
            process.returncode = None
            process.stdin.drain = AsyncMock()
            process.wait = AsyncMock()
        first.stdout.readline = AsyncMock(return_value=b"invalid\n")
        second.stdout.readline = AsyncMock(side_effect=[b"ready\n", b"shown\n", b"cancel\n"])
        create.side_effect = [first, second]
        client = WindowsCopyMenuClient()
        with self.assertRaises(ValueError):
            await client.start()
        first.kill.assert_called_once()
        self.assertEqual(await client.show((100, 200)), "cancel")
        self.assertEqual(create.await_count, 2)
        await client.close()
        second.kill.assert_called_once()

    async def test_native_popup_preserves_selection_and_copies_only_on_choice(self):
        from textual.geometry import Offset
        from textual.selection import Selection
        with tempfile.TemporaryDirectory() as temporary:
            conversation = Conversation.create(Path(temporary))
            conversation.messages = [{"role": "user", "content": "只复制这四字，其他不要"}]
            app = DianStarApp(conversation)
            async with app.run_test() as pilot:
                await pilot.pause()
                chat = app.query_one("#chat", ChatLog)
                app.screen.selections = {chat: Selection(Offset(0, 1), Offset(4, 1))}
                original_selection = dict(app.screen.selections)
                with patch("dianstar.tui.os.name", "nt"), patch.object(
                    DianStarApp, "is_headless", new_callable=PropertyMock, return_value=False
                ), patch("dianstar.tui.cursor_position", return_value=(420, 250)), patch.object(
                    app._native_menu, "show", return_value="copy"
                ) as native, patch.object(app, "copy_to_clipboard") as copy:
                    await pilot.mouse_down(chat, offset=(18, 4), button=3)
                    await pilot.mouse_up(chat, offset=(18, 4))
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    native.assert_called_once_with((420, 250))
                    copy.assert_called_once_with("只复制这")
                    self.assertEqual(app.screen.selections, original_selection)
                    self.assertFalse(app.query_one("#copy-menu", CopyMenu).display)
                    native.return_value = "cancel"
                    copy.reset_mock()
                    await pilot.mouse_down(chat, offset=(30, 6), button=3)
                    await pilot.mouse_up(chat, offset=(30, 6))
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    copy.assert_not_called()
                    self.assertEqual(app.screen.selections, original_selection)
                    native.return_value = "outside"
                    await pilot.mouse_down(chat, offset=(30, 6), button=3)
                    await pilot.mouse_up(chat, offset=(30, 6))
                    await pilot.pause()
                    await app.workers.wait_for_complete()
                    self.assertEqual(app.screen.selections, {})
                    copy.assert_not_called()

    async def test_windows_copy_reports_success_and_failure_truthfully(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = DianStarApp(Conversation.create(Path(temporary)))
            async with app.run_test() as pilot:
                await pilot.pause()
                with patch("dianstar.tui.os.name", "nt"), patch.object(
                    DianStarApp, "is_headless", new_callable=PropertyMock, return_value=False
                ), patch("dianstar.tui.copy_windows_text") as native, patch.object(app, "_status") as status:
                    app.copy_to_clipboard("中文答案")
                    native.assert_called_once_with("中文答案")
                    self.assertEqual(app.clipboard, "中文答案")
                    status.assert_not_called()
                    native.side_effect = subprocess.TimeoutExpired("clipboard", 5)
                    app.copy_to_clipboard("不应声称成功")
                    self.assertEqual(app.clipboard, "中文答案")
                    status.assert_called_with("复制失败：系统剪贴板暂不可用，请重试。")
