"""Helpers for worldview, path-aware outline append, and long-form prompt blocks."""

from __future__ import annotations

import json
import re

from app import books, characters, synopsis
from app.books import BookError
from app.textutil import count_hanzi


def read_world(name: str) -> str:
    return books.read_world(name)


def write_world(name: str, text: str) -> dict:
    return books.write_world(name, text)


def world_prompt_text(name: str, max_chars: int = 4000) -> str:
    text = (books.read_world(name) or "").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n……\n"
    return text


def character_prompt_text(name: str, chapter_id: str | None, *, inclusive: bool = False, max_chars: int = 4000) -> str:
    items = characters.list_characters(name, chapter_id, inclusive=inclusive)
    text = characters.format_character_prompt(items)
    if not text:
        return ""
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n……\n"
    return text


def synopsis_prompt_text(name: str, chapter_id: str | None, *, include_self: bool = False, max_chars: int = 2500) -> str:
    if not chapter_id:
        return ""
    text, _source = synopsis.nearest_path_synopsis(name, chapter_id, include_self=include_self)
    text = (text or "").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "\n……\n"
    return text


def format_longform_blocks(
    *,
    world: str = "",
    characters_text: str = "",
    synopsis_text: str = "",
) -> str:
    parts = []
    if (world or "").strip():
        parts.append(f"【世界观】\n{world.strip()}\n")
    if (characters_text or "").strip():
        parts.append(f"【人物（基础设定与当前这条线的状态）】\n{characters_text.strip()}\n")
    if (synopsis_text or "").strip():
        parts.append(f"【路径前情摘要】\n{synopsis_text.strip()}\n")
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def load_longform_blocks(
    name: str,
    chapter_id: str | None = None,
    *,
    for_writing: bool = True,
    include_characters: bool = True,
    include_synopsis: bool = True,
    world_max: int = 4000,
    character_max: int = 4000,
    synopsis_max: int = 2500,
) -> str:
    world = world_prompt_text(name, max_chars=world_max)
    chars = ""
    syn = ""
    if include_characters and chapter_id:
        chars = character_prompt_text(name, chapter_id, inclusive=not for_writing, max_chars=character_max)
    elif include_characters and not chapter_id:
        chars = character_prompt_text(name, None, inclusive=False, max_chars=character_max)
    if include_synopsis and chapter_id:
        syn = synopsis_prompt_text(name, chapter_id, include_self=not for_writing, max_chars=synopsis_max)
    return format_longform_blocks(world=world, characters_text=chars, synopsis_text=syn)


def setting_prompt_blocks(name: str, chapter_id: str | None = None, *, world_max: int = 4000, character_max: int = 4000) -> str:
    """Worldview and character cards only, for outline and planning prompts."""
    return load_longform_blocks(
        name,
        chapter_id,
        for_writing=True,
        include_synopsis=False,
        world_max=world_max,
        character_max=character_max,
    )


def isolate_new_outline_section(generated: str, existing: str) -> str:
    """Keep only the newly written outline chapter, never replacing the current outline."""
    generated = (generated or "").strip()
    existing = existing or ""
    if not generated:
        return ""
    preamble, new_chapters = books.split_outline_chapters(generated)
    if not existing.strip():
        if not new_chapters:
            return generated.strip() + "\n"
        first = new_chapters[0]["text"].strip()
        head = (preamble or "").strip()
        if head:
            return head + "\n\n" + first + "\n"
        return first + "\n"

    _old_preamble, old_chapters = books.split_outline_chapters(existing)
    old_headings = {_normalize_heading(ch.get("heading") or "") for ch in old_chapters}
    old_keys = {(_chapter_key(ch)) for ch in old_chapters}
    added = []
    for chapter in new_chapters:
        heading = _normalize_heading(chapter.get("heading") or "")
        key = _chapter_key(chapter)
        if heading and heading in old_headings:
            continue
        if key in old_keys and chapter.get("num") is not None:
            # same number + branch already present: skip unless this is clearly new text
            continue
        added.append(chapter)
    if not added and new_chapters:
        # Model may have returned only the new chapter with a heading that
        # coincidentally matches; if the existing outline already has that
        # heading, still refuse to treat it as a replacement.
        only = new_chapters[-1]
        if _normalize_heading(only.get("heading") or "") not in old_headings:
            added = [only]
    if not added:
        # No heading overlap — if the model omitted a heading, do not append raw text
        # that might be a full outline rewrite without recognizable chapters.
        if new_chapters:
            return ""
        if "###" in generated or books.CHAPTER_HEADING_RE.search(generated):
            return ""
        return ""
    return added[0]["text"].strip() + "\n"


def append_outline_section(existing: str, section: str) -> str:
    existing = existing or ""
    section = (section or "").strip()
    if not section:
        return existing
    if not existing.strip():
        return section + ("\n" if not section.endswith("\n") else "")
    text = existing
    if not text.endswith("\n"):
        text += "\n"
    if not text.endswith("\n\n"):
        text += "\n"
    return text + section + ("\n" if not section.endswith("\n") else "")


def next_outline_spec(name: str, from_chapter: str | None = None) -> dict:
    """Describe the next outline chapter along the current plot branch."""
    from app.tree import chinese_numeral, find_node, get_book_tree, path_outline_chapters

    outline = books.read_outline(name)
    idea = books.read_idea(name)
    tree = get_book_tree(name)
    nodes = tree.get("nodes") or []
    tip = _branch_tip(tree, from_chapter) if nodes else None
    if not outline.strip() or not nodes:
        return {
            "is_first": True,
            "num": 1,
            "branch": None,
            "parent_id": None,
            "heading_prefix": f"### 第{chinese_numeral(1)}章",
            "path_outline": "",
            "outline": outline,
            "idea": idea,
        }
    if tip is None:
        raise BookError("请先在情节脉络中点选要继续的那条线")
    children = tip.get("children") or []
    if len(children) > 1:
        raise BookError("当前节点已分出多条线，请先点选要继续的那条线的最后一章")
    num = (tip.get("num") or 0) + 1
    branch = (tip.get("branch") or "").strip() or None
    parent_id = tip.get("chapter_id") or tip.get("id")
    prefix = f"### 第{chinese_numeral(num)}章"
    if branch:
        prefix += f" {branch}"
    path_text = ""
    if parent_id:
        preamble, chapters = books.split_outline_chapters(outline)
        path_chapters = path_outline_chapters(name, parent_id, chapters)
        if path_chapters is None:
            path_chapters = chapters
        parts = []
        if (preamble or "").strip():
            parts.append(preamble.strip())
        for chapter in path_chapters or []:
            parts.append((chapter.get("text") or "").strip())
        path_text = "\n\n".join(p for p in parts if p)
    return {
        "is_first": False,
        "num": num,
        "branch": branch,
        "parent_id": parent_id,
        "heading_prefix": prefix,
        "path_outline": path_text or outline,
        "outline": outline,
        "idea": idea,
        "tip": tip,
    }


def this_outline_spec(name: str, chapter_id: str | None, notes: str = "") -> dict:
    """Describe the selected chapter's outline section, with path text before it only."""
    from app.tree import (
        chinese_numeral,
        find_node,
        get_book_tree,
        outline_section_for_chapter,
        path_outline_chapters,
    )

    if not chapter_id:
        raise BookError("请先在情节脉络中点选要写大纲的那一章")
    outline = books.read_outline(name)
    idea = books.read_idea(name)
    tree = get_book_tree(name)
    node = find_node(tree, chapter_id)
    if not node:
        raise BookError("找不到要写大纲的章节")
    section = outline_section_for_chapter(name, node.get("chapter_id") or node.get("id"), outline) or ""
    num = node.get("num") or 1
    branch = (node.get("branch") or "").strip() or None
    heading = (node.get("heading") or "").strip()
    if not heading:
        heading = f"### 第{chinese_numeral(num)}章"
        if branch:
            heading += f" {branch}"
        title = (node.get("title") or "").strip()
        if title:
            heading += f" {title}"
    preamble, chapters = books.split_outline_chapters(outline)
    parent_id = node.get("parent_id")
    path_text = ""
    if parent_id:
        path_chapters = path_outline_chapters(name, parent_id, chapters)
        if path_chapters is None:
            path_chapters = [ch for ch in (chapters or []) if ch.get("num") is not None and ch.get("num") < num]
        parts = []
        if (preamble or "").strip():
            parts.append(preamble.strip())
        for chapter in path_chapters or []:
            parts.append((chapter.get("text") or "").strip())
        path_text = "\n\n".join(p for p in parts if p)
    elif (preamble or "").strip():
        path_text = preamble.strip()
    direction = ""
    for line in (section or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("走向：") or stripped.startswith("走向:"):
            direction = stripped.split("：", 1)[-1].split(":", 1)[-1].strip()
            break
    return {
        "num": num,
        "branch": branch,
        "heading": heading,
        "section": section,
        "path_outline": path_text or preamble,
        "outline": outline,
        "idea": idea,
        "direction": direction,
        "notes": (notes or "").strip(),
        "node": node,
        "chapter_id": node.get("chapter_id") or node.get("id"),
    }


def replace_outline_section(existing: str, old_section: str, new_section: str) -> str:
    """Replace one chapter section in the outline, leaving every other chapter untouched."""
    existing = existing or ""
    old = (old_section or "").strip()
    new = (new_section or "").strip()
    if not new:
        return existing
    if not old:
        return append_outline_section(existing, new)
    idx = existing.find(old)
    if idx < 0:
        # try heading-only match
        first = old.splitlines()[0].strip() if old else ""
        if first and first in existing:
            start = existing.find(first)
            rest = existing[start:]
            nxt = books.CHAPTER_HEADING_RE.search(rest[len(first):])
            end = start + len(first) + (nxt.start() if nxt else len(rest) - len(first))
            return existing[:start] + new + ("\n" if not new.endswith("\n") else "") + existing[end:].lstrip("\n")
        return append_outline_section(existing, new)
    after = existing[idx + len(old):]
    return existing[:idx] + new + ("\n" if not new.endswith("\n") else "") + after.lstrip("\n")


def keep_branch_marker(old_section: str, new_section: str) -> str:
    """Preserve the <!-- 分支：… --> comment if the model omitted it."""
    import re as _re
    new = (new_section or "").strip()
    old = old_section or ""
    m = _re.search(r"<!--\s*分支：[^>]+-->", old)
    if not m:
        return new
    if "<!--" in new and "分支" in new:
        return new
    lines = new.splitlines()
    if lines:
        return lines[0] + "\n\n" + m.group(0) + "\n\n" + "\n".join(lines[1:]).strip()
    return m.group(0) + "\n\n" + new


def parse_consistency_output(text: str, known_characters: list[dict] | None = None) -> dict:
    """Parse model output into conflicts and proposed patches. Does not write anything."""
    raw = (text or "").strip()
    payload = _extract_json_object(raw)
    conflicts: list[dict] = []
    proposals: list[dict] = []
    if payload:
        for item in payload.get("conflicts") or []:
            parsed = _coerce_conflict(item)
            if parsed:
                conflicts.append(parsed)
        for item in payload.get("proposed_patches") or payload.get("patches") or []:
            parsed = _coerce_patch(item, known_characters or [])
            if parsed:
                proposals.append(parsed)
    elif raw:
        # Fallback: treat non-empty prose as a single note, never as a patch write.
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
        if cleaned and cleaned not in {"无", "没有", "无冲突", "没有冲突"}:
            conflicts.append({"text": cleaned, "related_character": ""})
    return {
        "conflicts": conflicts,
        "proposed_patches": proposals,
        "has_issues": bool(conflicts or proposals),
    }


def _branch_tip(tree: dict, from_chapter: str | None):
    from app.tree import find_node

    nodes = tree.get("nodes") or []
    if not nodes:
        return None
    node = find_node(tree, from_chapter) if from_chapter else None
    if node is None:
        node = nodes[-1]
    by_id = {item["id"]: item for item in nodes}
    seen = set()
    current = node
    while current and current["id"] not in seen:
        seen.add(current["id"])
        children = [cid for cid in (current.get("children") or []) if cid in by_id]
        if len(children) != 1:
            break
        current = by_id[children[0]]
    return current


def _normalize_heading(heading: str) -> str:
    text = (heading or "").strip()
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def _chapter_key(chapter: dict) -> tuple:
    heading = _normalize_heading(chapter.get("heading") or "")
    # drop leading 第X章 for a softer key
    rest = re.sub(r"^第\s*[0-9一二三四五六七八九十百零〇○两]+\s*章\s*", "", heading)
    return (chapter.get("num"), rest)


def _extract_json_object(text: str) -> dict | None:
    snippet = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", snippet, flags=re.S | re.I)
    if fenced:
        snippet = fenced.group(1)
    else:
        start = snippet.find("{")
        end = snippet.rfind("}")
        if start >= 0 and end > start:
            snippet = snippet[start : end + 1]
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _extract_json(text: str):
    """Parse a JSON object or array from model output, including fenced blocks."""
    snippet = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", snippet, flags=re.S | re.I)
    if fenced:
        snippet = fenced.group(1)
    else:
        arr_start = snippet.find("[")
        obj_start = snippet.find("{")
        if arr_start >= 0 and (obj_start < 0 or arr_start < obj_start):
            end = snippet.rfind("]")
            if end > arr_start:
                snippet = snippet[arr_start : end + 1]
        elif obj_start >= 0:
            end = snippet.rfind("}")
            if end > obj_start:
                snippet = snippet[obj_start : end + 1]
    try:
        data = json.loads(snippet)
    except json.JSONDecodeError:
        return None
    return data


def parse_character_cards(generated: str) -> list[dict]:
    """Extract light character cards (name + basics) from a model draft. No patches."""
    data = _extract_json(generated)
    if data is None:
        return []
    if isinstance(data, dict):
        items = data.get("characters") or data.get("people") or data.get("人物") or []
        if isinstance(items, dict):
            items = [{"name": k, "basics": v if isinstance(v, str) else ""} for k, v in items.items()]
    elif isinstance(data, list):
        items = data
    else:
        items = []
    result = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            name, basics = item.strip(), ""
        elif isinstance(item, dict):
            name = (
                item.get("name")
                or item.get("姓名")
                or item.get("character")
                or ""
            )
            name = str(name).strip()
            basics = (
                item.get("basics")
                or item.get("设定")
                or item.get("基础设定")
                or item.get("description")
                or ""
            )
            basics = str(basics).strip()
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        result.append({"name": name, "basics": basics})
    return result


def _coerce_conflict(item) -> dict | None:
    if isinstance(item, str):
        text = item.strip()
        return {"text": text, "related_character": ""} if text else None
    if not isinstance(item, dict):
        return None
    text = (item.get("text") or item.get("conflict") or item.get("message") or "").strip()
    if not text:
        return None
    related = (item.get("related_character") or item.get("character") or item.get("name") or "").strip()
    return {"text": text, "related_character": related}


def _coerce_patch(item, known: list[dict]) -> dict | None:
    if not isinstance(item, dict):
        return None
    text = (item.get("text") or item.get("patch") or "").strip()
    if not text:
        return None
    character_id = (item.get("character_id") or item.get("id") or "").strip()
    character_name = (item.get("character_name") or item.get("name") or item.get("character") or "").strip()
    if not character_id and character_name:
        for known_item in known:
            if known_item.get("name") == character_name:
                character_id = known_item.get("id") or ""
                break
    if not character_id:
        return None
    chapter_id = str(item.get("chapter_id") or item.get("chapter") or "").strip()
    try:
        if chapter_id:
            chapter_id = books.validate_chapter_id(chapter_id)
    except BookError:
        pass
    return {
        "character_id": character_id,
        "character_name": character_name,
        "chapter_id": chapter_id,
        "text": text,
    }



ENDING_MARKER = "<!-- 结局 -->"
ENDING_MARKER_RE = re.compile(r"<!--\s*结局\s*-->")
OUTLINE_UNTIL_SAFETY_MAX = 40


def outline_section_is_ending(section: str) -> bool:
    return bool(ENDING_MARKER_RE.search(section or ""))


def ensure_ending_marker(section: str) -> str:
    """If an ending marker is present, put the canonical marker on its own last line."""
    text = section or ""
    if not ENDING_MARKER_RE.search(text):
        return text
    cleaned = ENDING_MARKER_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).rstrip()
    return f"{cleaned}\n\n{ENDING_MARKER}\n"


def _positive_int(value, lo: int, hi: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < lo:
        return None
    return min(number, hi)


def outline_until_plan(
    current_num: int,
    *,
    target_chapters: int | None = None,
    target_book_chars: int | None = None,
    chapter_target_chars: int = 2000,
    written_new: int = 0,
) -> dict:
    """Plan the next auto-outline chapter and whether writing should stop first."""
    current = max(0, int(current_num or 0))
    next_num = current + 1
    chapters_cap = _positive_int(target_chapters, 1, 200)
    book_chars = _positive_int(target_book_chars, 1000, 20_000_000)
    per_chapter = max(200, int(chapter_target_chars or 2000))
    word_cap = None
    if book_chars:
        word_cap = max(1, (book_chars + per_chapter - 1) // per_chapter)
    limit_num = None
    if chapters_cap and word_cap:
        limit_num = min(chapters_cap, word_cap)
    elif chapters_cap:
        limit_num = chapters_cap
    elif word_cap:
        limit_num = word_cap
    already_complete = bool(limit_num and current >= limit_num)
    safety_hit = limit_num is None and int(written_new or 0) >= OUTLINE_UNTIL_SAFETY_MAX
    remaining = None if limit_num is None else max(0, limit_num - current)
    if already_complete or safety_hit:
        remaining = 0
    parts: list[str] = []
    if limit_num and not already_complete and not safety_hit:
        parts.append(f"请把当前这条线安排到大约第{limit_num}章收尾。")
        if book_chars:
            parts.append(f"全书大约{book_chars}字，按每章约{per_chapter}字来安排篇幅。")
        parts.append(f"现在要写的是第{next_num}章，大约还剩{max(1, remaining or 1)}章（含本章）。")
        if remaining is not None and remaining <= 1:
            parts.append("这是按目标安排的最后一章，请在本章把这条线的故事收束。")
        elif remaining is not None and remaining <= 3:
            parts.append("已经接近目标，请开始收束，不要再开很大的新情节。")
    elif not already_complete and not safety_hit:
        parts.append("请顺着思路一直写到故事自然收尾。若本章还不是收尾，章末留下钩子，不要提前结束。")
    return {
        "current_num": current,
        "next_num": next_num,
        "limit_num": limit_num,
        "remaining": remaining,
        "already_complete": already_complete,
        "safety_hit": safety_hit,
        "pacing_note": "".join(parts),
    }


BODY_UNTIL_SAFETY_MAX = OUTLINE_UNTIL_SAFETY_MAX


def chapter_has_body(name: str, chapter_id: str) -> bool:
    try:
        path = books.chapter_path(name, chapter_id)
    except BookError:
        return False
    if not path.exists():
        return False
    return bool(path.read_text(encoding="utf-8").strip())


def branch_path_nodes(name: str, from_chapter: str | None = None) -> list[dict]:
    """Oldest-first nodes on the current line: ancestors, the selected node, then unique children."""
    from app.tree import find_node, get_book_tree

    tree = get_book_tree(name)
    nodes = tree.get("nodes") or []
    if not nodes:
        return []
    if tree.get("has_forks") and not from_chapter:
        raise BookError("当前大纲已有分叉，请先在情节脉络中点选要继续的那条线")
    by_id = {item["id"]: item for item in nodes}
    node = find_node(tree, from_chapter) if from_chapter else None
    if from_chapter and node is None:
        raise BookError("请先在情节脉络中点选要继续的那条线")
    if node is None:
        roots = [item for item in nodes if not item.get("parent_id")]
        start = roots[0] if len(roots) == 1 else (roots[0] if roots else nodes[0])
        # If the tree is a unique chain, start at the root and walk down.
        node = start
    path: list[dict] = []
    seen: set[str] = set()
    current = node
    ancestors: list[dict] = []
    while current and current.get("parent_id") and current["id"] not in seen:
        seen.add(current["id"])
        parent = by_id.get(current["parent_id"])
        if not parent:
            break
        ancestors.append(parent)
        current = parent
    ancestors.reverse()
    path.extend(ancestors)
    if node["id"] not in {item["id"] for item in path}:
        path.append(node)
    current = node
    seen = {item["id"] for item in path}
    while current:
        children = [cid for cid in (current.get("children") or []) if cid in by_id]
        if len(children) != 1:
            break
        child = by_id[children[0]]
        if child["id"] in seen:
            break
        path.append(child)
        seen.add(child["id"])
        current = child
    return path


def ensure_branch_chapter_ids(name: str, from_chapter: str | None = None) -> tuple[list[dict], str | None]:
    """Allocate chapter files for the current line, without overwriting existing bodies."""
    from app.tree import find_node, get_book_tree, open_tree_node

    path_nodes = branch_path_nodes(name, from_chapter)
    tree = get_book_tree(name)
    start = find_node(tree, from_chapter) if from_chapter else None
    start_index = start.get("outline_index") if start else None
    resolved = None
    ensured: list[dict] = []
    for node in path_nodes:
        tree = get_book_tree(name)
        current = next(
            (item for item in (tree.get("nodes") or []) if item.get("outline_index") == node.get("outline_index")),
            None,
        )
        if current is None:
            current = find_node(tree, node.get("chapter_id") or node.get("id"))
        if current is None:
            continue
        opened = open_tree_node(name, current["id"])
        cid = opened.get("id")
        tree = get_book_tree(name)
        fresh = find_node(tree, cid) if cid else None
        if fresh is None:
            continue
        ensured.append(fresh)
        if start_index is not None and fresh.get("outline_index") == start_index:
            resolved = fresh.get("chapter_id") or fresh.get("id")
    return ensured, resolved


def body_until_plan(
    name: str,
    *,
    from_chapter: str | None = None,
    target_chapters: int | None = None,
    target_book_chars: int | None = None,
    chapter_target_chars: int = 2000,
    written_new: int = 0,
) -> dict:
    """Decide which empty chapter on the current line should be written next."""
    from app.tree import get_book_tree

    path_nodes, resolved_from = ensure_branch_chapter_ids(name, from_chapter)
    tree = get_book_tree(name)
    by_id = {item["id"]: item for item in (tree.get("nodes") or [])}
    last = path_nodes[-1] if path_nodes else None
    stopped_at_fork = False
    if last:
        children = [cid for cid in (last.get("children") or []) if cid in by_id]
        stopped_at_fork = len(children) > 1

    items: list[dict] = []
    path_hanzi = 0
    for node in path_nodes:
        raw_id = node.get("chapter_id") or ""
        if not raw_id:
            continue
        try:
            cid = books.validate_chapter_id(raw_id)
        except BookError:
            continue
        has_body = chapter_has_body(name, cid)
        hanzi = 0
        if has_body:
            try:
                hanzi = int(books.read_chapter(name, cid).get("hanzi") or 0)
            except BookError:
                hanzi = 0
        path_hanzi += hanzi
        num = node.get("num")
        try:
            num = int(num) if num is not None else int(cid)
        except (TypeError, ValueError):
            num = int(cid)
        items.append(
            {
                "id": cid,
                "node_id": node.get("id"),
                "num": num,
                "has_body": has_body,
                "hanzi": hanzi,
                "title": node.get("display_title") or node.get("title") or f"第{num}章",
            }
        )

    chapters_cap = _positive_int(target_chapters, 1, 200)
    book_chars = _positive_int(target_book_chars, 1000, 20_000_000)
    has_cap = chapters_cap is not None or book_chars is not None
    chars_reached = bool(book_chars and path_hanzi >= book_chars)
    pending: list[dict] = []
    skipped = 0
    for item in items:
        if chapters_cap and item["num"] > chapters_cap:
            continue
        if item["has_body"]:
            skipped += 1
            continue
        pending.append(item)
    if chars_reached:
        pending = []

    safety_hit = (not has_cap) and int(written_new or 0) >= BODY_UNTIL_SAFETY_MAX
    next_item = None if safety_hit or not pending else pending[0]
    already_complete = next_item is None and not safety_hit
    reason = None
    if already_complete:
        uncapped_empty = [item for item in items if not item["has_body"]]
        if not items:
            reason = "empty_path"
        elif not uncapped_empty:
            reason = "all_written"
        elif chars_reached:
            reason = "chars"
        elif chapters_cap:
            reason = "cap"
        else:
            reason = "all_written"
    return {
        "items": items,
        "pending": pending,
        "next": next_item,
        "skipped": skipped,
        "path_hanzi": path_hanzi,
        "already_complete": already_complete,
        "complete_reason": reason,
        "safety_hit": safety_hit,
        "stopped_at_fork": stopped_at_fork,
        "remaining": 0 if next_item is None else len(pending),
        "has_cap": has_cap,
        "from_chapter": resolved_from or from_chapter,
    }


# Re-export count for callers that format messages.
hanzi = count_hanzi
