"""Book folder layout, chapter files, and settings on disk."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app.config import (
    DEFAULT_SETTINGS,
    RESERVED_DIR_NAMES,
    RESERVED_FILE_NAMES,
    article_root,
)
from app.textutil import count_hanzi, count_visible_hanzi, first_heading

IDEA_FILE = "思路.md"
OUTLINE_FILE = "大纲.md"
PREV_OUTLINE_FILE = "大纲.prev.md"
WORLD_FILE = "世界观.md"
SETTINGS_FILE = "settings.json"
STYLE_DIR = "style"
CHAPTERS_DIR = "chapters"

_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)


class BookError(ValueError):
    pass


def validate_book_name(name: str) -> str:
    if name is None:
        raise BookError("书名不能为空")
    name = name.strip()
    if not name:
        raise BookError("书名不能为空")
    if name in {".", ".."}:
        raise BookError("书名不合法")
    if name in RESERVED_DIR_NAMES or name.lower() in RESERVED_FILE_NAMES:
        raise BookError("该书名被系统保留")
    if _INVALID_NAME.search(name):
        raise BookError("书名不能包含特殊路径字符")
    if name.upper() in _WINDOWS_RESERVED:
        raise BookError("书名不合法")
    return name


def _root() -> Path:
    path = article_root()
    path.mkdir(parents=True, exist_ok=True)
    return path


def book_dir(name: str) -> Path:
    name = validate_book_name(name)
    base = _root().resolve()
    path = (base / name).resolve()
    if path.parent != base and not str(path).startswith(str(base) + "\\") and not str(path).startswith(str(base) + "/"):
        raise BookError("书名不合法")
    if path == base:
        raise BookError("书名不合法")
    return path


def list_books() -> list[dict]:
    root = _root()
    books = []
    if not root.exists():
        return books
    for item in sorted(root.iterdir(), key=lambda p: p.name):
        if not item.is_dir():
            continue
        if item.name in RESERVED_DIR_NAMES:
            continue
        try:
            validate_book_name(item.name)
        except BookError:
            continue
        books.append(_book_summary(item))
    return books


def _book_summary(path: Path) -> dict:
    idea = _read_text(path / IDEA_FILE)
    outline = _read_text(path / OUTLINE_FILE)
    chapters = list_chapters(path.name) if (path / CHAPTERS_DIR).exists() else []
    return {
        "name": path.name,
        "idea_hanzi": count_hanzi(idea),
        "outline_hanzi": count_hanzi(outline),
        "chapter_count": len(chapters),
        "chapter_hanzi": sum(ch["hanzi"] for ch in chapters),
    }


def create_book(name: str) -> dict:
    path = book_dir(name)
    if path.exists():
        raise BookError("已有同名作品")
    path.mkdir(parents=True)
    (path / STYLE_DIR).mkdir()
    (path / CHAPTERS_DIR).mkdir()
    _write_text(path / IDEA_FILE, "")
    _write_text(path / OUTLINE_FILE, "")
    _write_text(path / WORLD_FILE, "")
    save_settings(name, dict(DEFAULT_SETTINGS))
    return get_book(name)


def ensure_book(name: str) -> Path:
    path = book_dir(name)
    if not path.exists() or not path.is_dir():
        raise BookError("作品不存在")
    (path / STYLE_DIR).mkdir(exist_ok=True)
    (path / CHAPTERS_DIR).mkdir(exist_ok=True)
    if not (path / IDEA_FILE).exists():
        _write_text(path / IDEA_FILE, "")
    if not (path / OUTLINE_FILE).exists():
        _write_text(path / OUTLINE_FILE, "")
    if not (path / WORLD_FILE).exists():
        _write_text(path / WORLD_FILE, "")
    if not (path / SETTINGS_FILE).exists():
        save_settings(name, dict(DEFAULT_SETTINGS))
    return path


def get_book(name: str) -> dict:
    path = ensure_book(name)
    idea = _read_text(path / IDEA_FILE)
    outline = _read_text(path / OUTLINE_FILE)
    world = _read_text(path / WORLD_FILE)
    chapters = list_chapters(name)
    result = {
        "name": path.name,
        "idea": idea,
        "outline": outline,
        "world": world,
        "settings": load_settings(name),
        "chapters": chapters,
        "idea_hanzi": count_hanzi(idea),
        "outline_hanzi": count_hanzi(outline),
        "world_hanzi": count_hanzi(world),
    }
    try:
        from app.tree import get_book_tree
        result["tree"] = get_book_tree(name)
    except Exception:
        result["tree"] = {"version": 1, "nodes": [], "has_forks": False}
    return result


def read_idea(name: str) -> str:
    return _read_text(ensure_book(name) / IDEA_FILE)


def write_idea(name: str, text: str) -> dict:
    _write_text(ensure_book(name) / IDEA_FILE, text or "")
    return {"ok": True, "hanzi": count_hanzi(text)}


def read_world(name: str) -> str:
    return _read_text(ensure_book(name) / WORLD_FILE)


def write_world(name: str, text: str) -> dict:
    _write_text(ensure_book(name) / WORLD_FILE, text or "")
    return {"ok": True, "hanzi": count_hanzi(text)}


def read_outline(name: str) -> str:
    return _read_text(ensure_book(name) / OUTLINE_FILE)


def write_outline(name: str, text: str) -> dict:
    previous = read_outline(name)
    if previous.strip() and not (text or "").strip():
        backup_outline(name)
        try:
            from app.history import capture_outline
            capture_outline(name, previous, "clear")
        except Exception:
            pass
    _write_text(ensure_book(name) / OUTLINE_FILE, text or "")
    result = {"ok": True, "hanzi": count_hanzi(text)}
    try:
        from app.tree import sync_book_tree
        result["tree"] = sync_book_tree(name)
    except Exception:
        # Tree sync must not block saving the outline itself.
        pass
    return result


def backup_outline(name: str) -> bool:
    current = read_outline(name).strip()
    if not current:
        return False
    _write_text(ensure_book(name) / PREV_OUTLINE_FILE, current + "\n")
    return True


def has_previous_outline(name: str) -> bool:
    return bool(_read_text(ensure_book(name) / PREV_OUTLINE_FILE).strip())


def restore_previous_outline(name: str) -> dict:
    previous = _read_text(ensure_book(name) / PREV_OUTLINE_FILE).strip()
    if not previous:
        raise BookError("没有可恢复的上一版大纲")
    current = read_outline(name).strip()
    if current and current != previous:
        from app.history import capture_outline
        capture_outline(name, current, "restore")
        _write_text(ensure_book(name) / PREV_OUTLINE_FILE, current + "\n")
    written = write_outline(name, previous + "\n")
    result = {"ok": True, "outline": read_outline(name), "hanzi": count_hanzi(read_outline(name))}
    if "tree" in written:
        result["tree"] = written["tree"]
    return result


def load_settings(name: str) -> dict:
    path = book_dir(name) / SETTINGS_FILE
    data = dict(DEFAULT_SETTINGS)
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    data["temperature"] = _clamp_float(data.get("temperature"), 0.0, 2.0, DEFAULT_SETTINGS["temperature"])
    data["chapter_target_chars"] = _clamp_int(
        data.get("chapter_target_chars"), 200, 20000, DEFAULT_SETTINGS["chapter_target_chars"]
    )
    data["enable_thinking"] = bool(data.get("enable_thinking"))
    llm_url = str(data.get("llm_url") or DEFAULT_SETTINGS["llm_url"]).strip()
    data["llm_url"] = llm_url or DEFAULT_SETTINGS["llm_url"]
    styles = data.get("selected_styles")
    if not isinstance(styles, list):
        data["selected_styles"] = list(DEFAULT_SETTINGS["selected_styles"])
    else:
        data["selected_styles"] = [str(s) for s in styles]
    return data


def save_settings(name: str, settings: dict) -> dict:
    path = book_dir(name)
    path.mkdir(parents=True, exist_ok=True)
    merged = load_settings(name) if (path / SETTINGS_FILE).exists() else dict(DEFAULT_SETTINGS)
    if settings:
        merged.update(settings)
        # Re-load through validation
        (path / SETTINGS_FILE).write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        merged = load_settings(name)
    else:
        (path / SETTINGS_FILE).write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return merged


def list_chapters(name: str) -> list[dict]:
    path = ensure_book(name) / CHAPTERS_DIR
    items = []
    if not path.exists():
        return items
    for file in sorted(path.glob("*.md")):
        chapter_id = file.stem
        if not re.fullmatch(r"\d+", chapter_id):
            continue
        text = _read_text(file)
        items.append(
            {
                "id": chapter_id,
                "filename": file.name,
                "title": first_heading(text) or f"第{int(chapter_id)}章",
                "hanzi": count_visible_hanzi(text),
                "empty": not text.strip(),
            }
        )
    return items


def chapter_path(name: str, chapter_id: str) -> Path:
    cid = validate_chapter_id(chapter_id)
    return ensure_book(name) / CHAPTERS_DIR / f"{cid}.md"


def validate_chapter_id(chapter_id: str) -> str:
    raw = str(chapter_id).strip()
    if not re.fullmatch(r"\d+", raw):
        raise BookError("章节编号不合法")
    return raw.zfill(2) if len(raw) <= 2 else raw


def read_chapter(name: str, chapter_id: str) -> dict:
    path = chapter_path(name, chapter_id)
    if not path.exists():
        raise BookError("章节不存在")
    text = _read_text(path)
    return {
        "id": path.stem,
        "filename": path.name,
        "text": text,
        "hanzi": count_visible_hanzi(text),
        "title": first_heading(text) or f"第{int(path.stem)}章",
    }


def write_chapter(name: str, chapter_id: str, text: str) -> dict:
    path = chapter_path(name, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, text or "")
    return {
        "id": path.stem,
        "filename": path.name,
        "hanzi": count_visible_hanzi(text),
        "title": first_heading(text) or f"第{int(path.stem)}章",
    }


def previous_chapter_path(name: str, chapter_id: str) -> Path:
    cid = validate_chapter_id(chapter_id)
    return ensure_book(name) / CHAPTERS_DIR / f"{cid}.prev.md"


def backup_chapter(name: str, chapter_id: str) -> bool:
    try:
        current = read_chapter(name, chapter_id)["text"].strip()
    except BookError:
        return False
    if not current:
        return False
    path = previous_chapter_path(name, chapter_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, current + "\n")
    return True


def has_previous_chapter(name: str, chapter_id: str) -> bool:
    path = previous_chapter_path(name, chapter_id)
    return path.exists() and bool(_read_text(path).strip())


def restore_previous_chapter(name: str, chapter_id: str) -> dict:
    path = previous_chapter_path(name, chapter_id)
    previous = _read_text(path).strip() if path.exists() else ""
    if not previous:
        raise BookError("没有可恢复的上一版正文")
    try:
        current = read_chapter(name, chapter_id)["text"].strip()
    except BookError:
        current = ""
    if current and current != previous:
        from app.history import capture_chapter
        capture_chapter(name, chapter_id, current, "restore")
        _write_text(path, current + "\n")
    written = write_chapter(name, chapter_id, previous + "\n")
    written["ok"] = True
    written["text"] = read_chapter(name, chapter_id)["text"]
    written["hanzi"] = count_visible_hanzi(written["text"])
    return written


def saved_chapter_dir(name: str) -> Path:
    return ensure_book(name) / CHAPTERS_DIR / "saved"


def latest_saved_chapter_path(name: str, chapter_id: str) -> Path:
    cid = validate_chapter_id(chapter_id)
    return ensure_book(name) / CHAPTERS_DIR / f"{cid}.saved.md"


def save_chapter_snapshot(name: str, chapter_id: str, text: str | None = None) -> dict:
    cid = validate_chapter_id(chapter_id)
    if text is not None:
        incoming = text.strip()
        if not incoming:
            raise BookError("当前章节没有可保存的正文")
        write_chapter(name, cid, incoming + "\n")
    try:
        current = read_chapter(name, cid)
    except BookError as exc:
        raise BookError("当前章节没有可保存的正文") from exc
    body = (current.get("text") or "").strip()
    if not body:
        raise BookError("当前章节没有可保存的正文")
    payload = body + "\n"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive_dir = saved_chapter_dir(name)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = archive_dir / f"{cid}-{stamp}.md"
    _write_text(archive, payload)
    latest = latest_saved_chapter_path(name, cid)
    _write_text(latest, payload)
    from app.history import capture_chapter
    capture_chapter(name, cid, payload, "save")
    return {
        "ok": True,
        "id": cid,
        "hanzi": count_visible_hanzi(payload),
        "title": current.get("title") or f"第{int(cid)}章",
        "saved_as": archive.name,
        "time": stamp,
    }


def restore_saved_chapter(name: str, chapter_id: str) -> dict:
    cid = validate_chapter_id(chapter_id)
    path = latest_saved_chapter_path(name, cid)
    saved = _read_text(path).strip() if path.exists() else ""
    if not saved:
        archive_dir = saved_chapter_dir(name)
        archives = sorted(archive_dir.glob(f"{cid}-*.md")) if archive_dir.exists() else []
        if archives:
            saved = _read_text(archives[-1]).strip()
    if not saved:
        raise BookError("没有已保存的本章存档")
    from app.history import capture_current_chapter
    capture_current_chapter(name, cid, "restore")
    backup_chapter(name, cid)
    written = write_chapter(name, cid, saved + "\n")
    written["ok"] = True
    written["text"] = read_chapter(name, cid)["text"]
    written["hanzi"] = count_visible_hanzi(written["text"])
    return written


_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "○": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}

CHAPTER_HEADING_RE = re.compile(
    r"(?m)^(#{2,4})\s*第\s*([0-9]+|[一二三四五六七八九十百零〇○两]+)\s*章[^\n]*$"
)


def parse_chapter_token(token: str) -> int | None:
    raw = (token or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if any(ch not in _CN_DIGITS and ch not in {"十", "百"} for ch in raw):
        return None
    if raw == "十":
        return 10
    if raw.startswith("十"):
        return 10 + _CN_DIGITS.get(raw[1:], 0)
    if "百" in raw:
        left, _, right = raw.partition("百")
        hundreds = _CN_DIGITS.get(left, 1) if left else 1
        if not right:
            return hundreds * 100
        if right.startswith("十") or "十" in right:
            return hundreds * 100 + (parse_chapter_token(right) or 0)
        return hundreds * 100 + _CN_DIGITS.get(right, 0)
    if "十" in raw:
        left, _, right = raw.partition("十")
        tens = _CN_DIGITS.get(left, 0)
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(raw) == 1:
        return _CN_DIGITS.get(raw)
    return None


def split_outline_chapters(outline_text: str) -> tuple[str, list[dict]]:
    text = outline_text or ""
    matches = list(CHAPTER_HEADING_RE.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()]
    chapters: list[dict] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        heading = match.group(0).strip()
        section = text[match.start() : end].strip()
        chapters.append(
            {
                "num": parse_chapter_token(match.group(2)),
                "heading": heading,
                "text": section,
            }
        )
    return preamble, chapters


def outline_has_chapter(outline_text: str, num: int) -> bool:
    _, chapters = split_outline_chapters(outline_text)
    return any(ch.get("num") == num for ch in chapters)


def outline_chapter_section(outline_text: str, num: int) -> str:
    _, chapters = split_outline_chapters(outline_text)
    candidates = [ch for ch in chapters if ch.get("num") == num]
    if not candidates:
        return ""

    def score(ch: dict) -> tuple[int, int, int]:
        text = ch["text"]
        heading = ch["heading"]
        thin_placeholder = ("待拟标题" in heading or "待补充" in text) and count_hanzi(text) < 40
        return (0 if thin_placeholder else 1, count_hanzi(text), len(text))

    return max(candidates, key=score)["text"]


def add_chapter_slot(name: str, title: str = "") -> dict:
    existing = list_chapters(name)
    next_num = 1
    if existing:
        next_num = max(int(ch["id"]) for ch in existing) + 1
    chapter_id = f"{next_num:02d}"
    heading = (title or "").strip() or f"第{next_num}章 待拟标题"
    if not heading.startswith("第"):
        heading = f"第{next_num}章 {heading}"
    write_chapter(name, chapter_id, "")
    outline = read_outline(name)
    if not outline_has_chapter(outline, next_num):
        block = f"\n\n### {heading}\n\n（待补充）\n"
        if outline.strip() and not outline.endswith("\n"):
            outline += "\n"
        write_outline(name, outline + block)
    result = {
        "id": chapter_id,
        "title": heading,
        "outline": read_outline(name),
        "chapters": list_chapters(name),
    }
    try:
        from app.tree import get_book_tree
        result["tree"] = get_book_tree(name)
    except Exception:
        pass
    return result


def chapter_has_backup(name: str, chapter_id: str) -> bool:
    cid = validate_chapter_id(chapter_id)
    if has_previous_chapter(name, cid):
        return True
    latest = latest_saved_chapter_path(name, cid)
    if latest.exists() and _read_text(latest).strip():
        return True
    archive_dir = saved_chapter_dir(name)
    if archive_dir.exists() and any(archive_dir.glob(f"{cid}-*.md")):
        return True
    return False


def delete_chapter(name: str, chapter_id: str) -> dict:
    """Delete a chapter body/file. Keep the outline node and any backups. Do not renumber."""
    cid = validate_chapter_id(chapter_id)
    path = chapter_path(name, cid)
    if not path.exists():
        raise BookError("章节不存在")
    current = _read_text(path).strip()
    backed_up = False
    if current:
        save_chapter_snapshot(name, cid, current)
        backed_up = backup_chapter(name, cid) or True
    path.unlink()
    tree = None
    try:
        from app.tree import sync_book_tree
        tree = sync_book_tree(name)
    except Exception:
        tree = None
    return {
        "ok": True,
        "id": cid,
        "backed_up": bool(backed_up),
        "has_backup": chapter_has_backup(name, cid),
        "chapters": list_chapters(name),
        "tree": tree,
        "outline": read_outline(name),
    }


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _clamp_float(value, lo, hi, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _clamp_int(value, lo, hi, default):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))
