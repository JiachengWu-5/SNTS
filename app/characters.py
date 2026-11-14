"""Character cards with path-accumulated state patches."""

from __future__ import annotations

import json
import re
from typing import Iterable

from app.books import BookError, ensure_book, validate_chapter_id, _read_text, _write_text

CHARACTERS_FILE = "人物.json"
CHARACTERS_VERSION = 1
_NAME_INVALID = re.compile(r"[\r\n]")


def characters_path(name: str):
    return ensure_book(name) / CHARACTERS_FILE


def empty_store() -> dict:
    return {"version": CHARACTERS_VERSION, "characters": []}


def load_characters(name: str) -> dict:
    path = characters_path(name)
    if not path.exists():
        return empty_store()
    try:
        data = json.loads(_read_text(path) or "{}")
    except json.JSONDecodeError:
        return empty_store()
    if not isinstance(data, dict):
        return empty_store()
    items = data.get("characters")
    if not isinstance(items, list):
        items = []
    cleaned = []
    for item in items:
        if isinstance(item, dict):
            cleaned.append(_public_character(item))
    return {"version": CHARACTERS_VERSION, "characters": cleaned}


def save_characters(name: str, store: dict) -> dict:
    payload = {
        "version": CHARACTERS_VERSION,
        "characters": [_public_character(item) for item in (store.get("characters") or [])],
    }
    _write_text(characters_path(name), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def list_characters(name: str, chapter_id: str | None = None, *, inclusive: bool = False) -> list[dict]:
    store = load_characters(name)
    path_ids = resolve_path_ids(name, chapter_id) if chapter_id else []
    if chapter_id and not inclusive and path_ids:
        current = _safe_chapter_id(chapter_id)
        if current and path_ids[-1] == current:
            path_ids = path_ids[:-1]
    result = []
    for item in store["characters"]:
        result.append(decorate_character(item, path_ids))
    return result


def get_character(name: str, character_id: str) -> dict:
    store = load_characters(name)
    item = _find(store, character_id)
    if not item:
        raise BookError("找不到该人物")
    return item


def add_character(name: str, char_name: str, basics: str = "", appearance: str = "") -> dict:
    store = load_characters(name)
    trimmed = _validate_name(char_name)
    if any(item["name"] == trimmed for item in store["characters"]):
        raise BookError("已有同名人物")
    item = {
        "id": _next_id(store, "c"),
        "name": trimmed,
        "basics": (basics or "").strip(),
        "appearance": (appearance or "").strip(),
        "patches": [],
    }
    store["characters"].append(item)
    save_characters(name, store)
    return item


def update_character(name: str, character_id: str, *, char_name: str | None = None, basics: str | None = None, appearance: str | None = None) -> dict:
    store = load_characters(name)
    item = _find(store, character_id)
    if not item:
        raise BookError("找不到该人物")
    if char_name is not None:
        trimmed = _validate_name(char_name)
        if any(other["name"] == trimmed and other["id"] != item["id"] for other in store["characters"]):
            raise BookError("已有同名人物")
        item["name"] = trimmed
    if basics is not None:
        item["basics"] = basics.strip()
    if appearance is not None:
        item["appearance"] = appearance.strip()
    save_characters(name, store)
    return item


def delete_character(name: str, character_id: str) -> dict:
    store = load_characters(name)
    before = len(store["characters"])
    store["characters"] = [item for item in store["characters"] if item["id"] != character_id]
    if len(store["characters"]) == before:
        raise BookError("找不到该人物")
    save_characters(name, store)
    return {"ok": True, "id": character_id, "characters": store["characters"]}


def add_patch(name: str, character_id: str, chapter_id: str, text: str, patch_id: str | None = None) -> dict:
    store = load_characters(name)
    item = _find(store, character_id)
    if not item:
        raise BookError("找不到该人物")
    body = (text or "").strip()
    if not body:
        raise BookError("请填写状态补丁")
    cid = validate_chapter_id(chapter_id)
    patch = {
        "id": patch_id or _next_id(store, "p"),
        "chapter_id": cid,
        "text": body,
    }
    # unique among all patches
    used = {p.get("id") for p in _all_patches(store)}
    if patch["id"] in used:
        patch["id"] = _fresh_id("p", used)
    item["patches"].append(patch)
    save_characters(name, store)
    return patch


def update_patch(name: str, character_id: str, patch_id: str, *, text: str | None = None, chapter_id: str | None = None) -> dict:
    store = load_characters(name)
    item = _find(store, character_id)
    if not item:
        raise BookError("找不到该人物")
    patch = next((p for p in item["patches"] if p["id"] == patch_id), None)
    if not patch:
        raise BookError("找不到该补丁")
    if text is not None:
        body = text.strip()
        if not body:
            raise BookError("请填写状态补丁")
        patch["text"] = body
    if chapter_id is not None:
        patch["chapter_id"] = validate_chapter_id(chapter_id)
    save_characters(name, store)
    return patch


def delete_patch(name: str, character_id: str, patch_id: str) -> dict:
    store = load_characters(name)
    item = _find(store, character_id)
    if not item:
        raise BookError("找不到该人物")
    before = len(item["patches"])
    item["patches"] = [p for p in item["patches"] if p["id"] != patch_id]
    if len(item["patches"]) == before:
        raise BookError("找不到该补丁")
    save_characters(name, store)
    return {"ok": True, "id": patch_id}


def confirm_patches(name: str, patches: Iterable[dict]) -> list[dict]:
    """Write user-confirmed patches only. Ignores empty or unknown characters."""
    written: list[dict] = []
    for raw in patches or []:
        if not isinstance(raw, dict):
            continue
        character_id = (raw.get("character_id") or "").strip()
        chapter_id = raw.get("chapter_id") or ""
        text = (raw.get("text") or "").strip()
        if not character_id or not text or not str(chapter_id).strip():
            continue
        written.append(add_patch(name, character_id, str(chapter_id), text))
    return written


def decorate_character(item: dict, path_ids: list[str]) -> dict:
    allowed = set(path_ids or [])
    patches = [dict(p) for p in (item.get("patches") or []) if p.get("chapter_id") in allowed]
    # keep path order
    order = {cid: i for i, cid in enumerate(path_ids or [])}
    patches.sort(key=lambda p: (order.get(p.get("chapter_id"), 10_000), p.get("id") or ""))
    public = _public_character(item)
    public["path_patches"] = patches
    public["current_state"] = format_current_state(patches)
    return public


def format_current_state(patches: list[dict]) -> str:
    if not patches:
        return ""
    lines = []
    for patch in patches:
        cid = patch.get("chapter_id") or ""
        try:
            label = f"第{int(cid)}章"
        except (TypeError, ValueError):
            label = cid or "未编号"
        lines.append(f"{label}：{patch.get('text') or ''}")
    return "\n".join(lines)


def format_character_prompt(characters: list[dict]) -> str:
    if not characters:
        return ""
    parts = []
    for item in characters:
        name = item.get("name") or "未命名"
        basics = (item.get("basics") or "").strip() or "（无）"
        appearance = (item.get("appearance") or "").strip()
        state = (item.get("current_state") or "").strip()
        if not state:
            state = "（沿本线尚无状态变化）"
        block = f"【{name}】\n基础设定：\n{basics}"
        if appearance:
            block += f"\n外形：\n{appearance}"
        block += f"\n当前状态（沿本线累积）：\n{state}"
        parts.append(block)
    return "\n\n".join(parts)


def resolve_path_ids(name: str, chapter_id: str | None) -> list[str]:
    if not chapter_id:
        return []
    try:
        cid = validate_chapter_id(chapter_id)
    except BookError:
        return []
    try:
        from app.tree import path_chapter_ids

        ids = path_chapter_ids(name, cid)
    except Exception:
        ids = None
    if ids is not None:
        return [str(item) for item in ids]
    try:
        number = int(cid)
    except ValueError:
        return [cid]
    if number < 1:
        return [cid]
    return [validate_chapter_id(str(i)) for i in range(1, number + 1)]


def _public_character(item: dict) -> dict:
    patches = []
    for raw in item.get("patches") or []:
        if not isinstance(raw, dict):
            continue
        pid = str(raw.get("id") or "").strip() or _fresh_id("p", set())
        chapter_id = str(raw.get("chapter_id") or "").strip()
        try:
            chapter_id = validate_chapter_id(chapter_id) if chapter_id else ""
        except BookError:
            chapter_id = chapter_id
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        patches.append({"id": pid, "chapter_id": chapter_id, "text": text})
    return {
        "id": str(item.get("id") or "").strip() or _fresh_id("c", set()),
        "name": str(item.get("name") or "").strip() or "未命名",
        "basics": str(item.get("basics") or ""),
        "appearance": str(item.get("appearance") or ""),
        "patches": patches,
    }


def _find(store: dict, character_id: str) -> dict | None:
    wanted = (character_id or "").strip()
    for item in store.get("characters") or []:
        if item.get("id") == wanted:
            return item
    return None


def _validate_name(name: str) -> str:
    trimmed = (name or "").strip()
    if not trimmed:
        raise BookError("请填写人物名称")
    if _NAME_INVALID.search(trimmed):
        raise BookError("人物名称不能换行")
    if len(trimmed) > 40:
        raise BookError("人物名称过长")
    return trimmed


def _all_patches(store: dict) -> list[dict]:
    patches: list[dict] = []
    for item in store.get("characters") or []:
        patches.extend(item.get("patches") or [])
    return patches


def _next_id(store: dict, prefix: str) -> str:
    used = set()
    if prefix == "c":
        used = {item.get("id") for item in store.get("characters") or []}
    else:
        used = {p.get("id") for p in _all_patches(store)}
    return _fresh_id(prefix, used)


def _fresh_id(prefix: str, used: set) -> str:
    n = 1
    while f"{prefix}{n}" in used:
        n += 1
    return f"{prefix}{n}"


def _safe_chapter_id(chapter_id: str) -> str | None:
    try:
        return validate_chapter_id(chapter_id)
    except BookError:
        return None
