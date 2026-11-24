"""Plot tree derived from a book's outline: forks, path context, and branch edits."""

from __future__ import annotations

import json
import re
from typing import Any

from app.books import (
    BookError,
    CHAPTER_HEADING_RE,
    backup_outline,
    book_dir,
    chapter_path,
    ensure_book,
    has_previous_chapter,
    latest_saved_chapter_path,
    list_chapters,
    parse_chapter_token,
    read_outline,
    saved_chapter_dir,
    split_outline_chapters,
    validate_chapter_id,
    write_chapter,
    write_outline,
)

TREE_FILE = "tree.json"
TREE_VERSION = 1

BRANCH_LABEL_RE = re.compile(
    r"^[【\[\s·]*(?P<label>(?:[一二三四五六七八九十百零〇○两\d]+结局|结局[ABCDE甲乙丙丁一二三四五六七八九十0-9]+))[】\]\s·]*"
)
BRANCH_META_RE = re.compile(
    r"<!--\s*分支：\s*(?P<label>[^；;>]+?)\s*(?:[；;]\s*分叉于：\s*(?P<parent>[0-9]+))?\s*-->"
)
_CN_ONES = "零一二三四五六七八九"


def chinese_numeral(num: int) -> str:
    n = int(num)
    if n < 0:
        return str(n)
    if n < 10:
        return _CN_ONES[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_ONES[n - 10]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _CN_ONES[tens] + "十" + (_CN_ONES[ones] if ones else "")
    return str(n)


def tree_path(name: str):
    return ensure_book(name) / TREE_FILE


def load_tree(name: str) -> dict:
    path = tree_path(name)
    if not path.exists():
        return empty_tree()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty_tree()
    if not isinstance(data, dict):
        return empty_tree()
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        data["nodes"] = []
    data.setdefault("version", TREE_VERSION)
    return data


def save_tree(name: str, tree: dict) -> dict:
    payload = {
        "version": TREE_VERSION,
        "nodes": [ _public_node(node) for node in (tree.get("nodes") or []) ],
        "has_forks": bool(tree.get("has_forks")),
    }
    path = tree_path(name)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return decorate_tree(name, payload)


def empty_tree() -> dict:
    return {"version": TREE_VERSION, "nodes": [], "has_forks": False}


def _public_node(node: dict) -> dict:
    return {
        "id": node.get("id") or "",
        "chapter_id": node.get("chapter_id"),
        "outline_index": node.get("outline_index"),
        "num": node.get("num"),
        "title": node.get("title") or "",
        "heading": node.get("heading") or "",
        "branch": node.get("branch"),
        "parent_id": node.get("parent_id"),
        "children": list(node.get("children") or []),
    }


def extract_branch_meta(heading: str, section_text: str) -> tuple[str | None, str | None, str]:
    """Return (branch_label, explicit_parent_file_id, title)."""
    heading = (heading or "").strip()
    match = CHAPTER_HEADING_RE.match(heading)
    rest = ""
    if match:
        rest = heading[match.end() :].strip()
        # match is on the full heading line; group covers up through 章 and the rest of the line
        # CHAPTER_HEADING_RE is anchored to the whole line, so rest may be empty.
        # Use a looser split after 章 instead.
    after = re.sub(r"^#{2,4}\s*第\s*(?:[0-9]+|[一二三四五六七八九十百零〇○两]+)\s*章\s*", "", heading)
    rest = after.strip()
    branch = None
    label_match = BRANCH_LABEL_RE.match(rest)
    if label_match:
        branch = label_match.group("label").strip()
        rest = rest[label_match.end() :].strip()
    title = rest
    explicit_parent = None
    meta = BRANCH_META_RE.search(section_text or "")
    if meta:
        if meta.group("label") and not branch:
            branch = meta.group("label").strip()
        elif meta.group("label"):
            # comment label wins when present (UI-created branches)
            commented = meta.group("label").strip()
            if commented:
                branch = commented
        if meta.group("parent"):
            try:
                explicit_parent = validate_chapter_id(meta.group("parent"))
            except BookError:
                explicit_parent = None
    return (branch or None, explicit_parent, title)


def parse_outline_nodes(outline_text: str) -> list[dict]:
    _, chapters = split_outline_chapters(outline_text)
    nodes: list[dict] = []
    for index, chapter in enumerate(chapters):
        heading = chapter.get("heading") or ""
        text = chapter.get("text") or ""
        branch, explicit_parent, title = extract_branch_meta(heading, text)
        nodes.append(
            {
                "outline_index": index,
                "num": chapter.get("num"),
                "heading": heading,
                "title": title,
                "branch": branch,
                "text": text,
                "explicit_parent_id": explicit_parent,
                "parent_index": None,
            }
        )
    _assign_parents(nodes)
    return nodes


def _assign_parents(nodes: list[dict]) -> None:
    last_by_label: dict[str, dict] = {}
    for i, node in enumerate(nodes):
        if i == 0:
            node["parent_index"] = None
            if node.get("branch"):
                last_by_label[node["branch"]] = node
            continue
        prev = nodes[i - 1]
        is_new_branch = False
        if node.get("explicit_parent_id"):
            is_new_branch = True
        elif node.get("branch"):
            last_same = last_by_label.get(node["branch"])
            if last_same is None:
                is_new_branch = True
            elif (
                node.get("num") is not None
                and last_same.get("num") is not None
                and node["num"] <= last_same["num"]
            ):
                is_new_branch = True
        elif (
            prev.get("num") is not None
            and node.get("num") is not None
            and node["num"] <= prev["num"]
        ):
            is_new_branch = True

        if is_new_branch:
            node["parent_index"] = _fork_parent_index(nodes, i, node)
        else:
            node["parent_index"] = i - 1

        if node.get("branch"):
            last_by_label[node["branch"]] = node


def _fork_parent_index(nodes: list[dict], index: int, node: dict) -> int | None:
    if node.get("explicit_parent_id"):
        wanted = node["explicit_parent_id"]
        for j in range(index - 1, -1, -1):
            if nodes[j].get("chapter_id") == wanted:
                return j
        # chapter ids may not be assigned yet; remember and resolve later
    target = (node["num"] - 1) if node.get("num") is not None else None
    if target is not None:
        for j in range(index - 1, -1, -1):
            if nodes[j].get("num") == target:
                return j
        for j in range(index - 1, -1, -1):
            if nodes[j].get("num") is not None and nodes[j]["num"] < node["num"]:
                return j
    return index - 1 if index else None


def _normalize_title(title: str) -> str:
    text = (title or "").strip()
    text = re.sub(r"\s+", "", text)
    text = text.replace("待拟标题", "")
    return text


def _fingerprint(num, branch, title) -> str:
    return f"{num}|{branch or ''}|{_normalize_title(title or '')}"


def _titles_match(file_title: str, node: dict) -> bool:
    raw = (file_title or "").strip()
    raw = re.sub(r"^#+\s*", "", raw)
    file_branch, _, file_rest = extract_branch_meta("### " + raw if not raw.startswith("第") else "### " + raw, "")
    # first_heading already strips hashes; it looks like "第N章 ..."
    heading = raw if raw.startswith("第") else f"第{node.get('num') or ''}章 {raw}"
    file_branch, _, file_title = extract_branch_meta("### " + heading, "")
    node_title = _normalize_title(node.get("title") or "")
    file_norm = _normalize_title(file_title or raw)
    if node.get("num") is not None:
        token = re.search(r"第\s*([0-9]+|[一二三四五六七八九十百零〇○两]+)\s*章", raw)
        if token:
            parsed = parse_chapter_token(token.group(1))
            if parsed != node["num"]:
                return False
    if node.get("branch") and file_branch and node["branch"] != file_branch:
        return False
    if node.get("branch") and node["branch"] in raw and (not node_title or node_title in file_norm or file_norm in node_title):
        return True
    if node_title and file_norm and (node_title == file_norm or node_title in file_norm or file_norm in node_title):
        return True
    return False


def _format_chapter_id(num: int) -> str:
    raw = str(int(num))
    return raw.zfill(2) if len(raw) <= 2 else raw


def _next_chapter_id(name: str, reserved: set[str] | None = None) -> str:
    used = {int(ch["id"]) for ch in list_chapters(name)}
    path = ensure_book(name) / "chapters"
    if path.exists():
        for file in path.glob("*.md"):
            if re.fullmatch(r"\d+", file.stem):
                used.add(int(file.stem))
        for file in path.glob("*.prev.md"):
            stem = file.name[: -len(".prev.md")]
            if re.fullmatch(r"\d+", stem):
                used.add(int(stem))
        for file in path.glob("*.saved.md"):
            stem = file.name[: -len(".saved.md")]
            if re.fullmatch(r"\d+", stem):
                used.add(int(stem))
    if reserved:
        for item in reserved:
            if re.fullmatch(r"\d+", str(item).strip()):
                used.add(int(item))
    next_num = (max(used) + 1) if used else 1
    return _format_chapter_id(next_num)


def _known_ids_from_tree(tree: dict) -> set[str]:
    ids: set[str] = set()
    for node in tree.get("nodes") or []:
        cid = node.get("chapter_id")
        if cid:
            ids.add(str(cid))
    return ids


def _assign_chapter_ids(name: str, nodes: list[dict], previous: dict) -> None:
    files = {ch["id"]: ch for ch in list_chapters(name)}
    prev_nodes = previous.get("nodes") or []
    used: set[str] = set()

    prev_by_fp: dict[str, dict] = {}
    prev_by_loose: dict[tuple, list[dict]] = {}
    for prev in prev_nodes:
        fp = _fingerprint(prev.get("num"), prev.get("branch"), prev.get("title"))
        prev_by_fp[fp] = prev
        key = (prev.get("num"), prev.get("branch") or "")
        prev_by_loose.setdefault(key, []).append(prev)

    def take(cid: str | None) -> bool:
        if not cid or cid in used:
            return False
        used.add(cid)
        return True

    for node in nodes:
        fp = _fingerprint(node.get("num"), node.get("branch"), node.get("title"))
        prev = prev_by_fp.get(fp)
        if not prev:
            loose = prev_by_loose.get((node.get("num"), node.get("branch") or "")) or []
            if len(loose) == 1:
                prev = loose[0]
        if prev and take(prev.get("chapter_id")):
            node["chapter_id"] = prev["chapter_id"]

    # Unique outline numbers map to the same file id first.
    # A chapter file may open with the wrong heading (e.g. 01.md starting
    # with「第五章」); the file id is the source of truth when the number is unique.
    from collections import Counter

    counts = Counter(node.get("num") for node in nodes if node.get("num") is not None)
    for node in nodes:
        if node.get("chapter_id") or node.get("num") is None:
            continue
        if counts[node["num"]] != 1:
            continue
        fid = _format_chapter_id(node["num"])
        if fid in files and take(fid):
            node["chapter_id"] = fid

    for node in nodes:
        if node.get("chapter_id"):
            continue
        for fid, chapter in files.items():
            if fid in used:
                continue
            if _titles_match(chapter.get("title") or "", node) and take(fid):
                node["chapter_id"] = fid
                break

    leftover_files = sorted((fid for fid in files if fid not in used), key=lambda x: int(x))
    leftover_nodes = [node for node in nodes if not node.get("chapter_id")]
    for node, fid in zip(leftover_nodes, leftover_files):
        if take(fid):
            node["chapter_id"] = fid


def _resolve_explicit_parents(nodes: list[dict]) -> None:
    by_chapter = {node.get("chapter_id"): i for i, node in enumerate(nodes) if node.get("chapter_id")}
    for i, node in enumerate(nodes):
        explicit = node.get("explicit_parent_id")
        if not explicit:
            continue
        parent_i = by_chapter.get(explicit)
        if parent_i is not None and parent_i != i:
            node["parent_index"] = parent_i


def _node_id(node: dict) -> str:
    if node.get("chapter_id"):
        return str(node["chapter_id"])
    return f"o{node.get('outline_index', 0)}"


def _assemble_tree(nodes: list[dict]) -> dict:
    assembled: list[dict] = []
    index_to_id: dict[int, str] = {}
    for node in nodes:
        nid = _node_id(node)
        index_to_id[node["outline_index"]] = nid
        assembled.append(
            {
                "id": nid,
                "chapter_id": node.get("chapter_id"),
                "outline_index": node.get("outline_index"),
                "num": node.get("num"),
                "title": node.get("title") or "",
                "heading": node.get("heading") or "",
                "branch": node.get("branch"),
                "parent_id": None,
                "children": [],
            }
        )
    for i, node in enumerate(nodes):
        parent_index = node.get("parent_index")
        if parent_index is None:
            continue
        parent_id = index_to_id.get(nodes[parent_index]["outline_index"])
        assembled[i]["parent_id"] = parent_id
    for item in assembled:
        if not item["parent_id"]:
            continue
        for parent in assembled:
            if parent["id"] == item["parent_id"]:
                parent["children"].append(item["id"])
                break
    has_forks = any(len(item["children"]) > 1 for item in assembled)
    if not has_forks:
        # multiple roots or a node with a branch label also counts
        labels = {item.get("branch") for item in assembled if item.get("branch")}
        has_forks = len(labels) >= 2 or any(item.get("branch") for item in assembled if item.get("parent_id"))
    return {"version": TREE_VERSION, "nodes": assembled, "has_forks": bool(has_forks)}


def _chapter_has_backup(name: str, chapter_id: str) -> bool:
    try:
        cid = validate_chapter_id(chapter_id)
    except BookError:
        return False
    if has_previous_chapter(name, cid):
        return True
    latest = latest_saved_chapter_path(name, cid)
    if latest.exists() and latest.read_text(encoding="utf-8-sig").strip():
        return True
    archive_dir = saved_chapter_dir(name)
    if archive_dir.exists() and any(archive_dir.glob(f"{cid}-*.md")):
        return True
    return False


def decorate_tree(name: str, tree: dict) -> dict:
    files = {ch["id"]: ch for ch in list_chapters(name)}
    nodes = []
    for node in tree.get("nodes") or []:
        item = dict(node)
        cid = item.get("chapter_id")
        file_info = files.get(cid) if cid else None
        item["has_file"] = bool(file_info) or (bool(cid) and chapter_path(name, cid).exists())
        item["empty"] = (not item["has_file"]) or bool(file_info and file_info.get("empty"))
        item["hanzi"] = file_info["hanzi"] if file_info else 0
        item["file_title"] = file_info["title"] if file_info else ""
        item["has_backup"] = bool(cid) and _chapter_has_backup(name, cid)
        item["display_title"] = _display_title(item)
        item["suggested_branch"] = ""
        nodes.append(item)
    decorated = tree_with_nodes(tree, nodes)
    by_id = {item["id"]: item for item in nodes}
    for item in nodes:
        plan = new_ending_plan(decorated, item["id"])
        item["ending_plan"] = plan
        item["suggested_branch"] = plan.get("suggested_branch") or suggest_branch_name(decorated, item["id"])
        # keep children as ids
        item["children"] = [cid for cid in (item.get("children") or []) if cid in by_id]
    has_forks = bool(tree.get("has_forks")) or any(len(item["children"]) > 1 for item in nodes)
    return {"version": TREE_VERSION, "nodes": nodes, "has_forks": has_forks}


def tree_with_nodes(tree: dict, nodes: list[dict]) -> dict:
    return {"version": tree.get("version", TREE_VERSION), "nodes": nodes, "has_forks": tree.get("has_forks")}


def _display_title(node: dict) -> str:
    num = node.get("num")
    branch = node.get("branch")
    title = (node.get("title") or "").strip()
    file_title = (node.get("file_title") or "").strip()
    if file_title and not title:
        return file_title
    prefix = f"第{chinese_numeral(num)}章" if num is not None else "未编号"
    bits = [prefix]
    if branch:
        bits.append(branch)
    if title:
        bits.append(title)
    return " ".join(bits)


def suggest_branch_name(tree: dict, node_id: str) -> str:
    nodes = {n["id"]: n for n in tree.get("nodes") or []}
    node = nodes.get(node_id)
    if not node:
        return "二结局"
    labels: list[str] = []
    # labels already used by this node's child branches (the child's branch or first labeled descendant)
    for child_id in node.get("children") or []:
        child = nodes.get(child_id)
        if not child:
            continue
        label = child.get("branch")
        if not label:
            # original unlabeled continuation counts as a taken "path", but not a 结局 name
            continue
        labels.append(label)
    # also collect sibling labels at this fork if we are adding another ending from the same node
    taken = set(labels)
    for i, ch in enumerate("一二三四五六七八九十", 1):
        candidate = f"{ch}结局"
        if candidate not in taken:
            return candidate
    return f"{len(taken) + 1}结局"


def first_node_of_ending(tree: dict, node: dict | None) -> dict | None:
    """The first chapter of this node's ending branch, or the node itself on the trunk."""
    if not node:
        return node
    by_id = {n["id"]: n for n in tree.get("nodes") or []}
    branch = (node.get("branch") or "").strip()
    if not branch:
        return node
    cur = node
    seen: set[str] = set()
    while cur and cur.get("parent_id") and cur["id"] not in seen:
        seen.add(cur["id"])
        parent = by_id.get(cur["parent_id"])
        if not parent:
            break
        if (parent.get("branch") or "").strip() != branch:
            return cur
        cur = parent
    return cur


def new_ending_plan(tree: dict, node_id: str | None) -> dict:
    """Where a new ending should fork, and at which chapter number it should start.

    Clicking 一结局 / 二结局 adds a sibling ending at the same fork, starting at the
    same chapter number (e.g. 第九章), not after the last file number.
    """
    node = find_node(tree, node_id)
    if not node:
        return {}
    by_id = {n["id"]: n for n in tree.get("nodes") or []}
    first = first_node_of_ending(tree, node) or node
    branch = (node.get("branch") or "").strip()
    if branch and first.get("parent_id") and by_id.get(first["parent_id"]):
        fork = by_id[first["parent_id"]]
        start_num = first.get("num") or ((fork.get("num") or 0) + 1)
    else:
        fork = node
        start_num = (node.get("num") or 0) + 1
    labels: list[str] = []
    for child_id in fork.get("children") or []:
        child = by_id.get(child_id)
        if child and (child.get("branch") or "").strip():
            labels.append((child.get("branch") or "").strip())
    suggested = ""
    taken = set(labels)
    for ch in "一二三四五六七八九十":
        candidate = f"{ch}结局"
        if candidate not in taken:
            suggested = candidate
            break
    if not suggested:
        suggested = f"{len(taken) + 1}结局"
    start_num = int(start_num or 1)
    return {
        "from_id": fork.get("id"),
        "from_chapter": fork.get("chapter_id") or fork.get("id"),
        "from_title": _display_title(fork),
        "start_num": start_num,
        "start_label": f"第{chinese_numeral(start_num)}章",
        "suggested_branch": suggested,
        "sibling_branches": labels,
    }


def sync_book_tree(name: str) -> dict:
    ensure_book(name)
    previous = load_tree(name)
    outline = read_outline(name)
    nodes = parse_outline_nodes(outline)
    _assign_chapter_ids(name, nodes, previous)
    _resolve_explicit_parents(nodes)
    # re-resolve explicit parents now that ids exist; also fix fork parent via explicit id
    _assign_parents(nodes)
    _resolve_explicit_parents(nodes)
    tree = _assemble_tree(nodes)
    return save_tree(name, tree)


def get_book_tree(name: str) -> dict:
    path = tree_path(name)
    if not path.exists():
        return sync_book_tree(name)
    # Always re-sync from outline so heading edits and new files are picked up,
    # while existing chapter_id attachments are preserved.
    return sync_book_tree(name)


def find_node(tree: dict, node_id: str | None) -> dict | None:
    if not node_id:
        return None
    wanted = str(node_id)
    try:
        if re.fullmatch(r"\d+", wanted):
            wanted_cid = validate_chapter_id(wanted)
        else:
            wanted_cid = None
    except BookError:
        wanted_cid = None
    outline_key = int(wanted[1:]) if re.fullmatch(r"o\d+", wanted) else None
    for node in tree.get("nodes") or []:
        if node.get("id") == wanted or node.get("id") == node_id:
            return node
        if wanted_cid and node.get("chapter_id") == wanted_cid:
            return node
        if outline_key is not None and node.get("outline_index") == outline_key:
            return node
    return None


def ancestor_chapter_ids(name: str, chapter_id: str) -> list[str] | None:
    """Nearest-first ancestor file ids. None means 'use numeric fallback'."""
    try:
        cid = validate_chapter_id(chapter_id)
    except BookError:
        return None
    tree = load_tree(name)
    if not (tree.get("nodes") or []):
        # try a sync if outline exists; still allow fallback if the chapter is unknown
        if read_outline(name).strip():
            tree = sync_book_tree(name)
        else:
            return None
    node = find_node(tree, cid)
    if not node:
        return None
    by_id = {item["id"]: item for item in tree["nodes"]}
    seen: set[str] = set()
    ancestors: list[str] = []
    current = node
    while current and current.get("parent_id"):
        parent = by_id.get(current["parent_id"])
        if not parent or parent["id"] in seen:
            break
        seen.add(parent["id"])
        parent_cid = parent.get("chapter_id")
        if parent_cid:
            ancestors.append(str(parent_cid))
        current = parent
    return ancestors


def path_chapter_ids(name: str, chapter_id: str) -> list[str] | None:
    """Oldest-first ids from trunk to the current chapter, including itself if mapped."""
    try:
        cid = validate_chapter_id(chapter_id)
    except BookError:
        return None
    ancestors = ancestor_chapter_ids(name, cid)
    if ancestors is None:
        return None
    return list(reversed(ancestors)) + [cid]


def outline_section_for_chapter(name: str, chapter_id: str, outline_text: str | None = None) -> str:
    try:
        cid = validate_chapter_id(chapter_id)
    except BookError:
        return ""
    outline = outline_text if outline_text is not None else read_outline(name)
    nodes = parse_outline_nodes(outline)
    tree = load_tree(name)
    if not tree.get("nodes"):
        tree = sync_book_tree(name)
    node = find_node(tree, cid)
    if not node:
        return ""
    index = node.get("outline_index")
    if index is None or index < 0 or index >= len(nodes):
        # try match by chapter_id after a fresh parse+assign
        _assign_chapter_ids(name, nodes, tree)
        _resolve_explicit_parents(nodes)
        for parsed in nodes:
            if parsed.get("chapter_id") == cid:
                return parsed.get("text") or ""
        return ""
    return nodes[index].get("text") or ""


def path_outline_chapters(name: str, chapter_id: str, chapters: list[dict]) -> list[dict] | None:
    """Filter split_outline_chapters() results to the current path.

    Includes ancestors, the current node, and the unique continuation after it
    (a single-child chain). Sibling endings are omitted so their plot does not
    leak into the prompt. A fork node's other branches are not included.
    """
    try:
        cid = validate_chapter_id(chapter_id)
    except BookError:
        return None
    tree = load_tree(name)
    if not tree.get("nodes"):
        if not read_outline(name).strip():
            return None
        tree = sync_book_tree(name)
    node = find_node(tree, cid)
    if not node:
        return None
    by_id = {item["id"]: item for item in tree["nodes"]}
    on_path: set[str] = set()
    current = node
    seen: set[str] = set()
    while current and current["id"] not in seen:
        on_path.add(current["id"])
        seen.add(current["id"])
        if not current.get("parent_id"):
            break
        current = by_id.get(current["parent_id"])
    # Walk unique descendants (single child only) so the rest of this ending remains visible.
    current = node
    while current:
        children = [child_id for child_id in (current.get("children") or []) if child_id in by_id]
        if len(children) != 1:
            break
        child = by_id[children[0]]
        if child["id"] in on_path:
            break
        on_path.add(child["id"])
        current = child
    index_on_path = {
        item.get("outline_index")
        for item in tree["nodes"]
        if item["id"] in on_path and item.get("outline_index") is not None
    }
    filtered: list[dict] = []
    for index, chapter in enumerate(chapters):
        if index in index_on_path:
            filtered.append(chapter)
    return filtered


def display_num_for_chapter(name: str, chapter_id: str, default: int) -> int:
    tree = load_tree(name)
    node = find_node(tree, chapter_id)
    if node and node.get("num") is not None:
        return int(node["num"])
    return default


def add_ending_branch(
    name: str,
    from_chapter: str,
    branch_name: str,
    idea: str = "",
    chapter_count: int = 1,
) -> dict:
    ensure_book(name)
    fork_id = from_chapter.strip()
    if not fork_id:
        raise BookError("请先选择要从哪里分出结局")
    label = (branch_name or "").strip()
    label = re.sub(r"[\r\n]+", " ", label)
    if not label:
        raise BookError("请填写分支名称，例如「三结局」")
    if len(label) > 40:
        raise BookError("分支名称过长")
    try:
        count = int(chapter_count or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 8))

    tree = sync_book_tree(name)
    clicked = find_node(tree, fork_id)
    if not clicked:
        raise BookError("找不到要分叉的节点")
    plan = new_ending_plan(tree, clicked.get("id") or fork_id)
    fork = find_node(tree, plan.get("from_id") or plan.get("from_chapter")) or clicked
    parent_cid = fork.get("chapter_id")
    if not parent_cid:
        parent_cid = _next_chapter_id(name)
        write_chapter(name, parent_cid, "")
        # attach and persist so the comment can point at a real id
        fork["chapter_id"] = parent_cid
        fork["id"] = parent_cid
        save_tree(name, tree)
        tree = sync_book_tree(name)
        fork = find_node(tree, parent_cid) or fork
        parent_cid = fork.get("chapter_id") or parent_cid

    start_num = int(plan.get("start_num") or ((fork.get("num") or 0) + 1))
    reserved: set[str] = set(_known_ids_from_tree(tree))
    new_ids: list[str] = []
    for _ in range(count):
        cid = _next_chapter_id(name, reserved)
        reserved.add(cid)
        new_ids.append(cid)
        write_chapter(name, cid, "")

    outline = read_outline(name)
    backup_outline(name)
    blocks = []
    idea_text = (idea or "").strip()
    for offset, cid in enumerate(new_ids):
        num = start_num + offset
        heading_title = "待拟标题"
        heading = f"### 第{chinese_numeral(num)}章 {label} {heading_title}"
        parts = [heading, "", f"<!-- 分支：{label}；分叉于：{parent_cid if offset == 0 else new_ids[offset - 1]} -->", ""]
        if offset == 0 and idea_text:
            parts.append(f"走向：{idea_text}")
            parts.append("")
        parts.append("（待补充）")
        parts.append("")
        blocks.append("\n".join(parts))
    addition = "\n\n" + "\n".join(blocks)
    if outline and not outline.endswith("\n"):
        outline += "\n"
    write_outline(name, (outline or "") + addition)
    tree = sync_book_tree(name)
    return {
        "ok": True,
        "branch": label,
        "from_chapter": parent_cid,
        "chapter_ids": new_ids,
        "outline": read_outline(name),
        "chapters": list_chapters(name),
        "tree": decorate_tree(name, load_tree(name)) if tree else tree,
        "tree_data": tree,
    }


def open_tree_node(name: str, node_id: str) -> dict:
    tree = sync_book_tree(name)
    node = find_node(tree, node_id)
    if not node:
        raise BookError("找不到该节点")
    cid = node.get("chapter_id")
    if not cid:
        cid = _next_chapter_id(name, _known_ids_from_tree(tree))
        write_chapter(name, cid, "")
        # rewrite tree.json attachment: store as previous mapping by outline index
        # After creating the file, re-sync will assign leftover file to this node if it is
        # the only leftover; to be deterministic, patch previous tree first.
        raw = load_tree(name)
        for item in raw.get("nodes") or []:
            if item.get("id") == node["id"] or (
                item.get("outline_index") == node.get("outline_index") and not item.get("chapter_id")
            ):
                item["chapter_id"] = cid
                item["id"] = cid
                if item.get("parent_id") == node["id"]:
                    pass
                break
        # also update children parent refs later via sync
        save_tree(name, raw)
        tree = sync_book_tree(name)
        node = find_node(tree, cid) or node
    path = chapter_path(name, cid)
    if not path.exists():
        write_chapter(name, cid, "")
    from app.books import read_chapter

    data = read_chapter(name, cid)
    data["ok"] = True
    data["tree"] = get_book_tree(name)
    data["node"] = find_node(data["tree"], cid)
    return data
