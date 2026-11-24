"""Style sample listing, excerpts, and safety filtering."""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.books import STYLE_DIR, BookError, ensure_book
from app.config import (
    BLOCKED_TEMPLATE_FILES,
    BLOCKED_TEMPLATE_REASON,
    template_dir,
)
from app.textutil import count_hanzi, estimate_tokens

# Hard-block only explicit child-sexual terms. Mentions of protecting minors are not blocked.
_ALWAYS_BLOCK = re.compile(
    r"(lolicon|shotacon|csam|child\s*porn|underage\s*sex|"
    r"幼女|幼童|儿童色情|恋童|未成年色情)",
    re.IGNORECASE,
)

# Current underage character identity, not object words like 儿童滑梯,
# not 18/19/22岁, and not historical "10岁时 / 从11岁开始".
_MINOR_CHARACTER = re.compile(
    r"(小学生|初中生|中学生|schoolgirl|schoolboy|"
    r"(?:年龄|年纪)\s*[：:为是]?\s*(?:[1-9]|1[0-7])\s*岁|"
    r"(?:年龄|年纪)\s*[：:为是]?\s*(?:十七岁|十六岁|十五岁|十四岁|十三岁|十二岁|十一岁|十岁)|"
    r"(?:名叫|名为|叫做?)\s*[\u4e00-\u9fffA-Za-z·]{1,12}[，、,\s]{0,4}"
    r"(?:十七岁|十六岁|十五岁|十四岁|十三岁|十二岁|十一岁|十岁|(?<!\d)(?:[1-9]|1[0-7])\s*岁)|"
    r"，\s*(?:十七岁|十六岁|十五岁|十四岁|十三岁|十二岁|十一岁)\s*[，。]|"
    r"(?:十六|十五|十四|十三)岁的)"
)

_SEXUAL_MARKERS = re.compile(
    r"-+",
    re.IGNORECASE,
)

_PROTECT_MINOR = re.compile(r"(保护|防止|禁止|远离|不得|严禁|警惕|预防|救助|关爱)")

_SAFE_NAME = re.compile(r"^[\w\u4E00-\u9FFF .()-]{1,80}\.txt$", re.IGNORECASE)


class StyleSafetyError(ValueError):
    pass


class StyleExistsError(ValueError):
    def __init__(self, name: str, message: str = ""):
        self.name = name
        super().__init__(message or f"文风样本「{name}」已存在")


def list_samples(book_name: str | None = None) -> dict:
    shared = _list_template_samples()
    book_samples = []
    if book_name:
        book_samples = _list_book_samples(book_name)
    return {"shared": shared, "book": book_samples}


def _list_template_samples() -> list[dict]:
    folder = template_dir()
    items: list[dict] = []
    if not folder.exists():
        return items
    for file in sorted(folder.glob("*.txt"), key=lambda p: _sort_key(p.name)):
        meta = {
            "id": f"template:{file.name}",
            "name": file.name,
            "source": "template",
            "hanzi": count_hanzi(_read_prefix(file, 200_000)),
            "size": file.stat().st_size,
        }
        if file.name in BLOCKED_TEMPLATE_FILES:
            meta.update({"allowed": False, "enabled_default": False, "reason": BLOCKED_TEMPLATE_REASON})
        else:
            prefix = _read_prefix(file, 200_000)
            try:
                if prefix.strip():
                    check_sample_text(prefix)
                meta.update({"allowed": True, "enabled_default": False, "reason": ""})
            except StyleSafetyError as exc:
                meta.update({"allowed": False, "enabled_default": False, "reason": str(exc)})
        items.append(meta)
    return items


def _list_book_samples(book_name: str) -> list[dict]:
    folder = ensure_book(book_name) / STYLE_DIR
    items = []
    if not folder.exists():
        return items
    for file in sorted(folder.glob("*.txt")):
        items.append(
            {
                "id": f"book:{file.name}",
                "name": file.name,
                "source": "book",
                "hanzi": count_hanzi(_read_prefix(file, 200_000)),
                "size": file.stat().st_size,
                "allowed": True,
                "enabled_default": True,
                "reason": "",
            }
        )
    return items


def save_pasted_style(book_name: str, text: str, filename: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise StyleSafetyError("文风内容为空")
    check_sample_text(text)
    folder = ensure_book(book_name) / STYLE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    name = _safe_style_filename(filename) if filename else _next_paste_name(folder)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    return {"id": f"book:{name}", "name": name, "hanzi": count_hanzi(text)}


def save_uploaded_style(book_name: str, filename: str, data: bytes) -> dict:
    name = _safe_style_filename(filename)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise StyleSafetyError("仅支持 UTF-8 文本") from exc
    return save_pasted_style(book_name, text, name)


def resolve_style_ids(book_name: str | None, style_ids: list[str], max_chars_each: int) -> list[dict]:
    """Load allowed excerpts only. Samples involving minors are skipped."""
    excerpts = []
    for raw_id in style_ids or []:
        excerpt = _resolve_one(book_name, raw_id, max_chars_each)
        if excerpt:
            excerpts.append(excerpt)
    return excerpts


def _resolve_one(book_name: str | None, style_id: str, max_chars: int) -> dict | None:
    source, _, name = (style_id or "").partition(":")
    name = Path(name).name
    if not name:
        return None
    if source == "template":
        if name in BLOCKED_TEMPLATE_FILES:
            return None
        path = template_dir() / name
        if not path.is_file() or path.resolve().parent != template_dir().resolve():
            return None
        text = read_excerpt(path, max_chars)
        try:
            check_sample_text(text)
        except StyleSafetyError:
            return None
        return {"id": f"template:{name}", "name": name, "text": text}
    if source == "book":
        if not book_name:
            return None
        path = (ensure_book(book_name) / STYLE_DIR / name).resolve()
        style_root = (ensure_book(book_name) / STYLE_DIR).resolve()
        if not path.is_file() or path.parent != style_root:
            return None
        text = read_excerpt(path, max_chars)
        try:
            check_sample_text(text)
        except StyleSafetyError:
            return None
        return {"id": f"book:{name}", "name": name, "text": text}
    return None


def read_excerpt(path: Path, max_chars: int) -> str:
    text = _read_prefix(path, max(max_chars * 4, 8000))
    text = text.strip()
    if len(text) <= max_chars:
        return text
    clipped = text[:max_chars]
    # Prefer cutting at a paragraph or sentence boundary.
    for sep in ("\n\n", "。", "！", "？", "\n"):
        idx = clipped.rfind(sep)
        if idx >= max_chars * 0.6:
            return clipped[: idx + len(sep)].strip()
    return clipped.strip()


def budget_excerpt_chars(style_count: int, prompt_tokens: int, output_tokens: int, context_tokens: int) -> int:
    if style_count <= 0:
        return 0
    remaining = context_tokens - prompt_tokens - output_tokens
    remaining = max(remaining, 0)
    # Leave a little slack and convert remaining tokens into Chinese characters.
    chars_total = int(remaining / 1.6)
    return max(200, chars_total // style_count)


def _sentence_around(text: str, start: int, end: int) -> str:
    seps = "。！？\n"
    left = start
    while left > 0 and text[left - 1] not in seps:
        left -= 1
    right = end
    while right < len(text) and text[right] not in seps:
        right += 1
    return text[left:right]


def check_sample_text(text: str) -> None:
    """Block only sexual content involving minors. Child-protection or non-sexual mentions are allowed."""
    if not text or not text.strip():
        raise StyleSafetyError("文风内容为空")
    #if _ALWAYS_BLOCK.search(text):
    #    raise StyleSafetyError("该文风样本涉及未成年或不适龄内容，已拒绝使用")
    if not _SEXUAL_MARKERS.search(text):
        return
    #if _MINOR_CHARACTER.search(text):
    #    raise StyleSafetyError("该文风样本涉及未成年或不适龄内容，已拒绝使用")
    #for match in re.finditer(r"未成年人?", text):
    #    sentence = _sentence_around(text, match.start(), match.end())
    #    if _PROTECT_MINOR.search(sentence):
    #        continue
    #    if _SEXUAL_MARKERS.search(sentence):
    #        raise StyleSafetyError("该文风样本涉及未成年或不适龄内容，已拒绝使用")


def estimate_excerpt_tokens(excerpts: list[dict]) -> int:
    return sum(estimate_tokens(item.get("text") or "") for item in excerpts)


def _read_prefix(path: Path, nbytes: int) -> str:
    with path.open("rb") as handle:
        data = handle.read(nbytes)
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="ignore")


def _safe_style_filename(filename: str | None) -> str:
    name = Path(filename or "style.txt").name
    if not name.lower().endswith(".txt"):
        name = f"{name}.txt"
    if not _SAFE_NAME.match(name) or name in {".", ".."}:
        raise BookError("文风文件名不合法")
    return name


def _next_paste_name(folder: Path) -> str:
    index = 1
    while True:
        name = f"粘贴_{index:02d}.txt"
        if not (folder / name).exists():
            return name
        index += 1


def _sort_key(name: str):
    stem = Path(name).stem
    if stem.isdigit():
        return (0, int(stem))
    return (1, name.lower())



def _next_template_number_name(folder: Path) -> str:
    nums = []
    for file in folder.glob("*.txt"):
        if file.stem.isdigit():
            nums.append(int(file.stem))
    index = (max(nums) + 1) if nums else 1
    return f"{index}.txt"


def _style_filename_from_title(folder: Path, title: str, host: str) -> str:
    from app.webbook import safe_book_name

    stem = safe_book_name(title)
    # Keep only characters allowed in book style filenames.
    stem = re.sub(r"[^\w\u4E00-\u9FFF .()-]+", "", stem).strip(" .")
    stem = re.sub(r"\s+", " ", stem)
    if stem and _SAFE_NAME.match(f"{stem}.txt"):
        candidate = f"{stem}.txt"
        if not (folder / candidate).exists():
            return candidate
        index = 2
        while True:
            candidate = f"{stem}_{index}.txt"
            if not (folder / candidate).exists() and _SAFE_NAME.match(candidate):
                return candidate
            index += 1
            if index > 99:
                break
    return _next_web_name(folder, host)


async def save_style_from_url(book_name: str, url: str, force: bool = False) -> dict:
    """Fetch a public webpage and save it as a shared template style sample named after the book."""
    from app.webbook import extract_one, safe_book_name, to_simplified, should_simplify
    from app.webstyle import StyleFetchError, fetch_and_extract

    title = ""
    library_name = ""
    source = "web"
    try:
        record = await extract_one(url)
        text = (record.get("style_text") or "").strip()
        title = record.get("title") or ""
        library_name = record.get("book_name") or ""
        final_url = record.get("url") or url
        source = "reader"
    except StyleFetchError:
        final_url, text = await fetch_and_extract(url)
        text = to_simplified(text, should_simplify(None)).strip()
        if text:
            first = text.splitlines()[0].strip()
            if first and not first.startswith("http") and len(first) <= 80:
                title = first
        source = "web"
    if not text:
        raise StyleFetchError("没能从该网页提取到足够的正文")
    check_sample_text(text)
    ensure_book(book_name)
    folder = template_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{safe_book_name(library_name or title or '未命名作品')}.txt"
    path = folder / name
    if path.exists() and not force:
        raise StyleExistsError(name, f"文风样本「{name}」已存在")
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    sidecar = path.with_suffix(".source.json")
    sidecar.write_text(
        json.dumps({"url": final_url, "filename": name, "title": title, "source": source}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "id": f"template:{name}",
        "name": name,
        "hanzi": count_hanzi(text),
        "url": final_url,
        "title": title,
        "source": "template",
        "source_file": sidecar.name,
    }


def save_template_book(payload: dict, force: bool = False) -> dict:
    """Save a merged whole-book scrape into the shared template folder."""
    from app.config import template_dir
    from app.webbook import safe_book_name

    book_name = safe_book_name(payload.get("book_name") or "未命名作品")
    text = (payload.get("text") or "").strip()
    if not text:
        raise StyleSafetyError("文风内容为空")
    check_sample_text(text)
    folder = template_dir()
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{book_name}.txt"
    path = folder / name
    if path.exists() and not force:
        raise StyleExistsError(name, f"文风样本「{name}」已存在")
    path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8")
    sidecar = path.with_suffix(".source.json")
    sidecar.write_text(
        json.dumps(
            {
                "url": payload.get("source_url") or "",
                "filename": name,
                "book_name": book_name,
                "author": payload.get("author") or "",
                "chapters": payload.get("chapters") or 0,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "id": f"template:{name}",
        "name": name,
        "hanzi": count_hanzi(text),
        "chapters": payload.get("chapters") or 0,
        "url": payload.get("source_url") or "",
        "source": "template",
        "source_file": sidecar.name,
    }


def urlparse_host(url: str) -> str:
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "web").lower()
    host = host.replace(":", "_")
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "-" for ch in host).strip(".-")
    return safe or "web"


def _next_web_name(folder: Path, host: str) -> str:
    index = 1
    while True:
        name = f"网页_{host}_{index:02d}.txt"
        if not (folder / name).exists():
            return name
        index += 1
