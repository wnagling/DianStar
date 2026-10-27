"""公开网页的有界读取与文本提取；DNS 检查后固定连接 IP，逐跳检查重定向。"""

import asyncio
import ipaddress
import json
import re
import socket
import ssl
from contextlib import asynccontextmanager
from contextvars import ContextVar
from html.parser import HTMLParser

import httpx

MAX_BYTES = 2 * 1024 * 1024
MAX_CONTENT = 16000
FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")
PUBLIC_DNS_URL = "https://cloudflare-dns.com/dns-query"
_TLS_TARGET = ContextVar("dianstar_web_tls_target", default=None)


class PinnedTLSContext(ssl.SSLContext):
    """Keep the original TLS identity when an HTTP proxy tunnels to a checked IP.

    httpcore's HTTP CONNECT path uses the IP as SNI, ignoring the request SNI
    extension. Override only that request's checked IP; certificate and hostname
    verification remain enabled. A distinct HTTPS proxy keeps its own identity.
    """

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        expected = _TLS_TARGET.get()
        if expected is not None and server_hostname == expected[0]:
            server_hostname = expected[1]
        return super().wrap_bio(incoming, outgoing, server_side=server_side,
                                server_hostname=server_hostname, session=session)


@asynccontextmanager
async def _public_stream(client: httpx.AsyncClient, target: httpx.URL, original: httpx.URL):
    token = _TLS_TARGET.set((target.host, original.host))
    try:
        async with client.stream("GET", target, headers={"Host": original.netloc.decode("ascii")},
                                 extensions={"sni_hostname": original.host}) as response:
            yield response
    finally:
        _TLS_TARGET.reset(token)


class WebError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def public_url(value: str) -> httpx.URL:
    try:
        if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 for c in value):
            raise ValueError
        url = httpx.URL(value)
        if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
                or url.port not in {None, 80, 443}):
            raise ValueError
        host = url.host.rstrip(".").lower()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise ValueError
        else:
            if (not address.is_global or address.is_multicast
                    or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None)):
                raise ValueError
        return url.copy_with(fragment=None)
    except (ValueError, TypeError, httpx.InvalidURL):
        raise WebError("invalid_url", "只支持不含登录凭据的公开 HTTP/HTTPS 网页和标准端口。") from None


def _is_public(address) -> bool:
    return (address.is_global and not address.is_multicast
            and not (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None))


async def _resolve_fake_ip(host: str, client: httpx.AsyncClient) -> list:
    """Resolve only a Fake-IP alias through a fixed HTTPS resolver, never trust the alias itself."""
    client.cookies.clear()
    async with client.stream("GET", PUBLIC_DNS_URL, params={"name": host, "type": "A"},
                             headers={"Accept": "application/dns-json"}) as response:
        if response.status_code != 200:
            raise WebError("dns_unavailable", "无法取得网页的真实公网地址，未访问虚拟或内网地址。")
        try:
            data = json.loads(await bounded_body(response, limit=65536))
            if data.get("Status") != 0 or data.get("TC", False):
                raise ValueError
            addresses = [ipaddress.ip_address(item["data"]) for item in data.get("Answer", [])
                         if item.get("type") == 1]
            if any(not isinstance(address, ipaddress.IPv4Address) for address in addresses):
                raise ValueError
        except (ValueError, TypeError, KeyError, AttributeError):
            raise WebError("dns_unavailable", "公网地址解析返回了无效结果，未读取网页。") from None
    return addresses


async def pinned_url(url: httpx.URL, *, resolver_client: httpx.AsyncClient | None = None) -> httpx.URL:
    records = await asyncio.get_running_loop().getaddrinfo(
        url.host, url.port or (443 if url.scheme == "https" else 80), type=socket.SOCK_STREAM)
    addresses = [ipaddress.ip_address(record[4][0]) for record in records]
    # 仅替换全为 Clash 常见虚拟网段的结果；真实内网和混合结果绝不重解析放行。
    if (resolver_client is not None and addresses
            and all(isinstance(ip, ipaddress.IPv4Address) and ip in FAKE_IP_RANGE for ip in addresses)):
        addresses = await _resolve_fake_ip(url.host, resolver_client)
    if not addresses or any(not _is_public(ip) for ip in addresses):
        raise WebError("blocked_address", "域名解析到本机、内网或保留地址，未读取网页。")
    return url.copy_with(host=str(addresses[0]))


async def bounded_body(response: httpx.Response, *, limit: int = MAX_BYTES) -> bytes:
    data = bytearray()
    async for chunk in response.aiter_bytes():
        data.extend(chunk)
        if len(data) > limit:
            raise WebError("response_too_large", "网页或服务返回内容过大，未读取完整结果。")
    return bytes(data)


async def public_get(client: httpx.AsyncClient, value: str) -> tuple[httpx.URL, str, bytes, str]:
    url = public_url(value)
    for _ in range(6):
        target = await pinned_url(url, resolver_client=client)
        client.cookies.clear()
        async with _public_stream(client, target, url) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise WebError("invalid_response", "网页重定向缺少目标。")
                url = public_url(str(url.join(location)))
                continue
            if response.status_code == 403 and response.headers.get("cf-mitigated") == "challenge":
                raise WebError("page_restricted", "目标页面要求浏览器验证，免费方式未取得正文。")
            response.raise_for_status()
            content = await bounded_body(response)
            return url, response.headers.get("content-type", "").lower(), content, response.encoding or "utf-8"
    raise WebError("too_many_redirects", "网页重定向次数过多，无法读取。")


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []
        self.article = []
        self.title = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(tag)
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr", "section"}:
            self.handle_data("\n")

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]
        if tag in {"p", "div", "li", "h1", "h2", "h3", "tr", "section"}:
            self.handle_data("\n")

    def handle_data(self, data):
        if "title" in self.stack:
            self.title.append(data)
        if any(tag in self.stack for tag in {"script", "style", "noscript", "template", "svg", "head", "nav", "footer"}):
            return
        self.parts.append(data)
        if "article" in self.stack or "main" in self.stack:
            self.article.append(data)


def extract_page(body: bytes, content_type: str, encoding: str) -> tuple[str, str]:
    try:
        text = body.decode(encoding, errors="replace")
    except LookupError:
        text = body.decode("utf-8", errors="replace")
    if "text/html" in content_type or "application/xhtml+xml" in content_type:
        parser = _PageText()
        parser.feed(text)
        title = " ".join("".join(parser.title).split())[:300]
        text = "".join(parser.article or parser.parts)
        restricted_titles = {"just a moment...", "access denied", "attention required! | cloudflare",
                             "安全验证", "人机验证"}
        if (title.strip().lower() in restricted_titles
                or (len(text) < 2000 and any(phrase in text.lower() for phrase in (
                    "verify you are human", "enable javascript and cookies to continue",
                    "you need to enable javascript to run this app")))):
            raise WebError("page_restricted", "免费方式取得的是验证或脚本提示页，没有取得正文。")
    elif any(kind in content_type for kind in ("text/plain", "text/markdown")):
        title = ""
    else:
        raise WebError("unsupported_content", "当前仅支持 HTML 和纯文本网页，不支持 PDF、图片或其他附件。")
    text = "\n".join(line for raw in text.splitlines() if (line := re.sub(r"[ \t\r\f\v]+", " ", raw).strip()))
    if not text:
        raise WebError("empty_page", "网页没有可读取的正文，可能需要登录或运行浏览器。")
    return title, text
