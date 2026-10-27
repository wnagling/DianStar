"""Free search quality and paid-escalation regression; no real API calls."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

import httpx

from dianstar.config.credentials import save_api_key
from dianstar.config.search import SearchSettings
from dianstar.tools.local.web import WebTools
from dianstar.tools.local.web_search import filter_search_noise, parse_so
from dianstar.tools.local.web_reader import WebError


SO_RESULT = '''<!DOCTYPE html><html><title>Search</title><ul>
<li class="res-list" data-result-type="normal"><h3 class="res-title">
<a href="https://www.so.com/link?m=tracking" data-mdurl="https://example.com/tes-schedule"><em>TES</em> 滔搏比赛赛程</a></h3>
<p class="res-desc">2026 年 10 月 26 日，查看 TES 队伍比赛安排。</p></li>
</ul></html>'''
NOISE_RSS = '<rss><channel><item><title>Wikipedia</title><link>https://en.wikipedia.org/wiki/Main_Page</link><description>Free encyclopedia</description></item></channel></rss>'


class SearchQualityTests(unittest.TestCase):
    def test_chinese_acronyms_and_mixed_queries_reject_the_reported_noise(self):
        noise = [{"title": "英雄联盟官方网站", "url": "https://lol.qq.com/main.shtml", "summary": "英雄联盟游戏介绍"},
                 {"title": "Wikipedia", "url": "https://www.wikipedia.org/", "summary": "Free encyclopedia"}]
        for query in ("英雄联盟 TES 今日 赛程 2026年10月26日", "滔搏 战队 赛程 全球总决赛 瑞士轮", "TES Top Esports match schedule October 26 2026"):
            with self.subTest(query=query), self.assertRaises(WebError):
                filter_search_noise(query, noise)
        relevant = {"title": "TES赛程公布", "url": "https://example.com/matches", "summary": "滔搏比赛安排"}
        self.assertEqual(filter_search_noise("英雄联盟 TES 今日 赛程", noise + [relevant]), [relevant])
        self.assertEqual(filter_search_noise("滔搏 赛程", [relevant]), [relevant])

    def test_dates_and_acronym_substrings_are_not_relevance(self):
        for title in ("Latest news 2026 10 26", "Windows update October 26 2026", "Contest schedules"):
            with self.assertRaises(WebError):
                filter_search_noise("TES match schedule October 26 2026", [{"title": title}])

    def test_site_constraints_are_checked_against_actual_source_domains(self):
        official = {'title':'Python tutorial','url':'https://docs.python.org/3/tutorial/index.html'}
        other = {'title':'Python tutorial','url':'https://www.w3schools.com/python/'}
        deceptive = {'title':'Python tutorial','url':'https://docs.python.org.evil.example/tutorial'}
        self.assertEqual(filter_search_noise('Python tutorial site:python.org', [other, official, deceptive]), [official])
        self.assertEqual(filter_search_noise('Python tutorial -site:python.org', [other, official]), [other])
        with self.assertRaises(WebError):
            filter_search_noise('Python tutorial site:docs.python.org', [other])

    def test_search_tracking_query_echo_is_not_a_topic_match(self):
        with self.assertRaises(WebError):
            filter_search_noise('TES schedule', [{'title':'Wikipedia','url':'https://example.com/home?q=TES+schedule'}])

    def test_html_parser_uses_direct_source_and_does_not_collect_scripts_or_other_people_searches(self):
        result = parse_so(SO_RESULT + '<h3><a href="https://example.org/noise">其他人还搜了</a></h3>')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["url"], "https://example.com/tes-schedule")
        self.assertIn("TES", result[0]["title"])
        self.assertIn("2026", result[0]["summary"])

    def test_challenge_unknown_markup_and_explicit_empty_are_different(self):
        with self.assertRaises(WebError) as blocked:
            parse_so('<html><title>安全验证</title></html>')
        self.assertEqual(blocked.exception.code, 'search_blocked')
        with self.assertRaises(WebError) as invalid:
            parse_so('<html><title>Changed layout</title><p>Unknown</p></html>')
        self.assertEqual(invalid.exception.code, 'search_response_invalid')
        self.assertEqual(parse_so('<p>没有找到相关结果</p>'), [])


class FreeSearchPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_irrelevant_rss_uses_real_free_alternative_without_permission_or_payment(self):
        requests = []
        def respond(request):
            requests.append(request)
            self.assertNotIn('authorization', request.headers)
            if request.url.host == 'www.bing.com':
                return httpx.Response(200, text=NOISE_RSS)
            self.assertEqual(request.url.host, 'www.so.com')
            self.assertEqual(request.url.params['q'], 'TES 滔搏 赛程')
            return httpx.Response(200, text=SO_RESULT)
        approve = AsyncMock()
        async with WebTools(SearchSettings(), Path('unused'), approve) as service:
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
            result = await service.execute('search_web', {'query': 'TES 滔搏 赛程'})
            self.assertEqual(result['provider'], 'free')
            self.assertEqual(result['results'][0]['url'], 'https://example.com/tes-schedule')
            self.assertEqual([item['engine'] for item in result['free_search_attempts']], ['bing', '360'])
            self.assertNotIn('api_fallback', result)
            self.assertEqual(service.paid_calls, 0)
            approve.assert_not_awaited()
        self.assertEqual(len(requests), 2)

    async def test_repeated_bad_results_never_become_paid_eligibility_even_with_persistent_allow(self):
        approve = AsyncMock()
        def respond(request):
            self.assertNotEqual(request.url.host, 'api.deepseek.com')
            if request.url.host == 'www.bing.com':
                return httpx.Response(200, text=NOISE_RSS)
            return httpx.Response(200, text=SO_RESULT.replace('TES', 'Other').replace('tes-schedule', 'encyclopedia').replace('滔搏', '其他').replace('赛程', '介绍'))
        async with WebTools(SearchSettings(paid_policy='allow'), Path('unused'), approve) as service:
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
            for query in ('TES match schedule', 'TES Top Esports schedule', 'TES 今日 赛程'):
                result = await service.execute('search_web', {'query': query})
                self.assertEqual(result['error'], 'search_low_relevance')
                self.assertEqual(len(result['free_search_attempts']), 2)
                self.assertNotIn('api_fallback', result)
            approve.assert_not_awaited()
            self.assertEqual(service.paid_calls, 0)

    async def test_timeout_or_redirect_never_follows_arbitrary_destination(self):
        requests = []
        def respond(request):
            requests.append(str(request.url))
            if request.url.host == 'www.bing.com':
                raise httpx.ReadTimeout('private detail')
            return httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        async with WebTools(SearchSettings(), Path('unused')) as service:
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
            result = await service.execute('search_web', {'query': 'TES schedule'})
            self.assertEqual(result['error'], 'free_search_unavailable')
            self.assertNotIn('api_fallback', result)
            self.assertNotIn('private detail', json.dumps(result))
        self.assertEqual(len(requests), 2)

    async def test_useful_primary_result_does_not_send_query_to_another_service(self):
        requests = []
        def respond(request):
            requests.append(request)
            self.assertEqual(request.url.host, 'www.bing.com')
            return httpx.Response(200, text='<rss><channel><item><title>TES schedule</title><link>https://example.com/matches</link></item></channel></rss>')
        async with WebTools(SearchSettings(), Path('unused')) as service:
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
            result = await service.execute('search_web', {'query': 'TES schedule'})
            self.assertEqual(len(result['results']), 1)
            self.assertEqual(len(result['free_search_attempts']), 1)
        self.assertEqual(len(requests), 1)

    async def test_plain_redirects_are_not_evidence_of_paid_service_necessity(self):
        async with WebTools(SearchSettings(paid_policy='allow'), Path('unused')) as service:
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(302, headers={'location': 'https://example.com/landing'})), follow_redirects=False)
            for query in ('TES schedule', 'Top Esports matches'):
                result = await service.execute('search_web', {'query': query})
                self.assertEqual(result['error'], 'free_search_unavailable')
                self.assertNotIn('api_fallback', result)
            self.assertEqual(service.paid_calls, 0)

    async def test_both_engines_restricted_need_two_search_queries_before_paid_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / '.env'
            save_api_key('fake-key', env)
            approve = AsyncMock(return_value='turn')
            requests = []
            def respond(request):
                requests.append(request)
                if request.url.host == 'api.deepseek.com':
                    return httpx.Response(200, json={'results': [{'title': 'TES schedule', 'url': 'https://example.com/matches'}]})
                return httpx.Response(403, text='no access')
            async with WebTools(SearchSettings(), env, approve) as service:
                service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
                query = {'query': 'TES schedule'}
                first = await service.execute('search_web', query)
                continued = {**query, 'api_fallback': first['api_fallback']}
                service._free_paths.add(('read_webpage', 'https://example.com'))
                await service.execute('search_web', continued)
                approve.assert_not_awaited()
                await service.execute('search_web', {'query': 'Top Esports matches'})
                result = await service.execute('search_web', continued)
                approve.assert_awaited_once()
                self.assertEqual(result['provider'], 'deepseek')
                self.assertEqual(sum(r.url.host == 'api.deepseek.com' for r in requests), 1)

    async def test_unsafe_source_links_are_not_returned_or_visited(self):
        def respond(request):
            return httpx.Response(200, text=NOISE_RSS if request.url.host == 'www.bing.com' else
                SO_RESULT.replace('https://example.com/tes-schedule', 'http://127.0.0.1/private'))
        async with WebTools(SearchSettings(), Path('unused')) as service:
            service._client = httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=False)
            result = await service.execute('search_web', {'query': 'TES schedule'})
            self.assertNotIn('results', result)
            self.assertNotIn('api_fallback', result)
