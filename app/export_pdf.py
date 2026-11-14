"""Export a book as a single A4 Chinese-font PDF, including every ending.

This module is a standalone export path. Callers must not route it through
JobManager.start(), so an in-flight writing or illustration job is not cancelled.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

from app.books import BookError, book_dir, ensure_book, list_chapters, load_settings, read_chapter, validate_chapter_id
from app.illustrations import image_path, list_illustrations
from app.textutil import ILLUST_MARKER_RE, find_illustration_ids, first_heading, strip_illustration_markers

logger = logging.getLogger("novel_writer")

HONG_KONG = timezone(timedelta(hours=8))

FONT_NAME = "BookBody"
FONT_NAME_BOLD = "BookBodyBold"

# Subtle ink color for clickable TOC titles (not web blue).
_TOC_LINK_RGB = (0.22, 0.16, 0.12)

# Prefer a CJK font. Windows 微软雅黑 / 宋体 first, then common macOS and
# Linux CJK fonts. Do not silently fall back to a Latin-only font, or the
# PDF would show boxes / missing glyphs.
_FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\msyh.ttf"),
    Path(r"C:\Windows\Fonts\simsun.ttc"),
    Path(r"C:\Windows\Fonts\simsun.ttf"),
    Path("/System/Library/Fonts/STHeiti Light.ttc"),
    Path("/System/Library/Fonts/PingFang.ttc"),
    Path("/Library/Fonts/Songti.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
)
_BOLD_CANDIDATES = (
    Path(r"C:\Windows\Fonts\msyhbd.ttc"),
    Path(r"C:\Windows\Fonts\msyhbd.ttf"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path("/System/Library/Fonts/STHeiti Medium.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc"),
)

_INVALID_FILE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DEST_SAFE = re.compile(r"[^A-Za-z0-9._-]+")

_FONT_READY = False
_RESOLVED_FONT: Path | None = None


class ExportPdfError(BookError):
    pass


def book_display_title(name: str) -> str:
    """Prefer a title field when one exists; otherwise use the book folder name."""
    title = ""
    try:
        settings = load_settings(name) or {}
        for key in ("title", "book_title", "display_title"):
            value = settings.get(key)
            if isinstance(value, str) and value.strip():
                title = value.strip()
                break
    except BookError:
        title = ""
    return title or (name or "").strip() or "未命名作品"


def current_line_chapter_ids(name: str, chapter_id: str | None = None) -> list[str]:
    """Chapter file ids along the current plot-tree line, oldest first.

    Reuses the same walks already used by the UI / tree helpers:
    - ancestors + current, as in path_chapter_ids
    - then the unique single-child continuation, as in path_outline_chapters
      (so 「全书」includes the rest of this ending, not only chapters so far)

    If no chapter is selected, walk the main spine from the root, following a
    unique child, or the single unlabeled (non-ending) child at a fork.
    If there is no tree, fall back to all chapter files in list order.

    This helper stays path-only. PDF export uses walk_export_tree() instead,
    so sibling endings are not omitted from the book file.
    """
    from app.tree import find_node, load_tree

    fallback = [item["id"] for item in list_chapters(name)]
    tree = load_tree(name)
    nodes = tree.get("nodes") or []
    if not nodes:
        return fallback

    by_id = {node["id"]: node for node in nodes}

    def chapter_of(node: dict) -> str | None:
        raw = (node.get("chapter_id") or "").strip()
        if not raw:
            return None
        try:
            return validate_chapter_id(raw)
        except BookError:
            return None

    def ancestors_and_self(node: dict) -> list[dict]:
        ordered: list[dict] = []
        seen: set[str] = set()
        current = node
        while current and current.get("id") not in seen:
            seen.add(current["id"])
            ordered.append(current)
            parent_id = current.get("parent_id")
            current = by_id.get(parent_id) if parent_id else None
        ordered.reverse()
        return ordered

    def unique_continuation(node: dict, seen_ids: set[str]) -> list[dict]:
        extra: list[dict] = []
        current = node
        seen = set(seen_ids)
        while current:
            child_ids = [cid for cid in (current.get("children") or []) if cid in by_id]
            if len(child_ids) != 1:
                break
            child = by_id[child_ids[0]]
            if child["id"] in seen:
                break
            extra.append(child)
            seen.add(child["id"])
            current = child
        return extra

    def ids_of(line_nodes: list[dict]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for node in line_nodes:
            cid = chapter_of(node)
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
        return out

    start = None
    if chapter_id and str(chapter_id).strip():
        start = find_node(tree, str(chapter_id).strip())
        if start:
            line = ancestors_and_self(start)
            seen = {node["id"] for node in line}
            line.extend(unique_continuation(start, seen))
            ids = ids_of(line)
            if ids:
                return ids

    roots = [node for node in nodes if not node.get("parent_id") or node.get("parent_id") not in by_id]
    if not roots:
        return fallback
    roots.sort(key=lambda node: (node.get("num") is None, node.get("num") or 0, str(node.get("id") or "")))
    start = roots[0]
    line = ancestors_and_self(start)
    seen = {node["id"] for node in line}
    current = start
    while current:
        children = [by_id[cid] for cid in (current.get("children") or []) if cid in by_id]
        nxt = None
        if len(children) == 1:
            nxt = children[0]
        elif len(children) > 1:
            unlabeled = [child for child in children if not (child.get("branch") or "").strip()]
            if len(unlabeled) == 1:
                nxt = unlabeled[0]
        if not nxt or nxt["id"] in seen:
            break
        line.append(nxt)
        seen.add(nxt["id"])
        current = nxt
    ids = ids_of(line)
    return ids or fallback


def _chinese_numeral(num: int) -> str:
    """Prefer app.tree.chinese_numeral; keep a local fallback for isolated tests."""
    try:
        from app.tree import chinese_numeral

        return chinese_numeral(num)
    except Exception:
        n = int(num)
        ones = "零一二三四五六七八九"
        if n < 0:
            return str(n)
        if n < 10:
            return ones[n]
        if n == 10:
            return "十"
        if n < 20:
            return "十" + ones[n - 10]
        if n < 100:
            tens, rest = divmod(n, 10)
            return ones[tens] + "十" + (ones[rest] if rest else "")
        return str(n)


def _chapter_id_of(node: dict) -> str | None:
    raw = (node.get("chapter_id") or "").strip()
    if not raw:
        return None
    try:
        return validate_chapter_id(raw)
    except BookError:
        return None


def _unique_dest(prefix: str, raw: str, used: set[str]) -> str:
    cleaned = _DEST_SAFE.sub("-", str(raw or "")).strip("-._") or "x"
    base = f"{prefix}-{cleaned}"
    dest = base
    suffix = 2
    while dest in used:
        dest = f"{base}-{suffix}"
        suffix += 1
    used.add(dest)
    return dest


def _ordered_children(node: dict, by_id: dict[str, dict], nodes: list[dict]) -> list[dict]:
    """Preserve the node's children list; append any missing parent-linked nodes."""
    seen: set[str] = set()
    out: list[dict] = []
    for child_id in node.get("children") or []:
        child = by_id.get(child_id)
        if not child or child["id"] in seen:
            continue
        seen.add(child["id"])
        out.append(child)
    extras: list[dict] = []
    for other in nodes:
        if other.get("parent_id") == node.get("id") and other["id"] not in seen:
            extras.append(other)
            seen.add(other["id"])
    extras.sort(key=lambda item: (item.get("num") is None, item.get("num") or 0, str(item.get("id") or "")))
    return out + extras


def _root_nodes(nodes: list[dict], by_id: dict[str, dict]) -> list[dict]:
    roots = [node for node in nodes if not node.get("parent_id") or node.get("parent_id") not in by_id]
    roots.sort(key=lambda node: (node.get("num") is None, node.get("num") or 0, str(node.get("id") or "")))
    return roots


def fork_branch_headings(children: list[dict]) -> list[str]:
    """Visible section titles for every child of a fork, never dropping unlabeled ones."""
    labels = [(child.get("branch") or "").strip() for child in children]
    unlabeled = [index for index, label in enumerate(labels) if not label]
    headings: list[str] = []
    unlabeled_seq = 0
    for label in labels:
        if label:
            headings.append(label)
        elif len(unlabeled) == 1:
            # One unlabeled sibling among labeled ones is the continuing trunk.
            headings.append("主线")
        else:
            unlabeled_seq += 1
            headings.append(f"结局{_chinese_numeral(unlabeled_seq)}")
    return headings


def walk_export_tree(tree: dict) -> list[dict]:
    """Walk the whole plot tree: shared trunk once, then every ending subtree.

    Returns a list of dicts:
      kind='chapter': chapter_id, dest, toc_level, node_id
      kind='heading': title, dest, toc_level, node_id

    Does not change path_chapter_ids / path_outline_chapters. Dest names are
    ASCII (ch-01, br-...) and unique even when two endings share a display number.
    An empty node list yields an empty result; the caller falls back to files.
    """
    nodes = [node for node in (tree.get("nodes") or []) if isinstance(node, dict) and node.get("id")]
    if not nodes:
        return []

    by_id = {node["id"]: node for node in nodes}
    used_dests: set[str] = set()
    items: list[dict] = []
    seen: set[str] = set()

    def emit_chapter(node: dict, toc_level: int) -> None:
        cid = _chapter_id_of(node)
        if not cid:
            return
        items.append(
            {
                "kind": "chapter",
                "chapter_id": cid,
                "node_id": node["id"],
                "dest": _unique_dest("ch", cid, used_dests),
                "toc_level": toc_level,
            }
        )

    def walk(node: dict, toc_level: int) -> None:
        current = node
        while current and current["id"] not in seen:
            seen.add(current["id"])
            emit_chapter(current, toc_level)
            children = [child for child in _ordered_children(current, by_id, nodes) if child["id"] not in seen]
            if not children:
                break
            if len(children) == 1:
                current = children[0]
                continue
            headings = fork_branch_headings(children)
            for child, heading in zip(children, headings):
                dest = _unique_dest("br", child.get("id") or heading, used_dests)
                items.append(
                    {
                        "kind": "heading",
                        "title": heading,
                        "node_id": child["id"],
                        "dest": dest,
                        "toc_level": toc_level,
                    }
                )
                walk(child, toc_level + 1)
            break

    for root in _root_nodes(nodes, by_id):
        if root["id"] not in seen:
            walk(root, 0)
    return items


def linear_export_items(chapter_ids: list[str]) -> list[dict]:
    """File-order chapters with no synthetic ending headings."""
    used: set[str] = set()
    items: list[dict] = []
    for cid in chapter_ids:
        try:
            validated = validate_chapter_id(cid)
        except BookError:
            continue
        items.append(
            {
                "kind": "chapter",
                "chapter_id": validated,
                "node_id": "",
                "dest": _unique_dest("ch", validated, used),
                "toc_level": 0,
            }
        )
    return items


def list_export_sections(name: str, chapter_id: str | None = None) -> list[dict]:
    """Sections for a full-book PDF. The optional chapter_id is ignored.

    Full tree + all endings is the default. path_chapter_ids remains path-only
    for writing prompts. With no tree, this is list_chapters() file order.
    """
    from app.tree import load_tree

    fallback = [item["id"] for item in list_chapters(name)]
    tree = load_tree(name)
    nodes = tree.get("nodes") or []
    if not nodes:
        return linear_export_items(fallback)
    items = walk_export_tree(tree)
    return items or linear_export_items(fallback)


def _prune_empty_headings(items: list[dict]) -> list[dict]:
    keep = [True] * len(items)
    for index, item in enumerate(items):
        if item.get("kind") != "heading":
            continue
        level = int(item.get("toc_level") or 0)
        has_content = False
        for later in items[index + 1 :]:
            if later.get("kind") == "heading" and int(later.get("toc_level") or 0) <= level:
                break
            if later.get("kind") == "chapter":
                has_content = True
                break
        if not has_content:
            keep[index] = False
    return [item for item, flag in zip(items, keep) if flag]


def resolve_chinese_font(explicit: str | Path | None = None) -> Path:
    """Return a usable Windows Chinese font path, or raise a clear Chinese error."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(_FONT_CANDIDATES)
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size > 0:
                return path
        except OSError:
            continue
    raise ExportPdfError(
        "未找到可用的中文字体。请确认本机已安装微软雅黑（msyh.ttc / msyh.ttf）或宋体（simsun.ttc / simsun.ttf）。"
    )


def register_fonts(explicit: str | Path | None = None) -> Path:
    global _FONT_READY, _RESOLVED_FONT
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_path = resolve_chinese_font(explicit)
    if _FONT_READY and _RESOLVED_FONT == font_path and FONT_NAME in pdfmetrics.getRegisteredFontNames():
        return font_path

    def _register(name: str, path: Path, subfont_index: int = 0) -> None:
        suffix = path.suffix.lower()
        if suffix == ".ttc":
            pdfmetrics.registerFont(TTFont(name, str(path), subfontIndex=subfont_index))
        else:
            pdfmetrics.registerFont(TTFont(name, str(path)))

    try:
        _register(FONT_NAME, font_path, 0)
    except Exception as exc:
        raise ExportPdfError(
            "未能加载中文字体。请确认本机已安装微软雅黑（msyh.ttc / msyh.ttf）或宋体（simsun.ttc / simsun.ttf）。"
        ) from exc

    bold_registered = False
    bold_candidates: list[Path] = []
    if font_path.name.lower().startswith("msyh") and font_path.suffix.lower() == ".ttc":
        bold_candidates.append(font_path)
    bold_candidates.extend(_BOLD_CANDIDATES)
    # Allow a bundled CJK TTC used in tests to also provide a bold face.
    if font_path.suffix.lower() == ".ttc" and font_path not in bold_candidates:
        bold_candidates.append(font_path)
    for bold_path in bold_candidates:
        try:
            if not bold_path.is_file():
                continue
            index = 1 if bold_path == font_path and bold_path.suffix.lower() == ".ttc" else 0
            _register(FONT_NAME_BOLD, bold_path, index)
            bold_registered = True
            break
        except Exception:
            continue
    if not bold_registered:
        # Titles can reuse the regular face when a bold file is missing.
        try:
            _register(FONT_NAME_BOLD, font_path, 0)
        except Exception as exc:
            raise ExportPdfError(
                "未能加载中文字体。请确认本机已安装微软雅黑（msyh.ttc / msyh.ttf）或宋体（simsun.ttc / simsun.ttf）。"
            ) from exc

    _FONT_READY = True
    _RESOLVED_FONT = font_path
    return font_path


def pdf_filename_for(title: str) -> str:
    text = (title or "").strip() or "全书"
    if text.startswith("《") and text.endswith("》") and len(text) >= 2:
        wrapped = text
    else:
        wrapped = f"《{text}》"
    wrapped = _INVALID_FILE.sub(" ", wrapped).strip() or "全书"
    return f"{wrapped}.pdf"


def export_book_pdf(
    name: str,
    chapter_id: str | None = None,
    *,
    font_path: str | Path | None = None,
    dest: Path | None = None,
) -> tuple[Path, str]:
    """Write the book PDF into the book folder and return (path, download_name).

    Exports the full plot tree, including every ending. chapter_id is accepted
    so the endpoint signature stays stable, but it no longer restricts the walk.
    Does not log chapter bodies. font_path is for tests; production leaves it unset.
    """
    try:
        from reportlab.lib.colors import Color
        from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfdoc
        from reportlab.platypus import (
            BaseDocTemplate,
            Flowable,
            Frame,
            Image,
            PageBreak,
            PageTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
        )

        class BookDocTemplate(BaseDocTemplate):
            """Writes ASCII named destinations (ch-01, br-...) into the catalog."""

            def _endBuild(self):
                canv = getattr(self, "canv", None)
                dests = getattr(canv, "_destinations", None) if canv else None
                if canv and dests:
                    catalog_dests = pdfdoc.PDFDictionary()
                    for dest_name, dest in dests.items():
                        catalog_dests[dest_name] = dest
                    canv._doc.Catalog.Dests = catalog_dests
                BaseDocTemplate._endBuild(self)
    except ImportError as exc:
        raise ExportPdfError("本机尚未安装 PDF 导出组件（reportlab）。请先安装后再试。") from exc

    ensure_book(name)
    register_fonts(font_path)

    title = book_display_title(name)
    sections = list_export_sections(name, chapter_id)
    illustrations = {item["id"]: item for item in list_illustrations(name)}

    built: list[dict] = []
    for section in sections:
        if section.get("kind") == "heading":
            built.append(dict(section))
            continue
        cid = section.get("chapter_id")
        if not cid:
            continue
        try:
            data = read_chapter(name, cid)
        except BookError:
            continue
        text_body = data.get("text") or ""
        marker_ids = find_illustration_ids(text_body)
        body_visible = strip_illustration_markers(text_body).strip()
        chapter_images = list_illustrations(name, cid)
        front_items = [
            item
            for item in chapter_images
            if item.get("id") and item["id"] not in marker_ids and _image_exists(name, item)
        ]
        has_inline = any(
            mid in illustrations and _image_exists(name, illustrations[mid]) for mid in marker_ids
        )
        if not body_visible and not front_items and not has_inline:
            continue
        payload = dict(section)
        payload.update(
            {
                "id": data["id"],
                "title": (data.get("title") or first_heading(text_body) or f"第{int(data['id'])}章"),
                "text": text_body,
                "marker_ids": marker_ids,
                "front_items": front_items,
            }
        )
        built.append(payload)

    built = _prune_empty_headings(built)
    if not any(item.get("kind") == "chapter" for item in built):
        raise ExportPdfError("当前书中的章节都还没有正文，无法导出。")

    download_name = pdf_filename_for(title)
    folder = book_dir(name)
    target = dest if dest is not None else (folder / download_name)
    link_color = Color(*_TOC_LINK_RGB)

    styles = {
        "cover": ParagraphStyle(
            name="CoverTitle",
            fontName=FONT_NAME_BOLD,
            fontSize=28,
            leading=40,
            alignment=TA_CENTER,
            wordWrap="CJK",
        ),
        "toc_head": ParagraphStyle(
            name="TocHead",
            fontName=FONT_NAME_BOLD,
            fontSize=18,
            leading=28,
            alignment=TA_CENTER,
            wordWrap="CJK",
            spaceAfter=16,
        ),
        "toc_item": ParagraphStyle(
            name="TocItem",
            fontName=FONT_NAME,
            fontSize=12,
            leading=22,
            alignment=TA_LEFT,
            wordWrap="CJK",
            textColor=link_color,
            underlineWidth=0.4,
            underlineOffset=-1.2,
            underlineColor=link_color,
        ),
        "toc_branch": ParagraphStyle(
            name="TocBranch",
            fontName=FONT_NAME_BOLD,
            fontSize=12,
            leading=22,
            alignment=TA_LEFT,
            wordWrap="CJK",
            textColor=link_color,
            underlineWidth=0.4,
            underlineOffset=-1.2,
            underlineColor=link_color,
        ),
        "toc_page": ParagraphStyle(
            name="TocPage",
            fontName="Helvetica",
            fontSize=12,
            leading=22,
            alignment=TA_RIGHT,
        ),
        "chapter": ParagraphStyle(
            name="ChapterTitle",
            fontName=FONT_NAME_BOLD,
            fontSize=16,
            leading=26,
            alignment=TA_CENTER,
            wordWrap="CJK",
            spaceAfter=14,
            spaceBefore=4,
        ),
        "branch": ParagraphStyle(
            name="BranchHeading",
            fontName=FONT_NAME_BOLD,
            fontSize=18,
            leading=28,
            alignment=TA_CENTER,
            wordWrap="CJK",
            spaceAfter=16,
            spaceBefore=8,
        ),
        "body": ParagraphStyle(
            name="Body",
            fontName=FONT_NAME,
            fontSize=12,
            leading=22,
            alignment=TA_JUSTIFY,
            firstLineIndent=24,
            wordWrap="CJK",
            spaceAfter=8,
        ),
    }

    page_width, page_height = A4
    margin = 20 * mm
    frame_w = page_width - 2 * margin
    frame_h = page_height - 2 * margin
    max_img_w = frame_w
    max_img_h = 160 * mm
    page_col = 18 * mm
    title_col = frame_w - page_col

    def make_image(item: dict) -> Image | None:
        path = _image_exists(name, item)
        if not path:
            return None
        try:
            raw = Path(path).read_bytes()
            reader = ImageReader(BytesIO(raw))
            iw, ih = reader.getSize()
            if not iw or not ih:
                return None
            scale = min(max_img_w / float(iw), max_img_h / float(ih), 1.0)
            img = Image(BytesIO(raw), width=iw * scale, height=ih * scale, kind="direct")
            img.hAlign = "CENTER"
            return img
        except Exception:
            logger.info("skipped unreadable illustration id=%s", item.get("id") or "")
            return None

    def body_flowables(text_body: str, title_text: str) -> list:
        items: list = []
        remainder = _drop_leading_title(text_body or "", title_text)
        parts = ILLUST_MARKER_RE.split(remainder)
        for index, chunk in enumerate(parts):
            if index % 2 == 1:
                item = illustrations.get(chunk)
                flow = make_image(item) if item else None
                if flow is not None:
                    items.append(Spacer(1, 6 * mm))
                    items.append(flow)
                    items.append(Spacer(1, 6 * mm))
                continue
            for para in _split_paragraphs(chunk):
                items.append(Paragraph(_escape_para(para), styles["body"]))
        return items

    class DestinationMark(Flowable):
        def __init__(self, dest: str, store: dict[str, int], outline_title: str = "", outline_level: int = 0) -> None:
            Flowable.__init__(self)
            self.dest = dest
            self.store = store
            self.outline_title = outline_title
            self.outline_level = outline_level
            self.width = 0
            self.height = 0

        def draw(self) -> None:
            self.store[self.dest] = int(self.canv.getPageNumber())
            # Jump to the top of this page, keeping the reader's zoom when possible.
            self.canv.bookmarkPage(self.dest, fit="XYZ", left=0, top=page_height, zoom=0)
            if self.outline_title:
                try:
                    self.canv.addOutlineEntry(
                        self.outline_title,
                        self.dest,
                        level=int(self.outline_level or 0),
                        closed=False,
                    )
                except Exception:
                    pass

    class TocLinkRow(Flowable):
        """A TOC row whose title cell is an internal link to dest."""

        def __init__(self, label: str, page_no: int | None, dest: str, *, branch: bool = False, indent: int = 0) -> None:
            Flowable.__init__(self)
            self.label = label
            self.page_no = page_no
            self.dest = dest
            self.branch = branch
            self.indent = max(0, int(indent or 0))
            self._table = None
            self.width = 0
            self.height = 0

        def _build(self):
            style = styles["toc_branch"] if self.branch else styles["toc_item"]
            left = Paragraph(_escape_para(self.label), style)
            right = Paragraph("" if self.page_no is None else str(self.page_no), styles["toc_page"])
            table = Table([[left, right]], colWidths=[title_col, page_col])
            left_pad = self.indent * 8 * mm
            table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                        ("LEFTPADDING", (0, 0), (0, 0), left_pad),
                        ("LEFTPADDING", (1, 0), (1, 0), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]
                )
            )
            return table

        def wrap(self, availWidth, availHeight):
            self._table = self._build()
            width, height = self._table.wrap(availWidth, availHeight)
            self.width, self.height = width, height
            return width, height

        def draw(self) -> None:
            self._table.drawOn(self.canv, 0, 0)
            # The whole title cell (including indent) is clickable.
            self.canv.linkRect("", self.dest, (0, 0, title_col, self.height), relative=1, thickness=0)

    dest_pages: dict[str, int] = {}

    def build_story(page_map: dict[str, int] | None) -> list:
        story: list = []
        story.append(Spacer(1, 70 * mm))
        story.append(Paragraph(_escape_para(title), styles["cover"]))
        story.append(PageBreak())
        story.append(Paragraph("目录", styles["toc_head"]))
        for section in built:
            dest_name = section["dest"]
            page_no = page_map.get(dest_name) if page_map else None
            if section.get("kind") == "heading":
                story.append(
                    TocLinkRow(
                        section.get("title") or "",
                        page_no,
                        dest_name,
                        branch=True,
                        indent=int(section.get("toc_level") or 0),
                    )
                )
            else:
                story.append(
                    TocLinkRow(
                        section.get("title") or "",
                        page_no,
                        dest_name,
                        branch=False,
                        indent=int(section.get("toc_level") or 0),
                    )
                )
        story.append(PageBreak())
        previous_kind = None
        for section in built:
            kind = section.get("kind")
            # New endings start on a new page. The first chapter of an ending
            # stays on the same page as its section heading.
            if previous_kind is not None and not (previous_kind == "heading" and kind == "chapter"):
                story.append(PageBreak())
            dest_name = section["dest"]
            outline_title = section.get("title") or ""
            story.append(
                DestinationMark(
                    dest_name,
                    dest_pages,
                    outline_title=outline_title,
                    outline_level=int(section.get("toc_level") or 0),
                )
            )
            if kind == "heading":
                story.append(Paragraph(_escape_para(section.get("title") or ""), styles["branch"]))
            else:
                story.append(Paragraph(_escape_para(section.get("title") or ""), styles["chapter"]))
                for item in section.get("front_items") or []:
                    flow = make_image(item)
                    if flow is not None:
                        story.append(Spacer(1, 4 * mm))
                        story.append(flow)
                        story.append(Spacer(1, 6 * mm))
                story.extend(body_flowables(section.get("text") or "", section.get("title") or ""))
            previous_kind = kind
        return story

    def draw_page(canvas, doc) -> None:
        # Helvetica is used for digits so page numbers stay visible even if a
        # CJK face fails to embed ASCII glyphs.
        canvas.saveState()
        canvas.setFillColorRGB(0.35, 0.32, 0.28)
        canvas.setFont("Helvetica", 9)
        canvas.drawCentredString(page_width / 2.0, 12 * mm, str(doc.page))
        canvas.restoreState()

    def write_to(path: Path, story: list) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        doc = BookDocTemplate(
            str(path),
            pagesize=A4,
            title=title,
            author="本地小说写作",
        )
        frame = Frame(margin, margin, frame_w, frame_h, id="normal", showBoundary=0)
        doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=draw_page)])
        doc.build(story)

    pass1 = folder / f".export-pass1-{_stamp()}.pdf"
    try:
        write_to(pass1, build_story(None))
        for _ in range(2):
            snapshot = dict(dest_pages)
            dest_pages.clear()
            write_to(pass1, build_story(snapshot))
            if dest_pages == snapshot:
                break
        final_path = _commit_pdf(pass1, Path(target), title)
    finally:
        if pass1.exists():
            try:
                pass1.unlink()
            except OSError:
                pass

    logger.info("exported book pdf name=%s file=%s", name, final_path.name)
    return final_path, final_path.name


def _commit_pdf(src: Path, target: Path, title: str) -> Path:
    data = src.read_bytes()
    if not data:
        raise ExportPdfError("导出 PDF 失败，请稍后重试。")
    candidates = [target]
    if target.name != "全书.pdf":
        candidates.append(target.with_name("全书.pdf"))
    stamped = target.with_name(_INVALID_FILE.sub(" ", f"《{title}》-{_stamp()}.pdf").strip() or f"全书-{_stamp()}.pdf")
    candidates.append(stamped)
    last_error: Exception | None = None
    for path in candidates:
        try:
            path.write_bytes(data)
            return path
        except OSError as exc:
            last_error = exc
            continue
    raise ExportPdfError("无法写入 PDF 文件，请确认该书文件夹可写，并关闭已打开的同名 PDF。") from last_error


def _stamp() -> str:
    return datetime.now(HONG_KONG).strftime("%Y%m%d-%H%M%S")


def _image_exists(name: str, item: dict | None) -> Path | None:
    if not item:
        return None
    try:
        path = image_path(name, item)
    except BookError:
        return None
    try:
        if path.is_file() and path.stat().st_size > 0:
            return path
    except OSError:
        return None
    return None


def _drop_leading_title(text: str, title: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    # Skip a leading heading that was already used as the chapter title.
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx < len(lines):
        heading = first_heading(lines[idx])
        if heading and (heading == (title or "").strip() or lines[idx].strip().lstrip("#").strip() == (title or "").strip()):
            idx += 1
            while idx < len(lines) and not lines[idx].strip():
                idx += 1
    return "\n".join(lines[idx:])


def _split_paragraphs(text: str) -> list[str]:
    if not text:
        return []
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", cleaned)
    paras: list[str] = []
    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        # Line-as-paragraph (common in this app) vs wrapped block.
        if len(lines) == 1:
            paras.append(lines[0])
        elif all(len(line) < 80 or line.startswith("　") or line.startswith("  ") for line in lines):
            paras.extend(lines)
        else:
            paras.append("".join(lines))
    return paras


def _escape_para(text: str) -> str:
    return xml_escape(text or "").replace("\n", "<br/>")
