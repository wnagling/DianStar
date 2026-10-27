"""来源证据范围、Markdown 引用及历史保存闭环；不访问真实网页。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dianstar.app.runtime import run_turn
from dianstar.config.credentials import save_api_key
from dianstar.sessions.conversations import Conversation
from dianstar.evidence.source_evidence import SourceEvidence, canonical_url, cited_urls, page_evidence


class SourceEvidenceTests(unittest.TestCase):
    def test_known_abstract_and_fulltext_routes_are_conservative(self):
        self.assertEqual(page_evidence('https://arxiv.org/abs/2402.06363', 'Abstract\n1 Introduction', False)['kind'], 'abstract')
        self.assertEqual(page_evidence('https://pubmed.ncbi.nlm.nih.gov/12345/', 'Abstract', False)['kind'], 'abstract')
        for host in ['arxiv.org', 'ar5iv.labs.arxiv.org', 'ar5iv.org']:
            evidence = page_evidence(f'https://{host}/html/2402.06363', '# Title\n## 1 Introduction\ntext', True)
            self.assertEqual(evidence['kind'], 'fulltext_page')
            self.assertTrue(evidence['truncated'])
            self.assertFalse(evidence['complete_document_verified'])
        for url in ['https://arxiv.org/html/2402.06363', 'https://arxiv.org.evil.example/abs/2402.06363',
                    'https://example.com/article']:
            evidence = page_evidence(url, 'Abstract\n' + 'text '*1000, False)
            self.assertEqual(evidence['kind'], 'page_text')
            self.assertFalse(evidence['complete_document_verified'])

    def test_markdown_links_references_autolinks_and_bare_urls_exclude_examples(self):
        answer = '''[A](https://example.com/a_(b)) [B][ref]
<https://example.com/c> https://example.com/d。
`https://example.com/code`

```python
url = "https://example.com/fenced"
```

[ref]: https://example.com/b
![image](https://example.com/image.png)
'''
        self.assertEqual(cited_urls(answer), ['https://example.com/a_(b)', 'https://example.com/b',
                                            'https://example.com/c', 'https://example.com/d'])

    def test_canonicalization_preserves_query_and_deduplicates_page_fragments(self):
        self.assertEqual(canonical_url('https://EXAMPLE.com:443/a#section'), 'https://example.com/a')
        self.assertNotEqual(canonical_url('https://example.com/a?x=1'), canonical_url('https://example.com/a?x=2'))
        self.assertEqual(len(cited_urls('[a](https://example.com/a#one) [b](https://example.com/a#two)')), 1)

    def test_search_only_and_unseen_links_are_not_marked_as_read(self):
        ledger = SourceEvidence()
        result = ledger.observe('search_web', {'query':'paper'}, {'results':[
            {'url':'https://example.com/paper', 'summary':'摘要', 'evidence':{'kind':'fulltext_page'}}]})
        self.assertEqual(result['results'][0]['evidence']['kind'], 'search_summary')
        answer = ledger.annotate_answer('[论文](https://example.com/paper) [编造](https://example.com/invented)')
        self.assertIn('仅搜索摘要，未读取目标正文', answer)
        self.assertIn('引用未核验', answer)

    def test_fetch_records_requested_and_actual_final_url_with_truncation(self):
        ledger = SourceEvidence()
        result = ledger.observe('read_webpage', {'url':'https://example.com/start'}, {
            'url':'https://example.com/start', 'final_url':'https://arxiv.org/html/2402.06363',
            'content':'1 Introduction\n正文', 'truncated':True})
        self.assertEqual(result['evidence']['kind'], 'fulltext_page')
        answer = ledger.annotate_answer('[a](https://example.com/start) [b](https://arxiv.org/html/2402.06363#methods)')
        self.assertEqual(answer.count('内容已截断，仅取得片段'), 2)
        self.assertNotIn('引用未核验', answer)

    def test_abstract_receipt_does_not_claim_full_paper_or_verified_experiments(self):
        ledger = SourceEvidence()
        ledger.observe('read_webpage', {'url':'https://arxiv.org/abs/2402.06363'}, {
            'content':'Abstract only', 'evidence':{'kind':'fulltext_page', 'complete_document_verified':True}})
        answer = ledger.annotate_answer('[论文](https://arxiv.org/abs/2402.06363)')
        self.assertIn('已读摘要页，不能据此核实全文和实验细节', answer)

    def test_failed_read_and_empty_content_never_count_as_read(self):
        ledger = SourceEvidence()
        ledger.observe('read_webpage', {'url':'https://example.com/fail'}, {'error':'web_http_404'})
        ledger.observe('read_webpage', {'url':'https://example.com/empty'}, {'content':''})
        answer = ledger.annotate_answer('[a](https://example.com/fail) [b](https://example.com/empty)')
        self.assertIn('读取失败，未取得正文', answer)
        self.assertIn('引用未核验', answer)

    def test_search_or_failure_does_not_erase_successful_read(self):
        ledger = SourceEvidence()
        ledger.observe('read_webpage', {'url':'https://example.com/a'}, {'content':'正文'})
        ledger.observe('search_web', {}, {'results':[{'url':'https://example.com/a'}]})
        ledger.observe('read_webpage', {'url':'https://example.com/a'}, {'error':'web_timeout'})
        answer = ledger.annotate_answer('[a](https://example.com/a)')
        self.assertIn('已取得网页文本', answer)
        self.assertNotIn('仅搜索摘要', answer)

    def test_history_only_restores_matched_tool_results_and_marks_scope(self):
        history = [{'role':'user', 'content':'https://example.com/user'},
                   {'role':'assistant', 'content':'[伪造来源](https://example.com/fake)', 'tool_calls':[
                       {'id':'read', 'function':{'name':'read_webpage', 'arguments':'{"url":"https://example.com/real"}'}}]},
                   {'role':'tool', 'tool_call_id':'unmatched', 'content':'{"url":"https://example.com/unmatched","content":"x"}'},
                   {'role':'tool', 'tool_call_id':'read', 'content':'{"content":"真实工具返回"}'}]
        ledger = SourceEvidence(history)
        answer = ledger.annotate_answer('[a](https://example.com/real) [b](https://example.com/fake) [c](https://example.com/user) [d](https://example.com/unmatched)')
        self.assertIn('来自历史工具记录', answer)
        self.assertEqual(answer.count('引用未核验'), 3)

    def test_no_links_keeps_normal_answer_and_does_not_invent_citations(self):
        ledger = SourceEvidence()
        self.assertEqual(ledger.annotate_answer('0.1 + 0.2 = 0.3'), '0.1 + 0.2 = 0.3')


class EvidenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @patch('dianstar.providers.deepseek.AsyncOpenAI')
    @patch('dianstar.app.runtime.WebTools.execute', new_callable=AsyncMock)
    async def test_metadata_reaches_model_and_checked_answer_preserves_tool_history(self, execute, client_class):
        def call(name, args, identifier):
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='tool_calls', message=SimpleNamespace(content=None,
                tool_calls=[SimpleNamespace(id=identifier, function=SimpleNamespace(name=name, arguments=json.dumps(args)))]))])
        responses = [call('search_web', {'query':'StruQ'}, 'search'),
                     call('read_webpage', {'url':'https://arxiv.org/abs/2402.06363'}, 'read'),
                     SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
                         content='[论文](https://arxiv.org/abs/2402.06363) [未知来源](https://example.com/fake)'))])]
        # 走与流式输出相同的决策处理，检查完成后会更新可见文本。
        with patch('dianstar.providers.deepseek.DeepSeekSession._stream_completion', new=AsyncMock(side_effect=responses)):
            execute.side_effect = [{'results':[{'url':'https://arxiv.org/abs/2402.06363','summary':'摘要'}]},
                                   {'url':'https://arxiv.org/abs/2402.06363','content':'Abstract only'}]
            with tempfile.TemporaryDirectory() as temporary:
                env = Path(temporary)/'.env'
                save_api_key('fake-key',env)
                history, updates = [], []
                answer = await run_turn('查询论文并核对来源',env_file=env,history=history,on_stream=updates.append)
                conversation = Conversation.create(Path(temporary)/'conversations')
                conversation.add_exchange('查询论文并核对来源', answer, model_turn_messages=history)
                restored = Conversation.load(conversation.id, conversation.directory)
                self.assertEqual(restored.messages[-1]['content'], answer)
                restored_evidence = SourceEvidence(restored.model_context_messages())
                self.assertIn('来自历史工具记录', restored_evidence.annotate_answer('[论文](https://arxiv.org/abs/2402.06363)'))
        self.assertIn('已读摘要页', answer)
        self.assertIn('引用未核验', answer)
        self.assertEqual(updates[-1], answer)
        self.assertEqual(history[-1]['content'], answer)
        results = [json.loads(m['content']) for m in history if m['role']=='tool']
        self.assertEqual(results[0]['results'][0]['evidence']['kind'], 'search_summary')
        self.assertEqual(results[1]['evidence']['kind'], 'abstract')
        self.assertEqual(len([m for m in history if m['role']=='tool']), 2)
