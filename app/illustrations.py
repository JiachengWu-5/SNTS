"""Scene illustrations: markers, disk storage, describe prompts, and safety checks."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from app.books import BookError, ensure_book, validate_chapter_id, _read_text, _write_text
from app.textutil import (
    count_visible_hanzi,
    find_illustration_ids,
    illustration_marker,
    strip_illustration_markers,
)

ILLUST_DIR = "插图"
META_FILE = "元数据.json"
META_VERSION = 1
HONG_KONG = timezone(timedelta(hours=8))

# Re-export so callers can import from this module as specified.
strip_illustration_markers = strip_illustration_markers
illustration_marker = illustration_marker
find_illustration_ids = find_illustration_ids

UNDERAGE_SEXUAL_MESSAGE = "这段内容涉及未满 18 岁的性描写，不能用来出图。"

_SEXUAL_RE = re.compile(
    r"("
    r"性交|性爱|性行为|性器|性器官|性描写|色情|情色|淫秽|淫乱|淫荡|卖淫|"
    r"做爱|交媾|强奸|轮奸|迷奸|性侵|口交|肛交|手淫|自慰|射精|高潮|"
    r"阴茎|阳具|肉棒|阴道|阴部|阴唇|阴蒂|乳房|乳头|乳首|勃起|插入|"
    r"裸体|裸身|全裸|半裸|裸露|裸照|脱光|褪去衣|"
    r"porn|nsfw|nude|naked|intercourse|fellatio|cunnilingus|"
    r"\bsex\b|genitals?|penis|vagina|breasts?"
    r")",
    re.IGNORECASE,
)

_CSAM_ALWAYS_RE = re.compile(
    r"("
    r"幼交|童奸|奸幼|儿童色情|儿童淫秽|未成年色情|未成年性交|"
    r"萝莉色情|正太色情|lolicon|shotacon|child\s*porn|csam|"
    r"underage\s*(?:sex|porn|nude)|sexual(?:ly)?\s*(?:explicit\s*)?minors?"
    r")",
    re.IGNORECASE,
)

_MINOR_RE = re.compile(
    r"("
    r"未成年|未满\s*十?\s*[八8]|不满\s*十?\s*[八8]|未满十八|不满十八|"
    r"幼女|幼男|幼童|幼儿|儿童|小孩|小孩子|孩童|女童|男童|稚童|"
    r"小学生|初中生|小学[生一二三四五六]|"
    r"萝莉|正太|loli|shota|underage|preteen|toddler|child\b|children\b|"
    r"十几岁|"
    r"(?<![0-9零〇一二三四五六七八九十两廿卅])(?:1[0-7]|[1-9])\s*岁|"
    r"十[一二三四五六七]\s*岁|"
    r"(?<![十])[一二三四五六七八九]\s*岁|"
    r"(?:ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen)[\s\-]*years?"
    r")",
    re.IGNORECASE,
)


def illustrations_root(name: str) -> Path:
    return ensure_book(name) / ILLUST_DIR


def meta_path(name: str) -> Path:
    return illustrations_root(name) / META_FILE


def empty_store() -> dict:
    return {"version": META_VERSION, "illustrations": []}


def load_store(name: str) -> dict:
    path = meta_path(name)
    if not path.exists():
        return empty_store()
    try:
        data = json.loads(_read_text(path) or "{}")
    except json.JSONDecodeError:
        return empty_store()
    if not isinstance(data, dict):
        return empty_store()
    items = data.get("illustrations")
    if not isinstance(items, list):
        items = []
    cleaned = [_public_item(item) for item in items if isinstance(item, dict)]
    return {"version": META_VERSION, "illustrations": cleaned}


def save_store(name: str, store: dict) -> dict:
    payload = {
        "version": META_VERSION,
        "illustrations": [_public_item(item) for item in (store.get("illustrations") or [])],
    }
    path = meta_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def list_illustrations(name: str, chapter_id: str | None = None) -> list[dict]:
    store = load_store(name)
    items = store["illustrations"]
    if chapter_id:
        cid = validate_chapter_id(chapter_id)
        items = [item for item in items if item.get("chapter_id") == cid]
    return items


def get_illustration(name: str, illust_id: str) -> dict:
    store = load_store(name)
    item = _find(store, illust_id)
    if not item:
        raise BookError("找不到该插图")
    return item


def image_path(name: str, item: dict) -> Path:
    cid = validate_chapter_id(item["chapter_id"])
    filename = item.get("filename") or f"{item['id']}.png"
    return illustrations_root(name) / cid / filename


def save_illustration(
    name: str,
    chapter_id: str,
    png_bytes: bytes,
    *,
    prompt: str,
    source_excerpt: str = "",
) -> dict:
    if not png_bytes:
        raise BookError("出图没有返回图片")
    cid = validate_chapter_id(chapter_id)
    store = load_store(name)
    illust_id = _next_id(store)
    filename = f"{illust_id}.png"
    dest = illustrations_root(name) / cid
    dest.mkdir(parents=True, exist_ok=True)
    (dest / filename).write_bytes(png_bytes)
    now = datetime.now(HONG_KONG).strftime("%Y-%m-%dT%H:%M:%S%z")
    # +0800 -> +08:00 for readability
    if len(now) >= 5 and now[-5] in "+-" and now[-3] != ":":
        now = now[:-2] + ":" + now[-2:]
    item = {
        "id": illust_id,
        "chapter_id": cid,
        "prompt": (prompt or "").strip(),
        "source_excerpt": (source_excerpt or "").strip()[:4000],
        "created_at": now,
        "inserted": False,
        "filename": filename,
    }
    store["illustrations"].append(item)
    save_store(name, store)
    return dict(item)


def mark_inserted(name: str, illust_id: str, inserted: bool = True) -> dict:
    store = load_store(name)
    item = _find(store, illust_id)
    if not item:
        raise BookError("找不到该插图")
    item["inserted"] = bool(inserted)
    save_store(name, store)
    return dict(item)


def delete_illustration(name: str, illust_id: str) -> dict:
    store = load_store(name)
    item = _find(store, illust_id)
    if not item:
        raise BookError("找不到该插图")
    path = image_path(name, item)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
    store["illustrations"] = [row for row in store["illustrations"] if row.get("id") != illust_id]
    save_store(name, store)
    marker = illustration_marker(illust_id)
    chapter_id = item.get("chapter_id")
    text = None
    try:
        from app import books

        current = books.read_chapter(name, chapter_id)
        body = current.get("text") or ""
        if marker in body:
            cleaned = strip_markers_for_ids(body, [illust_id])
            books.write_chapter(name, chapter_id, cleaned)
            text = cleaned
    except BookError:
        text = None
    return {"ok": True, "id": illust_id, "chapter_id": chapter_id, "marker": marker, "text": text}


def insert_marker_info(name: str, illust_id: str) -> dict:
    item = get_illustration(name, illust_id)
    marker = illustration_marker(item["id"])
    mark_inserted(name, illust_id, True)
    return {
        "ok": True,
        "id": item["id"],
        "chapter_id": item["chapter_id"],
        "marker": marker,
        "inserted": True,
    }


def strip_markers_for_ids(text: str, ids: list[str]) -> str:
    cleaned = text or ""
    for illust_id in ids:
        cleaned = cleaned.replace(illustration_marker(illust_id), "")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def underage_sexual_refusal_reason(*texts: str) -> str | None:
    """Return a refusal message if any text depicts sexual content involving anyone under 18."""
    blob = "\n".join(t or "" for t in texts)
    if not blob.strip():
        return None
    if _CSAM_ALWAYS_RE.search(blob):
        return UNDERAGE_SEXUAL_MESSAGE
    has_minor = bool(_MINOR_RE.search(blob))
    has_sexual = bool(_SEXUAL_RE.search(blob))
    if has_minor and has_sexual:
        return UNDERAGE_SEXUAL_MESSAGE
    return None


def format_appearance_block(characters: list[dict] | None) -> str:
    if not characters:
        return ""
    parts = []
    for item in characters:
        name = (item.get("name") or "未命名").strip() or "未命名"
        appearance = (item.get("appearance") or "").strip()
        basics = (item.get("basics") or "").strip()
        if appearance:
            block = f"【{name}】\n外形：\n{appearance}"
            if basics:
                block += f"\n基础设定（仅作参考）：\n{basics}"
            parts.append(block)
        elif basics:
            parts.append(f"【{name}】\n基础设定（仅作参考，请从中提取可见外形，不要编造未写明的相貌）：\n{basics}")
    return "\n\n".join(parts)


def build_describe_messages(source_text: str, characters: list[dict] | None = None) -> list[dict]:
    source = strip_illustration_markers(source_text or "").strip()
    appearance = format_appearance_block(characters or [])
    appearance_block = f"【人物外形】\n{appearance}\n\n" if appearance else "【人物外形】\n（本书尚未填写人物外形，请只根据正文里写明的可见特征来写，不要编造相貌。）\n\n"
    system = (
        "你是一名中文小说插图顾问，只根据给定正文和人物外形，写出适合文生图模型的画面说明。"
        "只描写一个静止或短暂的可视场景：构图、环境、光线、人物外貌与姿态、服装与道具。"
        "不要写情节摘要、内心独白、镜头运动脚本或对画家的吩咐。"
        "所有人物必须按成年（18 岁或以上）来描绘。"
        "不得描写、暗示或要求任何涉及未满 18 岁的性内容、裸露或色情画面。"
        "成人之间的亲密或情色场面如果原文确有、且明显均为成年，可以按画面需要写，但不得把任何人写成未成年人。"
        "不要输出标题、编号或解释，只输出画面说明正文。"
    )
    user = (
        "请根据下面的「选定正文」写出一段中文画面说明，供文生图模型使用。"
        "若「人物外形」里有对应角色，外貌必须与之相符。\n\n"
        f"{appearance_block}"
        f"【选定正文】\n{source}\n"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def cleanup_describe_output(text: str) -> str:
    body = (text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
        body = body.strip()
    return body.strip().strip("\"“”")


async def describe_scene(
    book_name: str,
    chapter_id: str,
    source_text: str,
    *,
    llm_url: str | None = None,
    temperature: float | None = None,
    cancel_event=None,
) -> dict:
    from app import books, characters as character_store
    from app.llm import chat

    books.ensure_book(book_name)
    cid = validate_chapter_id(chapter_id)
    source = strip_illustration_markers(source_text).strip()
    if not source:
        raise BookError("请先选中一段正文，或确认本章有正文")
    reason = underage_sexual_refusal_reason(source)
    if reason:
        raise BookError(reason)
    settings = books.load_settings(book_name)
    try:
        chars = character_store.list_characters(book_name, cid, inclusive=True)
    except Exception:
        chars = []
    messages = build_describe_messages(source, chars)
    result = await chat(
        messages,
        llm_url=llm_url or settings.get("llm_url"),
        temperature=0.6 if temperature is None else float(temperature),
        max_tokens=1024,
        enable_thinking=False,
        cancel_event=cancel_event,
    )
    prompt = cleanup_describe_output(result.content)
    if not prompt:
        raise BookError("没有写出画面说明，请再试一次")
    reason = underage_sexual_refusal_reason(source, prompt)
    if reason:
        raise BookError(reason)
    return {
        "prompt": prompt,
        "source_excerpt": source[:4000],
        "chapter_id": cid,
        "hanzi": count_visible_hanzi(prompt),
    }


def _public_item(item: dict) -> dict:
    illust_id = str(item.get("id") or "").strip()
    chapter_id = str(item.get("chapter_id") or "").strip()
    try:
        chapter_id = validate_chapter_id(chapter_id) if chapter_id else ""
    except BookError:
        pass
    filename = str(item.get("filename") or "").strip() or (f"{illust_id}.png" if illust_id else "")
    return {
        "id": illust_id,
        "chapter_id": chapter_id,
        "prompt": str(item.get("prompt") or ""),
        "source_excerpt": str(item.get("source_excerpt") or ""),
        "created_at": str(item.get("created_at") or ""),
        "inserted": bool(item.get("inserted")),
        "filename": filename,
    }


def _find(store: dict, illust_id: str) -> dict | None:
    wanted = (illust_id or "").strip()
    for item in store.get("illustrations") or []:
        if item.get("id") == wanted:
            return item
    return None


def _next_id(store: dict) -> str:
    used = {item.get("id") for item in store.get("illustrations") or []}
    n = 1
    while f"i{n:04d}" in used:
        n += 1
    return f"i{n:04d}"
