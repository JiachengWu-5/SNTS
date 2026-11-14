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
    r"(小学生|初中生|中学生|高中生|schoolgirl|schoolboy|"
    r"(?:年龄|年纪)\s*[：:为是]?\s*(?:[1-9]|1[0-7])\s*岁|"
    r"(?:年龄|年纪)\s*[：:为是]?\s*(?:十七岁|十六岁|十五岁|十四岁|十三岁|十二岁|十一岁|十岁)|"
    r"(?:名叫|名为|叫做?)\s*[\u4e00-\u9fffA-Za-z·]{1,12}[，、,\s]{0,4}"
    r"(?:十七岁|十六岁|十五岁|十四岁|十三岁|十二岁|十一岁|十岁|(?<!\d)(?:[1-9]|1[0-7])\s*岁)|"
    r"，\s*(?:十七岁|十六岁|十五岁|十四岁|十三岁|十二岁|十一岁)\s*[，。]|"
    r"(?:十六|十五|十四|十三)岁的)"
)

_SEXUAL_MARKERS = re.compile(
    r"(性交|性爱|做爱|插入|射精|阴茎|阴道|口交|肛交|性行为|裸戏|情色|色情|"
    r"肉棒|小穴|爱液|高潮|调教|强[奸姦]|轮[奸姦]|"
    r"\bsex\b|\bporn\b|erotic|nude|naked)",
    re.IGNORECASE,
)

_PROTECT_MINOR = re.compile(r"(保护|防止|禁止|远离|不得|严禁|警惕|预防|救助|关爱)")

_SAFE_NAME = re.compile(r"^[\w\u4E00-\u9FFF .()-]{1,80}\.txt$", re.IGNORECASE)


class StyleSafetyError(ValueError):
    pass


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
    if _ALWAYS_BLOCK.search(text):
        raise StyleSafetyError("该文风样本涉及未成年或不适龄内容，已拒绝使用")
    if not _SEXUAL_MARKERS.search(text):
        return
    if _MINOR_CHARACTER.search(text):
        raise StyleSafetyError("该文风样本涉及未成年或不适龄内容，已拒绝使用")
    for match in re.finditer(r"未成年人?", text):
        sentence = _sentence_around(text, match.start(), match.end())
        if _PROTECT_MINOR.search(sentence):
            continue
        if _SEXUAL_MARKERS.search(sentence):
            raise StyleSafetyError("该文风样本涉及未成年或不适龄内容，已拒绝使用")


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


async def save_style_from_url(book_name: str, url: str) -> dict:
    """Fetch a public webpage, safety-scan the text, and save it as a book style sample."""
    from app.webstyle import StyleFetchError, fetch_and_extract

    final_url, text = await fetch_and_extract(url)
    check_sample_text(text)
    folder = ensure_book(book_name) / STYLE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    host = urlparse_host(final_url)
    name = _next_web_name(folder, host)
    path = folder / name
    path.write_text(text, encoding="utf-8")
    sidecar = path.with_suffix(".source.json")
    sidecar.write_text(
        json.dumps({"url": final_url, "filename": name}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "id": f"book:{name}",
        "name": name,
        "hanzi": count_hanzi(text),
        "url": final_url,
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
