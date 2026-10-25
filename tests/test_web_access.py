"""固定网页权限配置的持久化、拒绝授权与模型闭环；模拟网络和假 Key。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dianstar.agent import ask_deepseek
from dianstar.config import save_api_key
from dianstar.conversations import Conversation
from dianstar.search_config import load_search_settings, save_search_settings
from dianstar.tools.web import FETCH_TOOL, SEARCH_TOOL, WebTools
from dianstar.tools.web_access import WEB_ACCESS_STATUS_TOOL as WEB_ACCESS_TOOL, manage_web_access
from dianstar.tui import DianStarApp
from dianstar.web_approval import PaidWebApproval


class WebAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_term_confirmation_ui_saves_only_after_confirming(self):
        import asyncio
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env, config = root / '.env', root / '.local/config.json'
            save_api_key('fake-key', env)
            app = DianStarApp(Conversation.create(root / 'conversations'), config_file=config, env_file=env)
            outcomes = []
            async with app.run_test(size=(80, 24)) as pilot:
                async def change():
                    outcomes.append(await manage_web_access('allow', env_file=env, request_approval=app._approve_paid_web))
                worker = app.run_worker(change())
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, PaidWebApproval)
                self.assertFalse(app.screen.query('#paid-web-turn'))
                self.assertFalse(config.with_name('search.json').exists())
                button = app.screen.query_one('#paid-web-always')
                self.assertLessEqual(button.region.right, button.parent.content_region.right)
                await pilot.click('#paid-web-always')
                await asyncio.wait_for(worker.wait(), 5)
                self.assertTrue(outcomes[0]['saved'])
                self.assertEqual(load_search_settings(config.with_name('search.json')).paid_policy, 'allow')

    async def test_status_is_read_only_and_restrictive_changes_persist_without_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            path = env.parent / '.local/search.json'
            approve = AsyncMock()
            result = await manage_web_access('status', env_file=env)
            self.assertEqual((result['paid_policy'], result['saved']), ('ask', False))
            self.assertFalse(path.exists())
            # 旧配置中的独立 Key 已删除，也应能撤销长期授权。
            path.parent.mkdir()
            path.write_text(json.dumps({'provider':'auto','key_id':'deleted','paid_policy':'allow'}))
            for action in ['ask', 'deny', 'disable']:
                result = await manage_web_access(action, env_file=env, request_approval=approve)
                self.assertTrue(result['saved'])
                self.assertEqual(load_search_settings(path).key_id, 'deleted')
            self.assertEqual(load_search_settings(path).paid_policy, 'deny')
            self.assertFalse(result['web_enabled'])
            result = await manage_web_access('enable', env_file=env)
            self.assertTrue(result['web_enabled'])
            self.assertEqual(result['paid_policy'], 'deny')
            approve.assert_not_awaited()

    async def test_long_term_allow_requires_explicit_confirmation_and_preserves_config_on_denial(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            save_api_key('fake-secret', env)
            path = env.parent / '.local/search.json'
            save_search_settings('auto', 'current', path=path, env_file=env,
                                 config_file=path.with_name('config.json'), paid_policy='deny')
            original = path.read_bytes()
            result = await manage_web_access('allow', env_file=env)
            self.assertEqual(result['error'], 'web_access_approval_required')
            for choice in ['deny', 'turn', None]:
                approve = AsyncMock(return_value=choice)
                result = await manage_web_access('allow', env_file=env, request_approval=approve)
                self.assertEqual(result['error'], 'web_access_change_declined')
                self.assertTrue(approve.await_args.args[0]['settings_change'])
                self.assertEqual(path.read_bytes(), original)
            result = await manage_web_access('allow', env_file=env,
                                            request_approval=AsyncMock(return_value='always'))
            self.assertTrue(result['saved'])
            self.assertEqual(load_search_settings(path).paid_policy, 'allow')
            self.assertNotIn('fake-secret', json.dumps(result) + path.read_text())

    async def test_invalid_action_and_save_failure_do_not_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            result = await manage_web_access('../../other', env_file=env)
            self.assertEqual(result['error'], 'invalid_web_access_action')
            with patch('dianstar.tools.web_access.save_search_settings', side_effect=OSError('sensitive detail')):
                result = await manage_web_access('deny', env_file=env)
            self.assertEqual(result['error'], 'web_access_save_failed')
            self.assertNotIn('sensitive detail', json.dumps(result))
            self.assertNotIn('saved', result)
            self.assertFalse((env.parent / '.local/search.json').exists())

    @patch('dianstar.deepseek.AsyncOpenAI')
    async def test_direct_revocation_is_applied_before_research_and_consent_is_turn_scoped(self, client_class):
        def response(name=None, arguments=None):
            message = SimpleNamespace(content='已恢复询问' if name is None else None,
                tool_calls=[] if name is None else [SimpleNamespace(id=str(arguments),
                    function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))])
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop' if name is None else 'tool_calls', message=message)])
        step = 0
        async def model(**kwargs):
            nonlocal step
            step += 1
            if step == 1:
                return response('read_webpage', {'url':'https://example.com/one'})
            if step == 2:
                return response('read_webpage', {'url':'https://example.com/two'})
            results = [json.loads(message['content']) for message in kwargs['messages'] if message['role'] == 'tool']
            results = [result for result in results if result.get('api_fallback')]
            if step == 3:
                return response('read_webpage', {'url':'https://example.com/one','api_fallback':results[0]['api_fallback']})
            if step == 4:
                return response('read_webpage', {'url':'https://example.com/two','api_fallback':results[1]['api_fallback']})
            return response()
        client_class.return_value.chat.completions.create = AsyncMock(side_effect=model)
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            save_api_key('fake-key', env)
            from dianstar.tools.web_reader import WebError
            async def fetch(service, url):
                if service._provider == 'free':
                    raise WebError('empty_page', '无正文')
                return {'content':'正文'}
            approve = AsyncMock(return_value='turn')
            with patch.object(WebTools, 'fetch', new=fetch):
                await ask_deepseek('撤销始终允许', env_file=env, on_paid_web=approve)
            self.assertEqual(approve.await_count, 1)
            self.assertEqual(load_search_settings(env.parent / '.local/search.json').paid_policy, 'ask')

    @patch('dianstar.deepseek.AsyncOpenAI')
    async def test_disabled_web_can_be_enabled_by_tool_then_read_in_same_question(self, client_class):
        first = SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', message=SimpleNamespace(content=None,
            tool_calls=[SimpleNamespace(id='enable', function=SimpleNamespace(name='manage_web_access', arguments='{"action":"enable"}'))]))])
        second = SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', message=SimpleNamespace(content=None,
            tool_calls=[SimpleNamespace(id='read', function=SimpleNamespace(name='read_webpage', arguments='{"url":"https://example.com"}'))]))])
        final = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='已启用并读取'))])
        classifier = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"action":"enable"}'))])
        create = client_class.return_value.chat.completions.create = AsyncMock(side_effect=[classifier, second, final])
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            save_api_key('fake-key', env)
            path = env.parent / '.local/search.json'
            save_search_settings('disabled', 'current', path=path, env_file=env, config_file=path.with_name('config.json'))
            with patch.object(WebTools, 'fetch', new=AsyncMock(return_value={'content':'正文'})) as fetch:
                answer = await ask_deepseek('开启网页访问并读取示例页面', env_file=env, on_permission=AsyncMock(return_value=True))
            self.assertEqual(answer, '已启用并读取')
            fetch.assert_awaited_once()
            self.assertNotIn('tools', create.await_args_list[0].kwargs)
            updated = create.await_args_list[1].kwargs['tools']
            self.assertIn(WEB_ACCESS_TOOL, updated)
            self.assertIn(FETCH_TOOL, updated)
            self.assertEqual(load_search_settings(path).provider, 'auto')

    @patch('dianstar.deepseek.AsyncOpenAI')
    async def test_disable_denies_already_queued_read_without_network(self, client_class):
        calls = [SimpleNamespace(id='disable', function=SimpleNamespace(name='manage_web_access', arguments='{"action":"disable"}')),
                 SimpleNamespace(id='read', function=SimpleNamespace(name='read_webpage', arguments='{"url":"https://example.com"}'))]
        create = client_class.return_value.chat.completions.create = AsyncMock(side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', message=SimpleNamespace(content=None, tool_calls=calls))]),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='已关闭网页访问'))])])
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            save_api_key('fake-key', env)
            with patch.object(WebTools, 'fetch', new=AsyncMock()) as fetch:
                await ask_deepseek('关闭网页访问', env_file=env)
            fetch.assert_not_awaited()
            request = create.await_args_list[-1].kwargs
            self.assertIn(WEB_ACCESS_TOOL, request['tools'])
            self.assertNotIn(FETCH_TOOL, request['tools'])
            results = [json.loads(message['content']) for message in request['messages'] if message['role'] == 'tool']
            self.assertTrue(results[0]['saved'])
            self.assertFalse(results[0]['web_enabled'])
