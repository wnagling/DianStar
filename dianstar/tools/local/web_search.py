"""Bounded free search adapters. Engine failure is not evidence that paid data is needed."""

import re
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit, unquote
from xml.etree import ElementTree

import httpx

from dianstar.tools.local.web_reader import WebError, bounded_body, public_url


class FreeSearchError(WebError):
    def __init__(self, code: str, message: str, attempts: list[dict]):
        super().__init__(code, message)
        self.attempts = attempts


def filter_source_scope(query: str, results: list[dict]) -> list[dict]:
    """Check site operators against source URLs, including on API responses."""
    original_results = results
    scopes = []
    for match in re.finditer(r"(?<!\S)(-?)site:([^\s]+)", query, re.I):
        target = match[2].strip("\"'")
        parsed = urlsplit(target if "://" in target else "https://" + target)
        if parsed.hostname:
            scopes.append((bool(match[1]), parsed.hostname.lower().rstrip('.'), parsed.path))
    if scopes:
        def in_scope(result):
            source = urlsplit(str(result.get("url", "")))
            host = (source.hostname or "").lower().rstrip('.')
            included = []
            for excluded, domain, path in scopes:
                matches = (host == domain or host.endswith('.' + domain)) and source.path.startswith(path)
                if excluded and matches:
                    return False
                if not excluded:
                    included.append(matches)
            return not included or any(included)
        results = [result for result in results if in_scope(result)]
        if original_results and not results:
            raise WebError("search_low_relevance", "免费搜索结果不符合限定来源，不能作为相关来源；请调整免费查询。")
    return results


def filter_search_noise(query: str, results: list[dict]) -> list[dict]:
    """Lexical screening, not semantic verification. Dates alone are not relevance."""
    results = filter_source_scope(query, results)
    query = re.sub(r"-?site:\S+|(?:before|after|lang):\S+", " ", query)
    ignored = set("about against through with from find search best latest official website paper papers research arxiv match matches schedule schedules today tomorrow yesterday october january february march april may june july august september november december query test top".split())
    latin = set(re.findall(r"[a-z][a-z0-9_-]{2,}", query.lower())) - ignored
    acronyms = [term.lower() for term in re.findall(r"(?<![A-Za-z])[A-Z]{3,8}(?![A-Za-z])", query)
                if term.lower() in latin]
    ignored_cjk = {"今天", "今日", "明天", "昨天", "搜索", "查询", "查找", "最新", "官方网站", "测试"}
    runs = [word for word in re.findall(r"[\u4e00-\u9fff]{2,}", query) if word not in ignored_cjk]
    cjk = {word[i:i + 2] for word in runs for i in range(len(word) - 1)}
    if not latin and not cjk:
        return results
    kept = []
    for result in results:
        source = urlsplit(str(result.get("url", "")))
        # Tracking/search parameters can echo the query on an unrelated page.
        text = " ".join((str(result.get("title", "")), str(result.get("summary", "")),
                         source.hostname or "", unquote(source.path))).lower()
        hits = {term for term in latin if re.search(rf"(?<![a-z0-9_]){re.escape(term)}(?![a-z0-9_])", text)}
        if latin:
            relevant = len(hits) >= min(2, len(latin))
            if acronyms:
                relevant = relevant and acronyms[0] in hits
        else:
            relevant = sum(term in text for term in cjk) >= min(2, len(cjk))
        if relevant:
            kept.append(result)
    if results and not kept:
        raise WebError("search_low_relevance", "免费搜索结果没有命中主要检索词，不能作为相关来源；请改写关键词或限定来源，不应仅因此申请付费。")
    return kept


def normalize_items(items: list[dict]) -> list[dict]:
    results, seen = [], set()
    for item in items[:50]:
        if not isinstance(item, dict):
            raise ValueError
        try:
            url = str(public_url(item["url"]))
        except WebError:
            continue
        title, summary = item["title"], item.get("summary") or item.get("snippet", "")
        if not isinstance(title, str) or not isinstance(summary, str):
            raise ValueError
        if not title.strip() or url in seen:
            continue
        seen.add(url)
        published = item.get("date")
        results.append({"title": title[:300], "url": url, "summary": summary[:1000],
                        "published_at": published[:100] if isinstance(published, str) else None})
    return results


class _SoResults(HTMLParser):
    """Extract organic result headings/snippets; no JS, ads, or redirect following."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.items = []
        self.record = None
        self.record_depth = self.heading_depth = self.summary_depth = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag in self.VOID:
            return
        self.stack.append(tag)
        if len(self.stack) > 256:
            raise ValueError("Search markup too deep")
        depth = len(self.stack)
        if tag == "li" and "res-list" in classes:
            self._finish()
            self.record = {"title": [], "summary": [], "url": ""}
            self.record_depth = depth
        if self.record is None:
            return
        if tag == "h3":
            self.heading_depth = depth
        if "res-desc" in classes:
            self.summary_depth = depth
        if tag == "a" and self.heading_depth and not self.record["url"]:
            self.record["url"] = attrs.get("data-mdurl") or attrs.get("href", "")

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag not in self.stack:
            return
        depth = len(self.stack) - self.stack[::-1].index(tag)
        if self.heading_depth is not None and depth <= self.heading_depth:
            self.heading_depth = None
        if self.summary_depth is not None and depth <= self.summary_depth:
            self.summary_depth = None
        if self.record_depth is not None and depth <= self.record_depth:
            self._finish()
        del self.stack[depth - 1:]

    def handle_data(self, data):
        if self.record is None or "script" in self.stack or "style" in self.stack:
            return
        if self.heading_depth:
            self.record["title"].append(data)
        elif self.summary_depth:
            self.record["summary"].append(data)

    def _finish(self):
        if self.record and len(self.items) < 50:
            self.items.append({"title": " ".join(self.record["title"]).strip(),
                               "summary": " ".join(self.record["summary"]).strip(), "url": self.record["url"]})
        self.record = None
        self.record_depth = self.heading_depth = self.summary_depth = None


def parse_so(body: str) -> list[dict]:
    parser = _SoResults()
    parser.feed(body)
    parser.close()
    parser._finish()
    if parser.items:
        return parser.items
    if re.search(r"<title[^>]*>[^<]*(?:验证|captcha|verification)", body, re.I):
        raise WebError("search_blocked", "免费搜索要求验证，未取得结果。")
    if re.search(r"没有找到|未找到|没有相关结果|class=[\"'][^\"']*no-result", body):
        return []
    raise WebError("search_response_invalid", "免费搜索页面结构无法解析，未取得结果。")


async def free_search(client: httpx.AsyncClient, query: str, limit: int) -> tuple[list[dict], list[dict]]:
    attempts = []
    engines = (
        ("bing", "https://www.bing.com/search?" + urlencode({"q": query, "format": "rss", "count": limit})),
        ("360", "https://www.so.com/s?" + urlencode({"q": query, "pn": "1"})),
    )
    for engine, url in engines:
        try:
            # Fixed hosts only. Never follow a search service redirect to login,
            # captcha or an arbitrary destination; no credential headers sent.
            client.cookies.clear()
            async with client.stream("GET", url, timeout=8) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    raise WebError("search_redirect", "免费搜索入口发生跳转，未跟随到其他地址。")
                if response.status_code == 403:
                    raise WebError("search_blocked", "免费搜索受到访问限制，未取得结果。")
                response.raise_for_status()
                if response.status_code != 200:
                    raise WebError("search_response_invalid", "免费搜索尚未返回完整结果。")
                body = await bounded_body(response)
                encoding = response.encoding or "utf-8"
            if engine == "bing":
                if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
                    if re.search(rb"<title[^>]*>[^<]*(?:captcha|verification)", body, re.I):
                        raise WebError("search_blocked", "免费搜索返回了验证页，未取得搜索结果。")
                    raise WebError("search_response_invalid", "免费搜索未返回可解析的 RSS，未取得搜索结果。")
                root = ElementTree.fromstring(body)
                if root.tag != "rss" or root.find("channel") is None:
                    raise WebError("search_response_invalid", "免费搜索返回了不支持的格式。")
                items = [{"title": item.findtext("title", ""), "url": item.findtext("link", ""),
                          "summary": item.findtext("description", "")} for item in root.findall("./channel/item")]
            else:
                items = parse_so(body.decode(encoding, errors="replace"))
            results = filter_search_noise(query, normalize_items(items))
            attempts.append({"engine": engine, "status": "ok" if results else "empty", "count": len(results)})
            if results:
                return results[:limit], attempts
        except WebError as error:
            attempts.append({"engine": engine, "status": error.code})
        except (httpx.TimeoutException, TimeoutError):
            attempts.append({"engine": engine, "status": "web_timeout"})
        except httpx.HTTPStatusError as error:
            attempts.append({"engine": engine, "status": f"web_http_{error.response.status_code}"})
        except (httpx.HTTPError, OSError):
            attempts.append({"engine": engine, "status": "web_unavailable"})
        except (ValueError, TypeError, KeyError, LookupError, ElementTree.ParseError):
            attempts.append({"engine": engine, "status": "search_response_invalid"})
    statuses = {attempt["status"] for attempt in attempts}
    if statuses == {"empty"}:
        return [], attempts
    if statuses == {"search_blocked"}:
        raise FreeSearchError("search_blocked", "两个免费搜索入口均受访问限制；请先尝试其他具体查询或已有公开来源。", attempts)
    if "search_low_relevance" in statuses:
        raise FreeSearchError("search_low_relevance", "已尝试两个免费搜索入口，但结果未命中主要检索词。此类检索质量问题不触发付费；请改写查询或限定来源。", attempts)
    raise FreeSearchError("free_search_unavailable", "免费搜索未取得可用结果，可能有网络或解析问题；不会仅因此申请付费。", attempts)
