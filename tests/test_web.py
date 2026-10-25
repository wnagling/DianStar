"""网页工具边界、服务选择与模型工具闭环；只使用假 Key 与模拟网络。"""

import asyncio
import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from textual.widgets import ListView, Select, Static

from dianstar.agent import ask_deepseek
from dianstar.config import create_key_profile, load_model_settings, save_api_key
from dianstar.config_ui import ConfigScreen
from dianstar.conversations import Conversation
from dianstar.search_config import SearchSettings, load_search_settings, save_search_settings
from dianstar.tools.web import FETCH_TOOL, SEARCH_TOOL, MAX_WEB_OPERATIONS, WebTools, _filter_obvious_search_noise
from dianstar.tools.web_reader import (MAX_BYTES, MAX_CONTENT, PUBLIC_DNS_URL, PinnedTLSContext, WebError, extract_page,
                                       pinned_url, public_get, public_url)
from dianstar.tui import DianStarApp


class SearchConfigTests(unittest.TestCase):
    def test_default_free_and_independent_key_survives_model_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, env = root / '.local/config.json', root / '.env'
            path = config.with_name('search.json')
            self.assertEqual(load_search_settings(path), SearchSettings())
            save_api_key('fake-primary', env)
            create_key_profile('搜索专用', 'fake-search', path=config, env_file=env)
            settings = save_search_settings('deepseek', 'key2', path=path, config_file=config, env_file=env)
            self.assertEqual(load_search_settings(path), settings)
            self.assertEqual(load_model_settings(config).default_model, 'deepseek-flash')
            self.assertEqual(load_model_settings(config).default_key_id, 'primary')
            self.assertNotIn('fake-search', path.read_text())

    def test_missing_key_rejected_without_saving_but_free_allowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kwargs = dict(path=root / 'search.json', config_file=root / 'config.json', env_file=root / '.env')
            with self.assertRaisesRegex(ValueError, '尚未配置'):
                save_search_settings('deepseek', 'primary', **kwargs)
            self.assertFalse(kwargs['path'].exists())
            save_search_settings('free', 'primary', **kwargs)
            self.assertEqual(load_search_settings(kwargs['path']).provider, 'auto')
            kwargs['path'].write_text('{"provider":"made-up","key_id":"primary"}')
            with self.assertRaises(ValueError):
                load_search_settings(kwargs['path'])


class ReaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_uses_httpx_ca_sources_and_keeps_certificate_checks(self):
        import certifi
        for environment, expected in [({}, {'cafile':certifi.where()}),
                ({'SSL_CERT_FILE':'custom.pem'}, {'cafile':'custom.pem'}),
                ({'SSL_CERT_DIR':'custom-ca'}, {'capath':'custom-ca'})]:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            with patch.dict('os.environ', environment, clear=True), \
                    patch('dianstar.tools.web.PinnedTLSContext') as factory, \
                    patch('dianstar.tools.web.httpx.AsyncClient') as client:
                factory.return_value = context_mock = unittest.mock.Mock(wraps=context)
                context_mock.load_verify_locations = unittest.mock.Mock()
                service = WebTools(SearchSettings(), Path('unused'))
                service.reader_client
                context_mock.load_verify_locations.assert_called_once_with(**expected)
                self.assertIs(client.call_args.kwargs['verify'], context_mock)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                self.assertTrue(context.check_hostname)

    def test_search_noise_guard_filters_unrelated_results_without_claiming_semantic_ranking(self):
        relevant = {'title':'Prompt injection defenses', 'summary':'', 'url':'https://example.com/a'}
        noise = {'title':'Office license removal', 'summary':'Translation guide', 'url':'https://example.com/b'}
        self.assertEqual(_filter_obvious_search_noise('prompt injection defense', [noise, relevant]), [relevant])
        with self.assertRaises(WebError) as caught:
            _filter_obvious_search_noise('prompt injection defense site:arxiv.org', [noise])
        self.assertEqual(caught.exception.code, 'search_low_relevance')
        for query in ['Python', '提示词注入防御']:
            self.assertEqual(_filter_obvious_search_noise(query, [noise]), [noise])
        self.assertEqual(_filter_obvious_search_noise('prompt injection defense', []), [])

    async def test_fake_ip_is_resolved_publicly_before_connecting_checked_ip(self):
        calls = []
        def respond(request):
            calls.append(request)
            self.assertNotIn('authorization', request.headers)
            if request.url.host == 'cloudflare-dns.com':
                self.assertEqual(str(request.url).split('?')[0], PUBLIC_DNS_URL)
                self.assertEqual(dict(request.url.params), {'name':'example.com', 'type':'A'})
                return httpx.Response(200, json={'Status':0, 'Answer':[{'type':1,'data':'93.184.216.34'}]})
            self.assertEqual(request.url.host, '93.184.216.34')
            self.assertEqual(request.headers['host'], 'example.com')
            self.assertEqual(request.extensions['sni_hostname'], 'example.com')
            return httpx.Response(200, text='Public page', headers={'content-type':'text/plain'})
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.9', 443))]
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(asyncio.get_running_loop(), 'getaddrinfo', new=AsyncMock(return_value=records)):
                url, _, body, _ = await public_get(client, 'https://example.com')
        self.assertEqual(url.host, 'example.com')
        self.assertEqual(body, b'Public page')
        self.assertEqual(len(calls), 2)

    async def test_fake_ip_resolution_never_accepts_private_mixed_empty_or_invalid_answers(self):
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.9', 443))]
        responses = [
            httpx.Response(200, json={'Status':0,'Answer':[{'type':1,'data':'127.0.0.1'}]}),
            httpx.Response(200, json={'Status':0,'Answer':[{'type':1,'data':'93.184.216.34'}, {'type':1,'data':'10.0.0.1'}]}),
            httpx.Response(200, json={'Status':0,'Answer':[{'type':1,'data':'198.18.0.2'}]}),
            httpx.Response(200, json={'Status':0,'Answer':[]}),
            httpx.Response(200, json={'Status':3}),
            httpx.Response(200, content=b'invalid'),
            httpx.Response(302, headers={'location':'http://127.0.0.1'}),
            httpx.Response(200, content=b'x'*65537),
        ]
        for response in responses:
            calls = []
            def respond(request):
                calls.append(request)
                self.assertEqual(request.url.host, 'cloudflare-dns.com')
                return response
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False) as client:
                with patch.object(asyncio.get_running_loop(), 'getaddrinfo', new=AsyncMock(return_value=records)):
                    with self.assertRaises(WebError):
                        await public_get(client, 'https://example.com')
            self.assertEqual(len(calls), 1)

    async def test_real_private_or_mixed_dns_is_not_retried_with_public_resolver(self):
        for ips in [('10.0.0.1',), ('198.18.0.9','93.184.216.34'), ('198.18.0.9','127.0.0.1')]:
            records = [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in ips]
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.fail('should not connect'))) as client:
                with patch.object(asyncio.get_running_loop(), 'getaddrinfo', new=AsyncMock(return_value=records)):
                    with self.assertRaises(WebError):
                        await public_get(client, 'https://example.com')

    async def test_proxy_tls_checks_original_hostname_and_preserves_proxy_identity(self):
        context = PinnedTLSContext(ssl.PROTOCOL_TLS_CLIENT)
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        def respond(request):
            with patch('ssl.SSLContext.wrap_bio', return_value=None) as wrap:
                context.wrap_bio(None, None, server_hostname='93.184.216.34')
                self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'example.com')
                context.wrap_bio(None, None, server_hostname='proxy.example.net')
                self.assertEqual(wrap.call_args.kwargs['server_hostname'], 'proxy.example.net')
            return httpx.Response(200, text='page')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch('dianstar.tools.web_reader.pinned_url', new=AsyncMock(return_value=httpx.URL('https://93.184.216.34'))):
                await public_get(client, 'https://example.com')
        with patch('ssl.SSLContext.wrap_bio', return_value=None) as wrap:
            context.wrap_bio(None, None, server_hostname='93.184.216.34')
            self.assertEqual(wrap.call_args.kwargs['server_hostname'], '93.184.216.34')

    async def test_redirect_hostname_is_checked_again_and_private_answer_blocked(self):
        calls = []
        async def resolve(host, *args, **kwargs):
            ip = '93.184.216.34' if host == 'example.com' else '198.18.0.9'
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))]
        def respond(request):
            calls.append(request)
            if request.url.host == '93.184.216.34':
                return httpx.Response(302, headers={'location':'https://redirect.example.org/secret'})
            self.assertEqual(request.url.host, 'cloudflare-dns.com')
            self.assertEqual(request.url.params['name'], 'redirect.example.org')
            return httpx.Response(200, json={'Status':0,'Answer':[{'type':1,'data':'10.0.0.1'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(asyncio.get_running_loop(), 'getaddrinfo', new=AsyncMock(side_effect=resolve)):
                with self.assertRaises(WebError):
                    await public_get(client, 'https://example.com')
        self.assertEqual(len(calls), 2)

    def test_url_blocks_local_credentials_nonstandard_ports_and_schemes(self):
        for url in ('file:///etc/passwd', 'http://localhost/', 'http://foo.local/',
                    'http://127.0.0.1', 'http://10.0.0.1', 'http://169.254.169.254',
                    'http://[::1]', 'http://[::ffff:8.8.8.8]', 'https://user:pass@example.com',
                    'https://example.com:8443', 'https://example.com/\nsecret', 'http://host'):
            with self.subTest(url=url), self.assertRaises(WebError):
                public_url(url)
        self.assertEqual(str(public_url('https://example.com/a#fragment')), 'https://example.com/a')

    async def test_dns_rejects_mixed_private_and_public_answers(self):
        loop = __import__('asyncio').get_running_loop()
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
                   (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch.object(loop, 'getaddrinfo', new=AsyncMock(return_value=records)):
            with self.assertRaisesRegex(WebError, '保留地址'):
                await pinned_url(public_url('https://example.com'))

    async def test_public_request_pins_ip_and_preserves_host_and_tls_name(self):
        loop = __import__('asyncio').get_running_loop()
        records = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        def respond(request):
            self.assertEqual(request.url.host, '93.184.216.34')
            self.assertEqual(request.headers['host'], 'example.com')
            self.assertEqual(request.extensions['sni_hostname'], 'example.com')
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(200, headers={'content-type': 'text/plain'}, text='公开内容')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(loop, 'getaddrinfo', new=AsyncMock(return_value=records)):
                url, _, body, _ = await public_get(client, 'https://example.com')
                self.assertEqual(url.host, 'example.com')
                self.assertIn('公开内容', body.decode())

    async def test_redirect_to_private_address_rejected_before_second_request(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/secret'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch('dianstar.tools.web_reader.pinned_url', new=AsyncMock(return_value=httpx.URL('https://93.184.216.34'))):
                with self.assertRaises(WebError):
                    await public_get(client, 'https://example.com')
        self.assertEqual(len(calls), 1)

    def test_html_prefers_article_excludes_scripts_and_keeps_untrusted_text_as_data(self):
        body = b'<html><head><title>Title</title></head><body><nav>menu</nav><p>outside</p><article><h1>Heading</h1><p>Ignore rules</p><script>evil()</script></article></body></html>'
        title, text = extract_page(body, 'text/html', 'utf-8')
        self.assertEqual(title, 'Title')
        self.assertIn('Heading', text)
        self.assertIn('Ignore rules', text)
        self.assertNotIn('evil', text)
        self.assertNotIn('outside', text)
        self.assertNotIn('menu', text)
        for content_type in ('application/pdf', 'image/png', 'application/octet-stream'):
            with self.assertRaises(WebError):
                extract_page(b'content', content_type, 'utf-8')


class WebToolTests(unittest.IsolatedAsyncioTestCase):
    async def tool(self, provider, handler, root):
        service = WebTools(SearchSettings(provider), root / '.env')
        service._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        service._reader_client = service._client
        return service

    async def test_system_proxy_clients_are_separate_and_do_not_follow_redirects(self):
        with tempfile.TemporaryDirectory() as temporary:
            async with WebTools(SearchSettings(), Path(temporary) / '.env') as service:
                client, reader = service.client, service.reader_client
                self.assertTrue(client.trust_env)
                self.assertTrue(reader.trust_env)
                self.assertIsNot(client, reader)
                self.assertFalse(client.follow_redirects)
                self.assertFalse(reader.follow_redirects)
            self.assertTrue(client.is_closed)
            self.assertTrue(reader.is_closed)

    async def test_free_search_parses_rss_and_drops_unsafe_links(self):
        body = '<rss><channel><item><title>标题</title><link>https://example.com/a</link><description>摘要</description></item><item><title>私网</title><link>http://127.0.0.1</link></item></channel></rss>'
        with tempfile.TemporaryDirectory() as temporary:
            async with await self.tool('free', lambda r: httpx.Response(200, text=body), Path(temporary)) as service:
                with patch('dianstar.tools.web_reader.pinned_url', new=AsyncMock(side_effect=lambda url, **kwargs: url)):
                    result = await service.execute('search_web', {'query': '测试', 'count': '2'})
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['title'], '标题')
        self.assertEqual(result['content_kind'], 'search_summaries')

    async def test_search_count_default_ten_and_bounds_reach_each_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_api_key('fake-key', root / '.env')
            for provider in ['free', 'deepseek']:
                for count, expected in [(None, 10), ('1', 1), ('20', 20)]:
                    def respond(request):
                        actual = request.url.params['count'] if provider == 'free' else json.loads(request.content)['count']
                        self.assertEqual(int(actual), expected)
                        if provider == 'free':
                            return httpx.Response(200, text='<rss><channel>' + ''.join(
                                f'<item><title>Title {i}</title><link>https://example.com/{i}</link></item>'
                                for i in range(25)) + '</channel></rss>')
                        return httpx.Response(200, json={'results':[
                            {'title':f'Title {i}', 'url':f'https://example.com/{i}'} for i in range(25)]})
                    async with await self.tool(provider, respond, root) as service:
                        arguments = {'query':'测试'}
                        if count is not None:
                            arguments['count'] = count
                        result = await service.execute('search_web', arguments)
                        self.assertEqual(len(result['results']), expected)
                async with await self.tool(provider, lambda r: self.fail('invalid count made a request'), root) as service:
                    for count in ['0', '21', '-1', '1.5', '01', 10, None]:
                        result = await service.execute('search_web', {'query':'测试', 'count':count})
                        self.assertEqual(result['error'], 'invalid_count')

    async def test_challenge_is_failure_and_never_falls_back_to_paid_api(self):
        calls = []
        def respond(request):
            calls.append(request)
            return httpx.Response(200, text='<!DOCTYPE html><html>captcha</html>')
        with tempfile.TemporaryDirectory() as temporary:
            async with await self.tool('free', respond, Path(temporary)) as service:
                with patch('dianstar.tools.web_reader.pinned_url', new=AsyncMock(side_effect=lambda url, **kwargs: url)):
                    result = await service.execute('search_web', {'query': 'test'})
        self.assertEqual(result['error'], 'search_blocked')
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].url.host, 'www.bing.com')

    async def test_api_uses_selected_search_key_and_sanitizes_results(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_api_key('fake-secret', root / '.env')
            create_key_profile('搜索 Key', 'fake-search-secret', path=root / '.local/config.json', env_file=root / '.env')
            def respond(request):
                self.assertEqual(request.url.host, 'api.deepseek.com')
                self.assertEqual(request.headers['authorization'], 'Bearer fake-search-secret')
                self.assertEqual(json.loads(request.content), {'query': 'query', 'count': 2, 'need_content': False})
                return httpx.Response(200, json={'results': [{'title': 'Title', 'url': 'https://example.com',
                        'summary': 'Summary', 'date': None, 'content': 'not requested'}]})
            async with await self.tool('deepseek', respond, root) as service:
                service.settings = SearchSettings('deepseek', 'key2')
                result = await service.execute('search_web', {'query': 'query', 'count': '2'})
            self.assertNotIn('fake-secret', json.dumps(result))
            self.assertNotIn('fake-search-secret', json.dumps(result))
            self.assertNotIn('content', result['results'][0])
            self.assertIsNone(result['results'][0]['published_at'])

    async def test_missing_api_key_does_not_make_network_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            async with await self.tool('deepseek', lambda r: self.fail('unexpected request'), Path(temporary)) as service:
                result = await service.execute('search_web', {'query': 'query'})
        self.assertEqual(result['error'], 'search_key_missing')

    async def test_cancel_propagates_and_closes_network_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            started = asyncio.Event()
            async def respond(request):
                started.set()
                await asyncio.Event().wait()
            service = await self.tool('deepseek', respond, Path(temporary))
            save_api_key('fake-secret', Path(temporary) / '.env')
            async def run():
                async with service:
                    await service.execute('search_web', {'query': 'query'})
            task = asyncio.create_task(run())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(service.client.is_closed)

    async def test_status_errors_do_not_leak_server_body_or_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_api_key('fake-secret', root / '.env')
            for status in (401, 402, 403, 404, 429, 500):
                async with await self.tool('deepseek', lambda r: httpx.Response(status, text='fake-secret'), root) as service:
                    result = await service.execute('search_web', {'query': 'query'})
                    self.assertEqual(result['error'], f'web_http_{status}')
                    self.assertNotIn('fake-secret', json.dumps(result))

    async def test_fetch_marks_truncation_and_preserves_real_source(self):
        body = '<main><p>' + '内容' * MAX_CONTENT + '</p></main>'
        with tempfile.TemporaryDirectory() as temporary:
            async with await self.tool('free', lambda r: httpx.Response(200, text=body, headers={'content-type': 'text/html'}), Path(temporary)) as service:
                with patch('dianstar.tools.web_reader.pinned_url', new=AsyncMock(side_effect=lambda url, **kwargs: url)):
                    result = await service.execute('read_webpage', {'url': 'https://example.com/a'})
        self.assertTrue(result['truncated'])
        self.assertTrue(result['untrusted'])
        self.assertEqual(len(result['content']), MAX_CONTENT)
        self.assertEqual(result['final_url'], 'https://example.com/a')

    async def test_deepseek_fetch_does_not_invent_final_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_api_key('fake-secret', root / '.env')
            async with await self.tool('deepseek', lambda r: httpx.Response(200, json={'title': 'Title', 'content': '# Body'}), root) as service:
                result = await service.execute('read_webpage', {'url': 'https://example.com'})
        self.assertIsNone(result['final_url'])
        self.assertEqual(result['format'], 'markdown')

    async def test_empty_results_bad_parameters_budget_and_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_api_key('fake-secret', root / '.env')
            async with await self.tool('deepseek', lambda r: httpx.Response(200, json={'results': []}), root) as service:
                self.assertEqual((await service.execute('search_web', {'query': 'query'}))['results'], [])
                self.assertEqual((await service.execute('search_web', {'query': ''}))['error'], 'invalid_query')
                self.assertEqual((await service.execute('search_web', {'query': 'q', 'count': '21'}))['error'], 'invalid_count')
                await service.execute('search_web', {'query': 'fourth'})
                service.operations = MAX_WEB_OPERATIONS
                self.assertEqual((await service.execute('read_webpage', {'url': 'https://example.com'}))['error'], 'web_call_limit')
            async with await self.tool('disabled', lambda r: self.fail('unexpected request'), root) as service:
                self.assertEqual((await service.execute('search_web', {'query': 'q'}))['error'], 'search_disabled')

    async def test_oversized_response_malformed_json_and_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_api_key('fake-secret', root / '.env')
            handlers = [lambda r: httpx.Response(200, content=b'x' * (MAX_BYTES + 1)),
                        lambda r: httpx.Response(200, text='invalid json'),
                        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout('fake-secret'))]
            for handler, code in zip(handlers, ('response_too_large', 'invalid_web_response', 'web_timeout')):
                async with await self.tool('deepseek', handler, root) as service:
                    result = await service.execute('search_web', {'query': 'q'})
                    self.assertEqual(result['error'], code)
                    self.assertNotIn('fake-secret', json.dumps(result))


class WebIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @patch('dianstar.deepseek.AsyncOpenAI')
    @patch('dianstar.agent.WebTools.execute', new_callable=AsyncMock)
    async def test_registry_search_fetch_roundtrip_preserves_sources_and_model(self, execute, client_class):
        def action(name, args, call_id):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', message=SimpleNamespace(
                content=None, tool_calls=[SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))]))])
        client = client_class.return_value
        client.chat.completions.create = AsyncMock(side_effect=[
            action('search_web', {'query': '资料'}, 'search'),
            action('read_webpage', {'url': 'https://example.com/a'}, 'fetch'),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='结论。[来源](https://example.com/a)'))])])
        execute.side_effect = [{'results': [{'url': 'https://example.com/a', 'summary': '线索'}]},
                               {'url': 'https://example.com/a', 'content': '原文', 'untrusted': True}]
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            save_api_key('fake-model-key', env)
            history = []
            answer = await ask_deepseek('查资料', env_file=env, history=history)
        requests = client.chat.completions.create.await_args_list
        self.assertIn(SEARCH_TOOL, requests[0].kwargs['tools'])
        self.assertIn(FETCH_TOOL, requests[0].kwargs['tools'])
        self.assertTrue(all(call.kwargs['model'] == 'deepseek-flash' for call in requests))
        self.assertIn('https://example.com/a', answer)
        self.assertEqual([m['tool_call_id'] for m in history if m['role'] == 'tool'], ['search', 'fetch'])

    @patch('dianstar.deepseek.AsyncOpenAI')
    async def test_disabled_web_tools_not_advertised(self, client_class):
        client_class.return_value.chat.completions.create = AsyncMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='你好'))]))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            save_api_key('fake-key', root / '.env')
            save_search_settings('disabled', 'primary', path=root / '.local/search.json',
                                 config_file=root / '.local/config.json', env_file=root / '.env')
            await ask_deepseek('你好', env_file=root / '.env')
        tools = client_class.return_value.chat.completions.create.await_args.kwargs['tools']
        self.assertNotIn(SEARCH_TOOL, tools)
        self.assertNotIn(FETCH_TOOL, tools)

    async def test_config_has_no_web_access_page(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, env = root / '.local/config.json', root / '.env'
            app = DianStarApp(Conversation.create(root / 'conversations'), config_file=config, env_file=env)
            async with app.run_test(size=(100, 35)) as pilot:
                app.push_screen(ConfigScreen(config_file=config, env_file=env))
                await pilot.pause()
                self.assertEqual(len(app.screen.query_one('#config-menu', ListView).children), 4)
                self.assertFalse(app.screen.query('#menu-search'))
                self.assertFalse(app.screen.query('#search-page'))
                self.assertEqual(load_model_settings(config).default_model, 'deepseek-flash')
