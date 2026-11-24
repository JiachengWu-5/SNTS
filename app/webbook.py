"""Fetch a public chapter or a whole book/series as a style sample."""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator, Callable, Iterable
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from app.textutil import count_hanzi
from app.webstyle import FETCH_TIMEOUT, MAX_BYTES, MAX_REDIRECTS, StyleFetchError, validate_public_http_url

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh-TW;q=0.9,zh;q=0.8,ja;q=0.7,en;q=0.6",
}

NOISE_RE = re.compile(
    r"(點擊下一頁|点击下一页|本章未完|广告|廣告|請收藏本站|请收藏本站|"
    r"愛麗絲書屋|爱丽丝书屋|ALICESW)",
    re.I,
)
SCRIPT_BOOK_RE = re.compile(r"book\.Info\s*=\s*\{(.*?)\}\s*;", re.S)
SCRIPT_FIELD_RE = re.compile(r'"(Id|Name|authorName|url)"\s*:\s*(?:(\d+)|"([^"]*)")')
CHAPTER_HREF_RE = re.compile(r"/book/\d+/[0-9a-z]+\.html", re.I)
INVALID_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PIXIV_SERIES_RE = re.compile(r"(?:www\.)?pixiv\.net/novel/series/(\d+)", re.I)
PIXIV_NOVEL_RE = re.compile(
    r"(?:www\.)?pixiv\.net/(?:novel/show\.php\?(?:[^#]*&)?id=(\d+)|novel/(\d+)(?:[/?#]|$))",
    re.I,
)

ProgressFn = Callable[[dict[str, Any]], None]


def to_simplified(text: str, enabled: bool = True) -> str:
    if not enabled or not text:
        return text
    try:
        from opencc import OpenCC

        return OpenCC("tw2s").convert(text)
    except Exception:
        pass
    try:
        from zhconv import convert

        return convert(text, "zh-cn")
    except Exception:
        return text


def should_simplify(language: str | None) -> bool:
    lang = (language or "").strip().lower()
    if lang.startswith(("ja", "en", "ko")):
        return False
    return True


def _clean_line(text: str) -> str:
    text = unescape(text or "").replace("\xa0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def safe_book_name(name: str) -> str:
    name = to_simplified(_clean_line(name), True)
    name = INVALID_NAME_RE.sub("", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80] or "未命名作品"


def format_style_text(title: str, paragraphs: Iterable[str], simplify: bool) -> str:
    title = to_simplified(_clean_line(title), simplify)
    title = re.sub(r"\s+", " ", title)
    body = [to_simplified(_clean_line(p), simplify) for p in paragraphs]
    body = [p for p in body if p]
    if title:
        return title + "\n\n" + "\n".join(body) + "\n"
    return "\n".join(body) + "\n"


def looks_like_catalog_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "/other/chapters/" in path or path.endswith("/chapters") or "/catalog" in path


def looks_like_book_home(url: str) -> bool:
    path = urlparse(url).path.lower()
    return bool(re.search(r"/novel/\d+\.html", path))


def looks_like_chapter_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return bool(re.search(r"/book/\d+/[0-9a-z]+\.html", path))


def catalog_url_from_book_id(page_url: str, book_id: str) -> str:
    parsed = urlparse(page_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return urljoin(base, f"/other/chapters/id/{book_id}.html")


def parse_pixiv_novel_id(url: str) -> str:
    if PIXIV_SERIES_RE.search(url or ""):
        return ""
    match = PIXIV_NOVEL_RE.search(url or "")
    if not match:
        return ""
    return match.group(1) or match.group(2) or ""


def parse_pixiv_series_id(url: str) -> str:
    match = PIXIV_SERIES_RE.search(url or "")
    return match.group(1) if match else ""


def is_pixiv_url(url: str) -> bool:
    return bool(parse_pixiv_series_id(url) or parse_pixiv_novel_id(url))


class _AsyncFetcher:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "_AsyncFetcher":
        timeout = httpx.Timeout(connect=8.0, read=FETCH_TIMEOUT, write=8.0, pool=8.0)
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=BROWSER_HEADERS)
        return self

    async def __aexit__(self, *args) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch(self, url: str, referer: str | None = None, retries: int = 2) -> tuple[str, bytes, str]:
        if self._client is None:
            raise StyleFetchError("内部错误：未打开网页客户端")
        current = validate_public_http_url(url)
        headers = {}
        if referer:
            headers["Referer"] = referer
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            current = validate_public_http_url(url if attempt == 0 else current)
            try:
                response, current = await self._follow(current, headers)
                content_type = (response.headers.get("content-type") or "").lower()
                return current, response.content[:MAX_BYTES], content_type
            except StyleFetchError as exc:
                last_error = exc
                message = str(exc)
                if attempt < retries and ("403" in message or "无法打开" in message or "429" in message):
                    await asyncio.sleep(1.2 + attempt)
                    continue
                raise
        raise last_error or StyleFetchError("无法打开该网页，请检查链接或稍后重试")

    async def _follow(self, current: str, headers: dict[str, str]) -> tuple[httpx.Response, str]:
        assert self._client is not None
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_http_url(current)
            try:
                response = await self._client.get(current, headers=headers)
            except httpx.HTTPError as exc:
                raise StyleFetchError("无法打开该网页，请检查链接或稍后重试") from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise StyleFetchError("网页重定向无效")
                current = urljoin(current, location)
                continue
            if response.status_code == 429:
                raise StyleFetchError("网页暂时限流（HTTP 429），请稍后再试")
            if response.status_code >= 400:
                raise StyleFetchError(f"网页无法打开（HTTP {response.status_code}）")
            return response, current
        raise StyleFetchError("网页重定向次数过多")

    async def fetch_text(self, url: str, referer: str | None = None) -> tuple[str, str]:
        final_url, data, content_type = await self.fetch(url, referer=referer)
        text = data.decode("utf-8", errors="ignore")
        if not text and "html" not in content_type and "json" not in content_type and "text" not in content_type:
            raise StyleFetchError("该链接不是可提取的网页或文本")
        return final_url, text

    async def fetch_json(self, url: str, referer: str | None = None) -> tuple[str, dict[str, Any]]:
        final_url, data, _content_type = await self.fetch(url, referer=referer)
        try:
            parsed = json.loads(data.decode("utf-8", errors="ignore"))
        except ValueError as exc:
            raise StyleFetchError("没能解析该网页的数据") from exc
        if not isinstance(parsed, dict):
            raise StyleFetchError("没能解析该网页的数据")
        return final_url, parsed


def parse_book_info(html: str) -> dict[str, str]:
    info = {"id": "", "name": "", "author": "", "url": ""}
    match = SCRIPT_BOOK_RE.search(html or "")
    if match:
        blob = match.group(1)
        for field, num, string in SCRIPT_FIELD_RE.findall(blob):
            value = num or string
            if field == "Id":
                info["id"] = str(value)
            elif field == "Name":
                info["name"] = value
            elif field == "authorName":
                info["author"] = value
            elif field == "url":
                info["url"] = value
    if not info["id"]:
        bid = re.search(r'<body[^>]*data-bid="([^"]+)"', html or "", re.I)
        if bid:
            found = re.search(r"/novel/(\d+)\.html", bid.group(1))
            if found:
                info["id"] = found.group(1)
    if not info["name"]:
        crumb = re.search(
            r'class="[^"]*(?:crumbs-nav|book-info)[^"]*"[\s\S]{0,2000}?>([\s\S]{0,4000}?)</(?:div|nav|ul)>',
            html or "",
            re.I,
        )
        if crumb:
            links = [_clean_line(re.sub(r"<[^>]+>", " ", a)) for a in re.findall(r"<a[^>]*>(.*?)</a>", crumb.group(1), re.I | re.S)]
            links = [item for item in links if item]
            if links:
                info["name"] = links[-1]
    return info


def _extract_paragraphs_from_html(block: str) -> list[str]:
    paragraphs: list[str] = []
    for raw in re.findall(r"<p[^>]*>(.*?)</p>", block or "", re.I | re.S):
        para = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
        para = re.sub(r"<script[\s\S]*?</script>", "", para, flags=re.I)
        para = re.sub(r"<[^>]+>", "", para)
        para = _clean_line(para.replace("\n", ""))
        if not para or (len(para) < 8 and NOISE_RE.search(para)):
            continue
        paragraphs.append(para)
    return paragraphs


def parse_reader_chapter(html: str, page_url: str) -> dict[str, Any]:
    title = ""
    for pattern in (
        r'<h3[^>]*class="[^"]*j_chapterName[^"]*"[^>]*>(.*?)</h3>',
        r'<h[1-3][^>]*class="[^"]*(?:j_chapterName|chapter-name|text-head)[^"]*"[^>]*>(.*?)</h[1-3]>',
        r"<h1[^>]*>(.*?)</h1>",
    ):
        match = re.search(pattern, html or "", re.I | re.S)
        if match:
            title = _clean_line(re.sub(r"<[^>]+>", " ", match.group(1)))
            if title:
                break
    content_match = re.search(
        r'<div[^>]*class="[^"]*(?:j_readContent|read-content)[^"]*"[^>]*>([\s\S]*?)</div>',
        html or "",
        re.I,
    )
    if not content_match:
        raise StyleFetchError(f"没有在页面里找到正文：{page_url}")
    paragraphs = _extract_paragraphs_from_html(content_match.group(1))
    if not paragraphs:
        raise StyleFetchError(f"正文是空的：{page_url}")
    next_url = ""
    prev_url = ""
    nurl = re.search(r'data-nurl="([^"]*)"', html or "", re.I)
    purl = re.search(r'data-purl="([^"]*)"', html or "", re.I)
    if nurl and nurl.group(1).strip():
        next_url = nurl.group(1).strip()
    if purl and purl.group(1).strip():
        prev_url = purl.group(1).strip()
    next_link = re.search(
        r'id="j_chapterNext"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*id="j_chapterNext"',
        html or "",
        re.I,
    )
    prev_link = re.search(
        r'id="j_chapterPrev"[^>]*href="([^"]+)"|href="([^"]+)"[^>]*id="j_chapterPrev"',
        html or "",
        re.I,
    )
    if next_link:
        next_url = next_url or (next_link.group(1) or next_link.group(2) or "")
    if prev_link:
        prev_url = prev_url or (prev_link.group(1) or prev_link.group(2) or "")
    return {
        "title": title or "未命名章节",
        "paragraphs": paragraphs,
        "next_url": urljoin(page_url, next_url) if next_url else "",
        "prev_url": urljoin(page_url, prev_url) if prev_url else "",
        "book": parse_book_info(html),
        "url": page_url,
        "language": "zh",
    }


def parse_catalog(html: str, page_url: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or "", re.I | re.S):
        href, inner = match.group(1), match.group(2)
        if not CHAPTER_HREF_RE.search(href):
            continue
        text = _clean_line(re.sub(r"<[^>]+>", " ", inner))
        full = urljoin(page_url, href)
        if full in seen or not text or text in {"开始阅读", "開始閱讀", "继续阅读", "繼續閱讀"}:
            continue
        seen.add(full)
        items.append({"title": text, "url": full})
    return items


def clean_pixiv_content(content: str) -> list[str]:
    text = content or ""
    text = re.sub(r"\[\[rb:\s*([^\]]+?)\s*>\s*[^\]]*?\]\]", r"\1", text)
    text = re.sub(r"\[jumpuri:\s*([^\]]+?)\s*>\s*[^\]]*?\]", r"\1", text)
    text = re.sub(r"\[chapter:([^\]]+)\]", r"\1\n\n", text)
    text = re.sub(r"\[(?:pixivimage|uploadedimage|jump):[^\]]*\]", "", text)
    text = text.replace("[newpage]", "\n\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    if re.search(r"\n\s*\n", text):
        parts = [re.sub(r"\s*\n\s*", "", part).strip() for part in re.split(r"\n\s*\n", text)]
    else:
        parts = [part.strip() for part in text.split("\n")]
    return [_clean_line(part) for part in parts if _clean_line(part)]


def _pixiv_error(data: dict[str, Any], fallback: str) -> None:
    if data.get("error"):
        message = str(data.get("message") or "").strip() or fallback
        raise StyleFetchError(message)


async def _pixiv_novel(fetcher: _AsyncFetcher, novel_id: str, referer: str | None = None) -> dict[str, Any]:
    page_url = f"https://www.pixiv.net/novel/show.php?id={novel_id}"
    ajax_url = f"https://www.pixiv.net/ajax/novel/{novel_id}?lang=zh"
    _final, data = await fetcher.fetch_json(ajax_url, referer=referer or page_url)
    _pixiv_error(data, "没能打开这篇 Pixiv 小说")
    body = data.get("body") or {}
    if not isinstance(body, dict):
        raise StyleFetchError("没能打开这篇 Pixiv 小说")
    if body.get("isLoginOnly") and not (body.get("content") or "").strip():
        raise StyleFetchError("这篇 Pixiv 小说需要登录后才能阅读")
    paragraphs = clean_pixiv_content(body.get("content") or "")
    if not paragraphs:
        raise StyleFetchError("这篇 Pixiv 小说没有可提取的正文")
    series = body.get("seriesNavData") or {}
    series = series if isinstance(series, dict) else {}
    return {
        "title": _clean_line(body.get("title") or "") or "未命名章节",
        "paragraphs": paragraphs,
        "url": page_url,
        "language": body.get("language") or "",
        "book": {
            "id": str(series.get("seriesId") or ""),
            "name": _clean_line(series.get("title") or body.get("title") or ""),
            "author": _clean_line(body.get("userName") or ""),
            "url": (
                f"https://www.pixiv.net/novel/series/{series.get('seriesId')}"
                if series.get("seriesId")
                else page_url
            ),
        },
        "series_id": str(series.get("seriesId") or ""),
        "next_id": str((series.get("next") or {}).get("id") or "") if isinstance(series.get("next"), dict) else "",
        "prev_id": str((series.get("prev") or {}).get("id") or "") if isinstance(series.get("prev"), dict) else "",
        "order": series.get("order"),
    }


async def _pixiv_series_meta(fetcher: _AsyncFetcher, series_id: str) -> dict[str, str]:
    page_url = f"https://www.pixiv.net/novel/series/{series_id}"
    ajax_url = f"https://www.pixiv.net/ajax/novel/series/{series_id}?lang=zh"
    _final, data = await fetcher.fetch_json(ajax_url, referer=page_url)
    _pixiv_error(data, "没能打开这个 Pixiv 系列")
    body = data.get("body") or {}
    if not isinstance(body, dict):
        raise StyleFetchError("没能打开这个 Pixiv 系列")
    return {
        "id": str(body.get("id") or series_id),
        "name": _clean_line(body.get("title") or ""),
        "author": _clean_line(body.get("userName") or ""),
        "url": page_url,
        "first_novel_id": str(body.get("firstNovelId") or ""),
        "count": str(body.get("publishedContentCount") or body.get("displaySeriesContentCount") or ""),
        "language": body.get("language") or "",
    }


async def _pixiv_series_items(fetcher: _AsyncFetcher, series_id: str) -> list[dict[str, str]]:
    referer = f"https://www.pixiv.net/novel/series/{series_id}"
    items: list[dict[str, str]] = []
    last_order = 0
    for _ in range(200):
        ajax = (
            "https://www.pixiv.net/ajax/novel/series_content/"
            f"{series_id}?limit=30&last_order={last_order}&order_by=asc&lang=zh"
        )
        try:
            _final, data = await fetcher.fetch_json(ajax, referer=referer)
        except StyleFetchError:
            break
        if data.get("error"):
            break
        contents = (((data.get("body") or {}).get("page") or {}).get("seriesContents")) or []
        if not isinstance(contents, list) or not contents:
            break
        orders: list[int] = []
        for row in contents:
            if not isinstance(row, dict):
                continue
            novel_id = str(row.get("id") or "")
            order = (row.get("series") or {}).get("contentOrder")
            if order is not None:
                try:
                    orders.append(int(order))
                except (TypeError, ValueError):
                    pass
            if novel_id:
                items.append(
                    {
                        "title": "",
                        "url": f"https://www.pixiv.net/novel/show.php?id={novel_id}",
                        "id": novel_id,
                    }
                )
        if len(contents) < 30:
            break
        last_order = max(orders) if orders else last_order + len(contents)
        await _pause(0.4)
    # de-dupe while keeping order
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in items:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        unique.append(item)
    return unique


async def _pause(delay: float) -> None:
    if delay <= 0:
        return
    await asyncio.sleep(delay + random.uniform(0, min(0.4, delay * 0.3)))


async def _collect_pixiv(
    fetcher: _AsyncFetcher,
    url: str,
    only: bool,
) -> tuple[dict[str, str], list[dict[str, Any]], str]:
    series_id = parse_pixiv_series_id(url)
    novel_id = parse_pixiv_novel_id(url)
    if series_id:
        book = await _pixiv_series_meta(fetcher, series_id)
        items = await _pixiv_series_items(fetcher, series_id)
        if not items and book.get("first_novel_id"):
            items = await _walk_pixiv_series(fetcher, book["first_novel_id"])
        if not items:
            raise StyleFetchError("这个 Pixiv 系列里没有找到可抓取的作品")
        if only:
            items = items[:1]
        return book, items, book.get("url") or url
    if not novel_id:
        raise StyleFetchError("无法识别该 Pixiv 链接")
    chapter = await _pixiv_novel(fetcher, novel_id, referer=url)
    book = chapter["book"]
    if only or not chapter.get("series_id"):
        return book, [{"title": chapter["title"], "url": chapter["url"], "id": novel_id, "_parsed": chapter}], chapter["url"]
    series_id = chapter["series_id"]
    try:
        await _pause(0.4)
        meta = await _pixiv_series_meta(fetcher, series_id)
        book = {**book, **{key: value for key, value in meta.items() if value}}
        items = await _pixiv_series_items(fetcher, series_id)
        if items:
            return book, items, book.get("url") or url
    except StyleFetchError:
        pass
    start = chapter
    guard = 0
    while start.get("prev_id") and guard < 500:
        guard += 1
        await _pause(0.6)
        start = await _pixiv_novel(fetcher, start["prev_id"], referer=start["url"])
    start_id = parse_pixiv_novel_id(start["url"]) or novel_id
    items = await _walk_pixiv_series(fetcher, start_id, first=start)
    return book, items, chapter["url"]


async def _walk_pixiv_series(
    fetcher: _AsyncFetcher,
    start_id: str,
    first: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    current = first or await _pixiv_novel(fetcher, start_id)
    items = [
        {
            "title": current["title"],
            "url": current["url"],
            "id": parse_pixiv_novel_id(current["url"]) or start_id,
            "_parsed": current,
        }
    ]
    guard = 0
    while current.get("next_id") and guard < 500:
        guard += 1
        await _pause(0.6)
        current = await _pixiv_novel(fetcher, current["next_id"], referer=current["url"])
        items.append(
            {
                "title": current["title"],
                "url": current["url"],
                "id": parse_pixiv_novel_id(current["url"]),
                "_parsed": current,
            }
        )
    return items


async def _collect_reader(
    fetcher: _AsyncFetcher,
    url: str,
    only: bool,
) -> tuple[dict[str, str], list[dict[str, Any]], str]:
    final_url, html = await fetcher.fetch_text(url)
    book = parse_book_info(html)
    if looks_like_catalog_url(final_url):
        items = parse_catalog(html, final_url)
        if not items:
            raise StyleFetchError("目录页里没有找到章节链接")
        return book, items[:1] if only else items, final_url
    if looks_like_book_home(final_url):
        match_id = re.search(r"/novel/(\d+)\.html", urlparse(final_url).path)
        book_id = book.get("id") or (match_id.group(1) if match_id else "")
        if not book_id:
            raise StyleFetchError("无法从书籍页解析编号")
        cat_url = catalog_url_from_book_id(final_url, book_id)
        cat_final, cat_html = await fetcher.fetch_text(cat_url, referer=final_url)
        items = parse_catalog(cat_html, cat_final)
        if not items:
            raise StyleFetchError(f"目录页里没有找到章节链接：{cat_final}")
        return book, items[:1] if only else items, cat_final
    if looks_like_chapter_url(final_url) or re.search(r"j_readContent|read-content", html or "", re.I):
        chapter = parse_reader_chapter(html, final_url)
        merged = dict(chapter["book"])
        merged.update({key: value for key, value in book.items() if value})
        book = merged
        if only:
            return book, [{"title": chapter["title"], "url": final_url, "_parsed": chapter}], final_url
        book_id = book.get("id")
        if book_id:
            cat_url = catalog_url_from_book_id(final_url, book_id)
            try:
                await _pause(0.5)
                cat_final, cat_html = await fetcher.fetch_text(cat_url, referer=final_url)
                items = parse_catalog(cat_html, cat_final)
                if items:
                    return book, items, cat_final
            except StyleFetchError:
                pass
        items = [{"title": chapter["title"], "url": final_url, "_parsed": chapter}]
        current = chapter
        guard = 0
        while current.get("next_url") and guard < 500:
            guard += 1
            await _pause(0.7)
            next_final, next_html = await fetcher.fetch_text(current["next_url"], referer=current["url"])
            current = parse_reader_chapter(next_html, next_final)
            items.append({"title": current["title"], "url": next_final, "_parsed": current})
        return book, items, final_url
    raise StyleFetchError("无法识别该链接是章节、书籍、目录还是 Pixiv 作品")


async def collect_source(url: str, only: bool) -> tuple[dict[str, str], list[dict[str, Any]], str]:
    async with _AsyncFetcher() as fetcher:
        if is_pixiv_url(url):
            return await _collect_pixiv(fetcher, url, only)
        return await _collect_reader(fetcher, url, only)


async def fetch_chapter_item(
    fetcher: _AsyncFetcher,
    item: dict[str, Any],
    referer: str | None = None,
) -> dict[str, Any]:
    if item.get("_parsed"):
        return item["_parsed"]
    url = item.get("url") or ""
    novel_id = item.get("id") or parse_pixiv_novel_id(url)
    if novel_id and ("pixiv.net" in url or item.get("id")):
        return await _pixiv_novel(fetcher, novel_id, referer=referer or url)
    final_url, html = await fetcher.fetch_text(url, referer=referer)
    return parse_reader_chapter(html, final_url)


def build_chapter_record(parsed: dict[str, Any], fallback_title: str, index: int) -> dict[str, Any]:
    title = parsed.get("title") or fallback_title or f"第{index}章"
    simplify = should_simplify(parsed.get("language"))
    style_text = format_style_text(title, parsed.get("paragraphs") or [], simplify)
    return {
        "title": to_simplified(_clean_line(title), simplify),
        "url": parsed.get("url") or "",
        "style_text": style_text,
        "hanzi": count_hanzi(style_text),
        "language": parsed.get("language") or "",
        "book": parsed.get("book") or {},
    }


async def extract_one(url: str) -> dict[str, Any]:
    """Fetch a single chapter/work as style text."""
    book, items, _source = await collect_source(url, only=True)
    if not items:
        raise StyleFetchError("没能从该网页提取到足够的正文")
    async with _AsyncFetcher() as fetcher:
        parsed = await fetch_chapter_item(fetcher, items[0], referer=url)
    record = build_chapter_record(parsed, items[0].get("title") or "", 1)
    record["book_name"] = safe_book_name(book.get("name") or parsed.get("book", {}).get("name") or record["title"])
    return record


async def iter_extract_all(url: str) -> AsyncIterator[dict[str, Any]]:
    """Yield progress events, then a final result for a whole book or series."""
    yield {"kind": "status", "message": "正在查找目录…"}
    async with _AsyncFetcher() as fetcher:
        if is_pixiv_url(url):
            book, items, source_url = await _collect_pixiv(fetcher, url, only=False)
        else:
            book, items, source_url = await _collect_reader(fetcher, url, only=False)
        book_name = safe_book_name(book.get("name") or "未命名作品")
        total = len(items)
        if total <= 0:
            raise StyleFetchError("没有找到可抓取的章节")
        yield {
            "kind": "catalog",
            "book_name": book_name,
            "total": total,
            "source_url": source_url,
            "author": book.get("author") or "",
        }
        chapters: list[dict[str, Any]] = []
        for index, item in enumerate(items, start=1):
            if index > 1:
                await _pause(0.55)
            parsed = await fetch_chapter_item(fetcher, item, referer=item.get("url") or source_url)
            record = build_chapter_record(parsed, item.get("title") or "", index)
            chapters.append(record)
            yield {
                "kind": "progress",
                "current": index,
                "total": total,
                "title": record["title"],
                "hanzi": record["hanzi"],
                "book_name": book_name,
            }
    merged = "\n\n".join(ch["style_text"].strip() for ch in chapters if ch.get("style_text", "").strip()) + "\n"
    yield {
        "kind": "result",
        "book_name": book_name,
        "author": book.get("author") or "",
        "source_url": source_url,
        "chapters": len(chapters),
        "hanzi": count_hanzi(merged),
        "text": merged,
        "titles": [ch["title"] for ch in chapters],
    }


# Avoid unused import warning for parse_qs on some linters; keep available for query parsing.
_ = parse_qs
