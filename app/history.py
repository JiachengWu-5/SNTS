"""Version timeline for chapter bodies and outlines.

Snapshots are complete copies. Autosave of in-progress typing does not
create entries; only generate / continue / expand / revise / explicit save
and restore-safety points do.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app import books
from app.textutil import count_hanzi, first_heading

HISTORY_ROOT = "history"
CHAPTERS_SUB = "chapters"
OUTLINE_SUB = "outline"
PREVIEW_CHARS = 80

KIND_LABELS = {
    "generate": "生成",
    "continue": "续写",
    "expand": "扩写",
    "revise": "按意见修改",
    "save": "保存",
    "restore": "恢复前",
    "prior": "改前",
    "prev": "上一版",
    "saved": "已保存",
    "outline": "生成大纲",
    "outline_revise": "按思路修改",
    "outline_section": "写本章大纲",
    "outline_next": "写下一章大纲",
}

_KIND_RE = re.compile(r"^[a-z_]+$")
_ENTRY_RE = re.compile(r"^(\d{8}-\d{6})(?:-(\d+))*-([a-z_]+)$")


def _safe_kind(kind: str) -> str:
    kind = (kind or "save").strip().lower()
    if not _KIND_RE.match(kind) or kind not in KIND_LABELS:
        return "save"
    return kind


def chapter_dir(name: str, chapter_id: str) -> Path:
    cid = books.validate_chapter_id(chapter_id)
    path = books.ensure_book(name) / HISTORY_ROOT / CHAPTERS_SUB / cid
    path.mkdir(parents=True, exist_ok=True)
    return path


def outline_dir(name: str) -> Path:
    path = books.ensure_book(name) / HISTORY_ROOT / OUTLINE_SUB
    path.mkdir(parents=True, exist_ok=True)
    return path


def _stamp() -> str:
    now = datetime.now()
    return now.strftime("%Y%m%d-%H%M%S") + f"-{now.microsecond:06d}"


def _normalize(text: str | None) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _preview(text: str) -> str:
    compact = re.sub(r"\s+", " ", _normalize(text))
    if len(compact) <= PREVIEW_CHARS:
        return compact
    return compact[:PREVIEW_CHARS] + "…"


def _parse_entry_id(entry_id: str) -> tuple[str, str] | None:
    raw = (entry_id or "").strip()
    match = _ENTRY_RE.match(raw)
    if not match:
        return None
    return raw, match.group(3)


def _entry_path(folder: Path, entry_id: str) -> Path:
    parsed = _parse_entry_id(entry_id)
    if not parsed:
        raise books.BookError("找不到这个版本")
    path = folder / f"{parsed[0]}.md"
    if not path.is_file():
        raise books.BookError("找不到这个版本")
    return path


def _unique_path(folder: Path, kind: str, stamp: str | None = None) -> Path:
    kind = _safe_kind(kind)
    base = stamp or _stamp()
    path = folder / f"{base}-{kind}.md"
    if not path.exists():
        return path
    for index in range(1, 100):
        candidate = folder / f"{base}-{index}-{kind}.md"
        if not candidate.exists():
            return candidate
    raise books.BookError("无法写入时间线")


def _summarize(path: Path, text: str) -> dict:
    entry_id = path.stem
    parsed = _parse_entry_id(entry_id)
    kind = parsed[1] if parsed else "save"
    time_token = parsed[0].split("-")[0] + "-" + parsed[0].split("-")[1] if parsed else ""
    try:
        stamp = datetime.strptime(time_token, "%Y%m%d-%H%M%S")
        time_iso = stamp.isoformat(sep=" ")
        time_label = stamp.strftime("%m/%d %H:%M")
        if stamp.year != datetime.now().year:
            time_label = stamp.strftime("%Y/%m/%d %H:%M")
    except ValueError:
        time_iso = time_token
        time_label = time_token
    return {
        "id": entry_id,
        "kind": kind,
        "kind_label": KIND_LABELS.get(kind, kind),
        "time": time_iso,
        "time_label": time_label,
        "hanzi": count_hanzi(text),
        "title": first_heading(text) or "",
        "preview": _preview(text),
        "mtime": path.stat().st_mtime,
    }


def _latest_text(folder: Path) -> str:
    files = sorted(folder.glob("*.md"), key=lambda p: p.name, reverse=True)
    if not files:
        return ""
    return _normalize(books._read_text(files[0]))


def _write_snapshot(folder: Path, text: str, kind: str) -> dict | None:
    body = _normalize(text)
    if not body:
        return None
    if body == _latest_text(folder):
        return None
    path = _unique_path(folder, kind)
    books._write_text(path, body + "\n")
    return _summarize(path, body)


def capture_chapter(name: str, chapter_id: str, text: str, kind: str) -> dict | None:
    return _write_snapshot(chapter_dir(name, chapter_id), text, kind)


def capture_current_chapter(name: str, chapter_id: str, kind: str) -> dict | None:
    try:
        current = books.read_chapter(name, chapter_id).get("text") or ""
    except books.BookError:
        return None
    return capture_chapter(name, chapter_id, current, kind)


def capture_outline(name: str, text: str, kind: str) -> dict | None:
    return _write_snapshot(outline_dir(name), text, kind)


def capture_current_outline(name: str, kind: str) -> dict | None:
    try:
        current = books.read_outline(name)
    except books.BookError:
        return None
    return capture_outline(name, current, kind)


def record_chapter_change(name: str, chapter_id: str, before: str, after: str, kind: str) -> None:
    if _normalize(before):
        capture_chapter(name, chapter_id, before, "prior")
    capture_chapter(name, chapter_id, after, kind)


def record_outline_change(name: str, before: str, after: str, kind: str) -> None:
    if _normalize(before):
        capture_outline(name, before, "prior")
    capture_outline(name, after, kind)


def _import_chapter_legacy(name: str, chapter_id: str) -> None:
    folder = chapter_dir(name, chapter_id)
    existing_hashes = {_normalize(books._read_text(p)) for p in folder.glob("*.md")}
    cid = books.validate_chapter_id(chapter_id)
    legacy: list[tuple[str, Path]] = []
    prev = books.previous_chapter_path(name, cid)
    if prev.exists():
        legacy.append(("prev", prev))
    saved_latest = books.latest_saved_chapter_path(name, cid)
    if saved_latest.exists():
        legacy.append(("saved", saved_latest))
    archive_dir = books.saved_chapter_dir(name)
    if archive_dir.exists():
        for path in sorted(archive_dir.glob(f"{cid}-*.md")):
            legacy.append(("saved", path))
    for kind, path in legacy:
        body = _normalize(books._read_text(path))
        if not body or body in existing_hashes:
            continue
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
        dest = _unique_path(folder, kind, stamp=stamp)
        books._write_text(dest, body + "\n")
        existing_hashes.add(body)


def _import_outline_legacy(name: str) -> None:
    folder = outline_dir(name)
    existing_hashes = {_normalize(books._read_text(p)) for p in folder.glob("*.md")}
    prev = books.ensure_book(name) / books.PREV_OUTLINE_FILE
    if not prev.exists():
        return
    body = _normalize(books._read_text(prev))
    if not body or body in existing_hashes:
        return
    stamp = datetime.fromtimestamp(prev.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
    dest = _unique_path(folder, "prev", stamp=stamp)
    books._write_text(dest, body + "\n")



def _item_sort_key(item: dict) -> tuple:
    entry_id = item.get("id") or ""
    prefix = entry_id.rsplit("-", 1)[0] if "-" in entry_id else entry_id
    return (prefix, entry_id, item.get("mtime") or 0)


def list_chapter_history(name: str, chapter_id: str) -> dict:
    _import_chapter_legacy(name, chapter_id)
    folder = chapter_dir(name, chapter_id)
    items = []
    for path in folder.glob("*.md"):
        items.append(_summarize(path, books._read_text(path)))
    items.sort(key=_item_sort_key, reverse=True)
    return {"items": items, "scope": "chapter", "chapter": books.validate_chapter_id(chapter_id)}


def list_outline_history(name: str) -> dict:
    _import_outline_legacy(name)
    folder = outline_dir(name)
    items = []
    for path in folder.glob("*.md"):
        items.append(_summarize(path, books._read_text(path)))
    items.sort(key=_item_sort_key, reverse=True)
    return {"items": items, "scope": "outline"}


def read_chapter_entry(name: str, chapter_id: str, entry_id: str) -> dict:
    _import_chapter_legacy(name, chapter_id)
    path = _entry_path(chapter_dir(name, chapter_id), entry_id)
    text = books._read_text(path)
    item = _summarize(path, text)
    item["text"] = text
    return item


def read_outline_entry(name: str, entry_id: str) -> dict:
    _import_outline_legacy(name)
    path = _entry_path(outline_dir(name), entry_id)
    text = books._read_text(path)
    item = _summarize(path, text)
    item["text"] = text
    return item


def restore_chapter_entry(name: str, chapter_id: str, entry_id: str) -> dict:
    item = read_chapter_entry(name, chapter_id, entry_id)
    text = _normalize(item.get("text"))
    if not text:
        raise books.BookError("这个版本是空的，无法恢复")
    capture_current_chapter(name, chapter_id, "restore")
    books.backup_chapter(name, chapter_id)
    written = books.write_chapter(name, chapter_id, text + "\n")
    written["ok"] = True
    written["text"] = books.read_chapter(name, chapter_id)["text"]
    written["hanzi"] = count_hanzi(written["text"])
    written["restored_from"] = item["id"]
    return written


def restore_outline_entry(name: str, entry_id: str) -> dict:
    item = read_outline_entry(name, entry_id)
    text = _normalize(item.get("text"))
    if not text:
        raise books.BookError("这个版本是空的，无法恢复")
    capture_current_outline(name, "restore")
    books.backup_outline(name)
    written = books.write_outline(name, text + "\n")
    written["ok"] = True
    written["text"] = books.read_outline(name)
    written["hanzi"] = count_hanzi(written["text"])
    written["restored_from"] = item["id"]
    return written


def save_outline_snapshot(name: str, text: str | None = None) -> dict:
    if text is not None:
        incoming = _normalize(text)
        if not incoming:
            raise books.BookError("当前大纲没有可保存的内容")
        books.write_outline(name, incoming + "\n")
    current = _normalize(books.read_outline(name))
    if not current:
        raise books.BookError("当前大纲没有可保存的内容")
    entry = capture_outline(name, current, "save")
    if entry is None:
        items = list_outline_history(name).get("items") or []
        entry = items[0] if items else {"id": "", "hanzi": count_hanzi(current), "kind": "save"}
    return {"ok": True, "hanzi": count_hanzi(current), "saved_as": entry.get("id"), "kind": "save"}
