"""工具来源台账与引用检查；记录获取证据，不判断结论是否真实。"""

import json
import re
from urllib.parse import urlsplit

from markdown_it import MarkdownIt

from .tools.web_reader import WebError, public_url


def canonical_url(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(public_url(value).copy_with(fragment=None))
    except (ValueError, WebError):
        return None


def page_evidence(url: str, content: str, truncated: bool) -> dict:
    """Recognize a few known page routes; never infer complete papers from length."""
    parsed = urlsplit(url)
    host, path = parsed.hostname, parsed.path
    arxiv = host in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}
    kind, basis = "page_text", "extracted_page_text"
    if (arxiv and path.startswith("/abs/")) or (
            host == "pubmed.ncbi.nlm.nih.gov" and re.fullmatch(r"/\d+/?", path)):
        kind, basis = "abstract", "known_abstract_route"
    elif ((arxiv and path.startswith(("/html/", "/pdf/"))) or (
            host in {"ar5iv.labs.arxiv.org", "ar5iv.org"} and path.startswith("/html/"))):
        if re.search(r"(?im)^\s*(?:#{1,6}\s*)?(?:(?:1|I)[.\s]+)?Introduction\b", content):
            kind, basis = "fulltext_page", "known_fulltext_route_with_introduction"
    return {"kind": kind, "basis": basis, "truncated": bool(truncated),
            "complete_document_verified": False}


def cited_urls(answer: str) -> list[str]:
    """Read Markdown links, references, autolinks and bare URLs, excluding code."""
    found = []
    for block in MarkdownIt().enable("table").parse(answer):
        link_depth = 0
        for token in block.children or []:
            if token.type == "link_open":
                link_depth += 1
                href = token.attrGet("href")
                if isinstance(href, str) and href.lower().startswith(("https://", "http://")):
                    found.append(href)
            elif token.type == "link_close":
                link_depth = max(0, link_depth - 1)
            elif token.type == "text" and not link_depth:
                for match in re.finditer(r"https?://[^\s<>\"\u3000-\u303f\uff00-\uffef]+", token.content):
                    url = match.group().rstrip(".,;:!?")
                    while url.endswith(")") and url.count(")") > url.count("("):
                        url = url[:-1]
                    found.append(url)
    seen, unique = set(), []
    for url in found:
        key = canonical_url(url) or url
        if key not in seen:
            seen.add(key)
            unique.append(url)
    return unique


class SourceEvidence:
    def __init__(self, history: list[dict] | None = None):
        self.sources = {}
        # 只恢复有工具调用对应的工具结果，不采信历史回答或用户粘贴的链接。
        calls = {}
        for message in history or []:
            if message.get("role") == "assistant":
                for call in message.get("tool_calls", []):
                    function = call.get("function", {})
                    if (function.get("name") in {"search_web", "read_webpage"}
                            or str(function.get("name", "")).startswith("mcp_github__")):
                        try:
                            arguments = json.loads(function.get("arguments", "{}"))
                            if isinstance(arguments, dict):
                                calls[call.get("id")] = (function["name"], arguments)
                        except (TypeError, ValueError):
                            pass
            elif message.get("role") == "tool":
                call = calls.pop(message.get("tool_call_id"), None)
                if call is not None:
                    try:
                        result = json.loads(message.get("content", "{}"))
                        if isinstance(result, dict):
                            self.observe(*call, result, historical=True)
                    except (TypeError, ValueError):
                        pass

    def _record(self, urls: list[str], evidence: dict, historical: bool) -> None:
        rank = {"unavailable": 0, "search_summary": 1, "page_text": 2,
                "abstract": 2, "fulltext_page": 3, "github_api": 2}
        record = {**evidence, "historical": historical}
        for url in urls:
            key = canonical_url(url)
            if key is None:
                continue
            previous = self.sources.get(key)
            score = (rank.get(record["kind"], 0), not record.get("truncated", False), not historical)
            old_score = ((rank.get(previous["kind"], 0), not previous.get("truncated", False),
                          not previous["historical"]) if previous else (-1, False, False))
            if score >= old_score:
                self.sources[key] = record

    def observe(self, name: str, arguments: dict, result: dict, *, historical: bool = False) -> dict:
        if result.get("error"):
            if name == "read_webpage":
                evidence = {"kind": "unavailable", "error": result["error"]}
                self._record([arguments.get("url")], evidence, historical)
                return {**result, "evidence": evidence}
            return result
        if name.startswith("mcp_github__") and result.get("server") == "github":
            evidence = {"kind": "github_api", "truncated": bool(result.get("truncated")),
                        "complete_document_verified": False}
            self._record(result.get("sources", []), evidence, historical)
            return {**result, "evidence": evidence}
        if name == "search_web" and isinstance(result.get("results"), list):
            evidence = {"kind": "search_summary", "complete_document_verified": False}
            items = []
            for item in result["results"]:
                if isinstance(item, dict):
                    self._record([item.get("url")], evidence, historical)
                    items.append({**item, "evidence": evidence})
            return {**result, "results": items}
        if name == "read_webpage" and isinstance(result.get("content"), str) and result["content"].strip():
            requested = result.get("url") or arguments.get("url")
            final = result.get("final_url")
            checked = canonical_url(final or requested)
            if checked is not None:
                evidence = page_evidence(checked, result["content"], result.get("truncated", False))
                self._record([requested, final], evidence, historical)
                return {**result, "evidence": evidence}
        return result

    def annotate_answer(self, answer: str) -> str:
        urls = cited_urls(answer)
        if not urls:
            return answer
        notes = []
        for index, url in enumerate(urls, 1):
            record = self.sources.get(canonical_url(url))
            if record is None:
                label = "未见成功检索或读取记录，引用未核验"
            elif record["kind"] == "unavailable":
                label = "读取失败，未取得正文，不能作为已读来源"
            elif record["kind"] == "search_summary":
                label = "仅搜索摘要，未读取目标正文"
            elif record["kind"] == "abstract":
                label = "已读摘要页，不能据此核实全文和实验细节"
            elif record["kind"] == "fulltext_page":
                label = "已取得正文页面，未据此确认论文完整性"
            elif record["kind"] == "github_api":
                label = "已取得 GitHub API 对象资料；不表示已读取其中所有评论、文件或链接"
            else:
                label = "已取得网页文本，是否为全文尚未确认"
            if record and record.get("truncated"):
                label += "；内容已截断，仅取得片段"
            if record and record.get("historical"):
                label += "；来自历史工具记录，非本轮新取得"
            # 按首次出现顺序去重；展示链接文本，核验注记本身不重新创建超链接。
            display = url.replace("`", "")
            if len(display) > 120:
                display = display[:117] + "..."
            notes.append(f"- 来源 {index}（`{display}`）：{label}。")
        return answer.rstrip() + "\n\n来源核验（按链接首次出现顺序，仅核对获取记录）：\n" + "\n".join(notes)
