"""免费优先、付费授权、预算和 TUI 中断恢复。全部使用假 Key 与模拟工具。"""

import asyncio
import json
import tempfile
import unittest
import httpx
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from textual.widgets import Static

from dianstar.config import save_api_key
from dianstar.agent import ask_deepseek
from dianstar.conversations import Conversation
from dianstar.search_config import SearchSettings, load_search_settings
from dianstar.tools.web import MAX_PAID_WEB_CALLS, MAX_WEB_OPERATIONS, WebTools
from dianstar.tools.web_reader import WebError, extract_page
from dianstar.tui import ChatInput, DianStarApp
from dianstar.web_approval import PaidWebApproval


class FallbackTests(unittest.IsolatedAsyncioTestCase):
    def service(self, root, *, policy='ask', approve=None):
        save_api_key('fake-key', root / '.env')
        return WebTools(SearchSettings('auto', 'current', policy), root / '.env', approve)

    def fake_fetch(self, service, calls):
        async def fetch(url):
            calls.append(service._provider)
            if service._provider == 'free':
                raise WebError('empty_page', '未取得正文')
            return {'provider':'deepseek', 'url':url, 'content':'正文'}
        return fetch

    async def prepare(self, service):
        results = []
        for suffix in ['one', 'two']:
            arguments = {'url':f'https://example.com/{suffix}'}
            result = await service.execute('read_webpage', arguments)
            results.append({**arguments, 'api_fallback':result['api_fallback']})
        return results

    async def test_first_irrelevant_search_never_prompts_even_with_persistent_allow(self):
        with tempfile.TemporaryDirectory() as temporary:
            for policy in ['ask', 'allow']:
                approve = AsyncMock(return_value='turn')
                requests = []
                def respond(request):
                    requests.append(request)
                    return httpx.Response(200, text='<rss><channel><item><title>Office license removal</title><link>https://example.com/office</link><description>Translation guide</description></item></channel></rss>')
                async with self.service(Path(temporary), policy=policy, approve=approve) as service:
                    service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
                    result = await service.execute('search_web', {'query':'prompt injection defense'})
                    self.assertEqual(result['error'], 'search_low_relevance')
                    self.assertNotIn('api_fallback', result)
                    self.assertEqual(service.paid_calls, 0)
                    approve.assert_not_awaited()
                    self.assertEqual([r.method for r in requests], ['GET', 'GET'])

    async def test_requires_distinct_free_alternative_and_exact_target_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            approve = AsyncMock(return_value='turn')
            async with self.service(Path(temporary), approve=approve) as service:
                calls = []
                with patch.object(service, 'fetch', side_effect=self.fake_fetch(service, calls)):
                    args = {'url':'https://example.com/one'}
                    first = await service.execute('read_webpage', args)
                    continued = {**args, 'api_fallback':first['api_fallback']}
                    result = await service.execute('read_webpage', continued)
                    self.assertEqual(result['error'], 'empty_page')
                    approve.assert_not_awaited()
                    self.assertEqual(calls, ['free'])
                    for token in ['invented', [], None]:
                        bad = await service.execute('read_webpage', {**args,'api_fallback':token})
                        # None 不指定备用，与首次免费访问相同；其他非法 token 不发请求。
                        if token is not None:
                            self.assertEqual(bad['error'], 'invalid_api_fallback')
                    # 对同一目标重新免费访问会使旧 token 失效。
                    latest = await service.execute('read_webpage', args)
                    continued['api_fallback'] = latest['api_fallback']
                    self.assertEqual((await service.execute('read_webpage', {**continued,'url':'https://example.com/other'}))['error'], 'invalid_api_fallback')
                    await service.execute('read_webpage', {'url':'https://example.com/two'})
                    result = await service.execute('read_webpage', continued)
                    self.assertEqual(result['provider'], 'deepseek')
                    self.assertEqual(result['free_error'], 'empty_page')
                    approve.assert_awaited_once()
                    self.assertEqual(approve.await_args.args[0]['free_error'], 'empty_page')
                    self.assertEqual((await service.execute('read_webpage', continued))['error'], 'invalid_api_fallback')

    async def test_recovered_free_target_invalidates_its_old_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            approve = AsyncMock()
            async with self.service(Path(temporary), approve=approve) as service:
                with patch.object(service, 'fetch', side_effect=[WebError('empty_page','无正文'), {'content':'已恢复'}]):
                    args = {'url':'https://example.com/one'}
                    first = await service.execute('read_webpage', args)
                    await service.execute('read_webpage', args)
                    result = await service.execute('read_webpage', {**args,'api_fallback':first['api_fallback']})
                self.assertEqual(result['error'], 'invalid_api_fallback')
                approve.assert_not_awaited()

    async def test_fallback_tokens_cannot_be_restored_in_next_question(self):
        with tempfile.TemporaryDirectory() as temporary:
            approve = AsyncMock()
            args = {'url':'https://example.com/one'}
            async with self.service(Path(temporary), approve=approve) as first:
                with patch.object(first, 'fetch', side_effect=self.fake_fetch(first, [])):
                    result = await first.execute('read_webpage', args)
            async with self.service(Path(temporary), approve=approve) as second:
                with patch.object(second, 'fetch', new=AsyncMock()) as fetch:
                    result = await second.execute('read_webpage', {**args, 'api_fallback':result['api_fallback']})
                    self.assertEqual(result['error'], 'invalid_api_fallback')
                    fetch.assert_not_awaited()
            approve.assert_not_awaited()

    @patch('dianstar.deepseek.AsyncOpenAI')
    async def test_model_can_explore_thirty_free_pages_then_continue_thirty_paid_requests(self, client_class):
        step = 0
        async def model(**kwargs):
            nonlocal step
            step += 1
            if step > 60:
                return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='已核对资料'))])
            args = {'url':f'https://example.com/{(step-1)%30}'}
            if step > 30:
                results = [json.loads(m['content']) for m in kwargs['messages'] if m['role']=='tool']
                args['api_fallback'] = results[step-31]['api_fallback']
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls',message=SimpleNamespace(content=None,
                tool_calls=[SimpleNamespace(id=str(step),function=SimpleNamespace(name='read_webpage',arguments=json.dumps(args)))]))])
        client_class.return_value.chat.completions.create = AsyncMock(side_effect=model)
        async def fetch(service, url):
            if service._provider == 'free':
                raise WebError('empty_page', '未取得正文')
            return {'provider':'deepseek', 'content':'正文'}
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary)/'.env'
            save_api_key('fake-key',env)
            approve = AsyncMock(return_value='turn')
            with patch.object(WebTools,'fetch',new=fetch):
                answer = await ask_deepseek('核对多个必要来源',env_file=env,on_paid_web=approve)
            self.assertEqual(answer,'已核对资料')
            self.assertEqual(step,61)
            approve.assert_awaited_once()

    async def test_thirty_free_failures_then_thirty_paid_continuations_have_independent_budgets(self):
        with tempfile.TemporaryDirectory() as temporary:
            approve = AsyncMock(return_value='turn')
            async with self.service(Path(temporary), approve=approve) as service:
                calls, continuations = [], []
                with patch.object(service, 'fetch', side_effect=self.fake_fetch(service, calls)):
                    for index in range(30):
                        args = {'url':f'https://example.com/{index}'}
                        result = await service.execute('read_webpage', args)
                        continuations.append({**args,'api_fallback':result['api_fallback']})
                    approve.assert_not_awaited()
                    for args in continuations:
                        self.assertEqual((await service.execute('read_webpage', args))['provider'], 'deepseek')
                    self.assertEqual((await service.execute('read_webpage', {'url':'https://example.com/extra'}))['error'],'web_call_limit')
                self.assertEqual((service.operations, service.paid_calls, service.calls), (30,30,60))
                self.assertEqual(calls, ['free']*30 + ['deepseek']*30)
                approve.assert_awaited_once()

    async def test_turn_consent_reused_but_new_question_asks_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            approve = AsyncMock(return_value='turn')
            for _ in range(2):
                async with self.service(Path(temporary), approve=approve) as service:
                    with patch.object(service,'fetch',side_effect=self.fake_fetch(service, [])):
                        continuations = await self.prepare(service)
                        for args in continuations:
                            self.assertEqual((await service.execute('read_webpage',args))['provider'],'deepseek')
            self.assertEqual(approve.await_count, 2)
            self.assertFalse((Path(temporary)/'.local/search.json').exists())

    async def test_deny_is_not_repeated_and_never_calls_paid_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            approve = AsyncMock(return_value='deny')
            async with self.service(Path(temporary),approve=approve) as service:
                calls=[]
                with patch.object(service,'fetch',side_effect=self.fake_fetch(service,calls)):
                    for args in await self.prepare(service):
                        self.assertEqual((await service.execute('read_webpage',args))['error'],'paid_web_declined')
                self.assertEqual(calls,['free','free'])
                approve.assert_awaited_once()

    async def test_persistent_allow_or_deny_only_applies_to_explicit_continuation(self):
        with tempfile.TemporaryDirectory() as temporary:
            for policy, expected in [('allow',1),('deny',0)]:
                approve=AsyncMock()
                async with self.service(Path(temporary),policy=policy,approve=approve) as service:
                    with patch.object(service,'fetch',side_effect=self.fake_fetch(service,[])):
                        continuations=await self.prepare(service)
                        self.assertEqual(service.paid_calls,0)
                        await service.execute('read_webpage',continuations[0])
                    self.assertEqual(service.paid_calls,expected)
                    approve.assert_not_awaited()

    async def test_network_security_not_found_and_empty_results_do_not_issue_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            approve=AsyncMock()
            async with self.service(Path(temporary),approve=approve) as service:
                for code in ['web_timeout','web_unavailable','dns_unavailable','blocked_address','invalid_url',
                             'web_http_401','web_http_403','web_http_404','web_http_429','web_http_500',
                             'response_too_large','too_many_redirects']:
                    with patch.object(service,'fetch',side_effect=WebError(code,'模拟失败')):
                        result=await service.execute('read_webpage',{'url':f'https://example.com/{code}'})
                    self.assertEqual(result['error'],code)
                    self.assertNotIn('api_fallback',result)
                with patch.object(service,'search',new=AsyncMock(return_value={'results':[]})):
                    result=await service.execute('search_web',{'query':'q'})
                self.assertEqual(result['results'],[])
                self.assertNotIn('api_fallback',result)
                approve.assert_not_awaited()

    async def test_missing_key_or_approval_ui_never_charges(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            for key in [False, True]:
                if key:
                    save_api_key('fake-key',root/'.env')
                async with WebTools(SearchSettings(),root/'.env') as service:
                    with patch.object(service,'fetch',side_effect=self.fake_fetch(service,[])):
                        args=(await self.prepare(service))[0]
                        result=await service.execute('read_webpage',args)
                    self.assertEqual(result['error'],'paid_web_approval_required' if key else 'search_key_missing')
                    self.assertEqual(service.paid_calls,0)

    async def test_no_remaining_budget_never_requests_approval(self):
        with tempfile.TemporaryDirectory() as temporary:
            approve=AsyncMock()
            async with self.service(Path(temporary),approve=approve) as service:
                with patch.object(service,'fetch',side_effect=self.fake_fetch(service,[])):
                    args=(await self.prepare(service))[0]
                    service.paid_calls=MAX_PAID_WEB_CALLS
                    await service.execute('read_webpage',args)
                approve.assert_not_awaited()

    async def test_cancel_pending_approval_never_calls_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            started=asyncio.Event()
            async def approve(request):
                started.set()
                await asyncio.Event().wait()
            async with self.service(Path(temporary),approve=approve) as service:
                with patch.object(service,'fetch',side_effect=self.fake_fetch(service,[])):
                    args=(await self.prepare(service))[0]
                    task=asyncio.create_task(service.execute('read_webpage',args))
                    await asyncio.wait_for(started.wait(),5)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                self.assertEqual(service.paid_calls,0)

    @patch('dianstar.deepseek.AsyncOpenAI')
    async def test_search_failure_then_free_official_page_success_answers_without_prompt(self, client_class):
        def call(name,args,identifier):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls',message=SimpleNamespace(content=None,
                tool_calls=[SimpleNamespace(id=identifier,function=SimpleNamespace(name=name,arguments=json.dumps(args)))]))])
        client_class.return_value.chat.completions.create=AsyncMock(side_effect=[
            call('search_web',{'query':'Python official tutorial'},'search'),
            call('read_webpage',{'url':'https://docs.python.org/3/tutorial/index.html'},'read'),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='已找到官方教程'))])])
        with tempfile.TemporaryDirectory() as temporary:
            env=Path(temporary)/'.env'
            save_api_key('fake-key',env)
            approve=AsyncMock()
            with patch.object(WebTools,'search',new=AsyncMock(side_effect=WebError('search_low_relevance','无关结果'))), \
                 patch.object(WebTools,'fetch',new=AsyncMock(return_value={'provider':'free','content':'官方教程'})):
                answer=await ask_deepseek('找 Python 官方教程',env_file=env,on_paid_web=approve)
            self.assertEqual(answer,'已找到官方教程')
            approve.assert_not_awaited()

    @patch('dianstar.deepseek.AsyncOpenAI')
    async def test_model_loop_can_read_thirty_pages_then_answer(self, client_class):
        replies = [SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls',
            message=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id=f'call-{index}',
                function=SimpleNamespace(name='read_webpage', arguments=json.dumps({'url':f'https://example.com/{index}'})))]))])
            for index in range(30)]
        replies.append(SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='已汇总30页'))]))
        client_class.return_value.chat.completions.create = AsyncMock(side_effect=replies)
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            save_api_key('fake-key',env)
            with patch.object(WebTools,'fetch',new=AsyncMock(return_value={'content':'正文'})) as fetch:
                answer = await ask_deepseek('阅读资料后汇总',env_file=env)
            self.assertEqual(answer,'已汇总30页')
            self.assertEqual(fetch.await_count,30)
            self.assertEqual(client_class.return_value.chat.completions.create.await_count,31)

    def test_challenge_pages_are_not_treated_as_article_content(self):
        for html in ('<html><title>Just a moment...</title><p>Verify you are human</p></html>',
                     '<html><p>You need to enable JavaScript to run this app.</p></html>'):
            with self.assertRaisesRegex(WebError, '没有取得正文'):
                extract_page(html.encode(), 'text/html', 'utf-8')

    def test_old_service_preferences_migrate_to_free_first_policies(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'search.json'
            for provider, policy in [('free','ask'), ('deepseek','allow'), ('disabled','ask')]:
                path.write_text(json.dumps({'provider':provider,'key_id':'primary'}), encoding='utf-8')
                settings = load_search_settings(path)
                self.assertEqual(settings.provider, 'disabled' if provider == 'disabled' else 'auto')
                self.assertEqual(settings.paid_policy, policy)


class ApprovalUITests(unittest.IsolatedAsyncioTestCase):
    async def test_approval_dialog_pauses_answer_and_resumes_without_losing_question(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = DianStarApp(Conversation.create(root / 'conversations'),
                              config_file=root / '.local/config.json', env_file=root / '.env')
            choices = []
            async def answer(question, *, on_paid_web, **kwargs):
                kwargs['on_thinking']('核对网页来源')
                kwargs['on_thinking']('核对网页来源并整理资料')
                choices.append(await on_paid_web({'operation':'读取网页','target':'https://example.com', 'remaining_calls':3}))
                return '已取得内容'
            with patch('dianstar.tui.ask_deepseek', side_effect=answer):
                async with app.run_test(size=(80,24)) as pilot:
                    app.query_one('#input', ChatInput).text = '阅读这篇文章'
                    await pilot.press('enter')
                    await pilot.pause()
                    self.assertIsInstance(app.screen, PaidWebApproval)
                    self.assertTrue(app.busy)
                    self.assertEqual(app.conversation.messages, [])
                    for button in app.screen.query('Button'):
                        self.assertLessEqual(button.region.right, 80)
                        self.assertLessEqual(button.region.bottom, 24)
                    await pilot.click('#paid-web-turn')
                    await asyncio.wait_for(app.workers.wait_for_complete(), 5)
                    self.assertEqual(choices, ['turn'])
                    self.assertEqual(app.conversation.messages[0]['content'], '阅读这篇文章')
                    self.assertFalse(app.busy)

    async def test_escape_denies_and_always_choice_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, env = root / '.local/config.json', root / '.env'
            save_api_key('fake-key', env)
            app = DianStarApp(Conversation.create(root / 'conversations'), config_file=config, env_file=env)
            choices = []
            async with app.run_test(size=(80,24)) as pilot:
                for allow in (False, True):
                    async def prompt():
                        choices.append(await app._approve_paid_web({'operation':'搜索','target':'q', 'remaining_calls':3}))
                    worker = app.run_worker(prompt())
                    await pilot.pause(0.2)
                    self.assertIsInstance(app.screen, PaidWebApproval)
                    if allow:
                        button = app.screen.query_one('#paid-web-always')
                        self.assertGreater(button.region.width, 0)
                        self.assertLessEqual(button.region.right, button.parent.content_region.right)
                        # 弹窗在点击处理中关闭时 Pilot.click 可返回 False；以回调结果验收。
                        await pilot.click('#paid-web-always')
                    else:
                        await pilot.press('escape')
                    await pilot.pause(0.2)
                    await asyncio.wait_for(worker.wait(), 5)
                    await pilot.pause(0.2)
                self.assertEqual(choices, ['deny','always'])
                settings = load_search_settings(config.with_name('search.json'))
                self.assertEqual(settings.paid_policy, 'allow')
                self.assertNotIn('fake-key', config.with_name('search.json').read_text())
