"""Fetch a public webpage and extract readable text for use as a style sample."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from app.textutil import count_hanzi

FETCH_TIMEOUT = 15.0
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 3
MAX_TEXT_CHARS = 80_000


class StyleFetchError(ValueError):
    pass


def validate_public_http_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise StyleFetchError("请粘贴网页链接")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise StyleFetchError("只支持 http 或 https 链接")
    if parsed.username or parsed.password:
        raise StyleFetchError("链接不合法")
    host = parsed.hostname
    if not host:
        raise StyleFetchError("链接不合法")
    _assert_public_host(host)
    return raw


def _assert_public_host(host: str) -> None:
    lowered = host.strip("[]").lower()
    if lowered in {"localhost", "localhost.localdomain", "metadata.google.internal"}:
        raise StyleFetchError("不能抓取本机或内网地址")
    if lowered.endswith(".localhost") or lowered.endswith(".local") or lowered.endswith(".internal"):
        raise StyleFetchError("不能抓取本机或内网地址")
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        ip = None
    if ip is not None and not _is_public_ip(ip):
        raise StyleFetchError("不能抓取本机或内网地址")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise StyleFetchError("无法解析该网页地址") from exc
    if not infos:
        raise StyleFetchError("无法解析该网页地址")
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not _is_public_ip(addr):
            raise StyleFetchError("不能抓取本机或内网地址")


def _is_public_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip.version == 6 and getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


async def fetch_and_extract(url: str) -> tuple[str, str]:
    current = validate_public_http_url(url)
    timeout = httpx.Timeout(connect=8.0, read=FETCH_TIMEOUT, write=8.0, pool=8.0)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Accept-Language": "zh-CN,zh-TW;q=0.9,zh;q=0.8"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_http_url(current)
            try:
                response = await client.get(current)
            except httpx.HTTPError as exc:
                raise StyleFetchError("无法打开该网页，请检查链接或稍后重试") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise StyleFetchError("网页重定向无效")
                current = urljoin(current, location)
                continue
            if response.status_code >= 400:
                raise StyleFetchError(f"网页无法打开（HTTP {response.status_code}）")
            content_type = (response.headers.get("content-type") or "").lower()
            data = response.content[:MAX_BYTES]
            break
        else:
            raise StyleFetchError("网页重定向次数过多")
    if "text/plain" in content_type:
        text = data.decode("utf-8", errors="ignore").strip()
    elif "html" in content_type or data.lstrip()[:16].lower().startswith((b"<!doctype", b"<html")):
        text = extract_readable_text(data.decode("utf-8", errors="ignore"))
    else:
        raise StyleFetchError("该链接不是可提取的网页或文本")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 40 or count_hanzi(text) + len(text) < 80:
        raise StyleFetchError("没能从该网页提取到足够的正文")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS].rsplit("\n", 1)[0].strip()
    return current, text


def extract_reader_chapter(html: str) -> str:
    """Pull title and paragraphs from common novel-reader markup."""
    from html import unescape

    title_match = re.search(
        r'<h3[^>]*class="[^"]*j_chapterName[^"]*"[^>]*>(.*?)</h3>',
        html or "",
        re.I | re.S,
    )
    content_match = re.search(
        r'<div[^>]*class="[^"]*(?:j_readContent|read-content)[^"]*"[^>]*>(.*?)</div>',
        html or "",
        re.I | re.S,
    )
    if not content_match:
        return ""
    paragraphs = []
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", content_match.group(1), re.I | re.S):
        para = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        para = re.sub(r"<[^>]+>", "", para)
        para = unescape(para)
        para = re.sub(r"[\xa0\u3000]", " ", para)
        para = re.sub(r"[ \t]+", " ", para).strip()
        if para:
            paragraphs.append(para)
    if not paragraphs:
        return ""
    title = ""
    if title_match:
        title = unescape(re.sub(r"<[^>]+>", "", title_match.group(1)))
        title = re.sub(r"\s+", " ", title).strip()
    lines = ([title, ""] if title else []) + paragraphs
    return "\n".join(lines).strip()


def extract_readable_text(html: str) -> str:
    reader = extract_reader_chapter(html)
    if reader:
        return reader
    parser = _VisibleTextParser()
    parser.feed(html or "")
    parser.close()
    chunks = parser.preferred or parser.body
    text = "\n".join(chunk.strip() for chunk in chunks if chunk and chunk.strip())
    return text.strip()


_SKIP = {"script", "style", "noscript", "svg", "canvas", "iframe", "template", "head"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_NOISE = {"nav", "footer", "header", "aside", "form"}
_MAIN = {"article", "main"}


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.body: list[str] = []
        self.preferred: list[str] = []
        self._skip = 0
        self._noise = 0
        self._main = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in _SKIP and tag not in _VOID:
            self._skip += 1
        if tag in _NOISE:
            self._noise += 1
        if tag in _MAIN:
            self._main += 1
        if tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section"}:
            self._flush()
        if tag in _MAIN and self._main:
            self._main -= 1
        if tag in _NOISE and self._noise:
            self._noise -= 1
        if tag in _SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip or self._noise:
            return
        text = re.sub(r"\s+", " ", data or "").strip()
        if text:
            self._buf.append(text)

    def _flush(self):
        if not self._buf:
            return
        line = " ".join(self._buf).strip()
        self._buf.clear()
        if not line:
            return
        self.body.append(line)
        if self._main:
            self.preferred.append(line)

    def close(self):
        self._flush()
        super().close()
