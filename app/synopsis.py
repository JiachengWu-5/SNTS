"""Per-path rolling synopses, stored beside the plot tree rather than in chapter bodies."""

from __future__ import annotations

import json

from app.books import BookError, ensure_book, validate_chapter_id, _read_text, _write_text
from app.characters import resolve_path_ids

SYNOPSIS_FILE = "路径摘要.json"
SYNOPSIS_VERSION = 1


def synopsis_path(name: str):
    return ensure_book(name) / SYNOPSIS_FILE


def load_synopses(name: str) -> dict:
    path = synopsis_path(name)
    if not path.exists():
        return {"version": SYNOPSIS_VERSION, "paths": {}}
    try:
        data = json.loads(_read_text(path) or "{}")
    except json.JSONDecodeError:
        return {"version": SYNOPSIS_VERSION, "paths": {}}
    if not isinstance(data, dict):
        return {"version": SYNOPSIS_VERSION, "paths": {}}
    raw = data.get("paths")
    if not isinstance(raw, dict):
        raw = {}
    paths = {}
    for key, value in raw.items():
        try:
            cid = validate_chapter_id(str(key))
        except BookError:
            continue
        text = value if isinstance(value, str) else ""
        paths[cid] = text
    return {"version": SYNOPSIS_VERSION, "paths": paths}


def save_synopses(name: str, store: dict) -> dict:
    paths = {}
    for key, value in (store.get("paths") or {}).items():
        try:
            cid = validate_chapter_id(str(key))
        except BookError:
            continue
        paths[cid] = value if isinstance(value, str) else ""
    payload = {"version": SYNOPSIS_VERSION, "paths": paths}
    _write_text(synopsis_path(name), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def read_synopsis(name: str, chapter_id: str) -> str:
    cid = validate_chapter_id(chapter_id)
    return (load_synopses(name)["paths"].get(cid) or "").strip()


def write_synopsis(name: str, chapter_id: str, text: str) -> dict:
    cid = validate_chapter_id(chapter_id)
    store = load_synopses(name)
    store["paths"][cid] = (text or "").rstrip()
    if store["paths"][cid] and not store["paths"][cid].endswith("\n"):
        # keep file values as plain strings without forcing a newline in the JSON value
        pass
    save_synopses(name, store)
    return {"ok": True, "chapter_id": cid, "text": store["paths"][cid], "hanzi": _hanzi(store["paths"][cid])}


def nearest_path_synopsis(name: str, chapter_id: str, *, include_self: bool = False) -> tuple[str, str | None]:
    """Return (text, source_chapter_id) walking the current path backwards.

    Sibling branches are never consulted. If include_self is false, skip the
    current chapter so writing chapter N uses the summary accumulated up to N-1.
    """
    try:
        cid = validate_chapter_id(chapter_id)
    except BookError:
        return "", None
    path_ids = resolve_path_ids(name, cid)
    if not path_ids:
        path_ids = [cid]
    if not include_self:
        if path_ids and path_ids[-1] == cid:
            path_ids = path_ids[:-1]
    store = load_synopses(name)["paths"]
    for item in reversed(path_ids):
        text = (store.get(item) or "").strip()
        if text:
            return text, item
    return "", None


def _hanzi(text: str) -> int:
    from app.textutil import count_hanzi

    return count_hanzi(text or "")
