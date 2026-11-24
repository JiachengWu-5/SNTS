"""Outline and chapter prompt orchestration."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator
from difflib import SequenceMatcher

from app import books, history, longform, style
from app.config import CONTEXT_TOKENS, PROMPT_SAFETY_MARGIN, THINKING_RESERVE_TOKENS
from app.llm import ChatResult, LLMError, StreamEvent, available_output_tokens, plan_max_tokens, stream_chat_with_retry
from app.textutil import HANZI_RE, count_hanzi, count_visible_hanzi, estimate_tokens, strip_illustration_markers

logger = logging.getLogger("novel_writer.generate")


def chapter_text_for_llm(text: str | None) -> str:
    """Strip illustration markers before chapter prose is sent to the writing model."""
    return strip_illustration_markers(text or "").strip()


SYSTEM_RULES = (
    "你是一名中文长篇小说作者助手。"
    "所有出场人物必须是原创的成年角色，年龄一律在 18 岁或以上。"
    "不得描写、暗示或模仿任何未成年人（未满 18 岁）或校园未成年的色情内容。"
    "不要输出思考过程或解释性前言，只输出用户要求的文稿。"
)


def _style_block(excerpts: list[dict]) -> str:
    if not excerpts:
        return "（未提供文风样本，请使用清晰、流畅的当代中文小说笔法。）"
    parts = []
    for index, item in enumerate(excerpts, 1):
        parts.append(f"【文风样本{index}：{item['name']}】\n{item['text']}")
    return "\n\n".join(parts)


def _load_styles(
    book_name: str,
    style_ids: list[str],
    prompt_chars_guess: int,
    output_tokens: int,
    max_total_chars: int | None = None,
) -> list[dict]:
    count = max(len(style_ids or []), 1)
    prompt_tokens = estimate_tokens("x" * prompt_chars_guess) + 800
    max_each = style.budget_excerpt_chars(count, prompt_tokens, output_tokens, CONTEXT_TOKENS)
    if max_total_chars is not None:
        max_each = min(max_each, max(80, max_total_chars // count))
    excerpts = style.resolve_style_ids(book_name, style_ids or [], max_each)

    def overflow(items: list[dict]) -> bool:
        token_overflow = (
            estimate_tokens(_style_block(items)) + prompt_tokens + output_tokens
            > CONTEXT_TOKENS - PROMPT_SAFETY_MARGIN
        )
        char_overflow = (
            max_total_chars is not None
            and sum(len(item.get("text") or "") for item in items) > max_total_chars
        )
        return token_overflow or char_overflow

    # If still too large, shrink further.
    while excerpts and overflow(excerpts):
        floor = 80 if max_total_chars is not None else 200
        max_each = max(floor, max_each // 2)
        excerpts = style.resolve_style_ids(book_name, style_ids or [], max_each)
        if max_each <= floor:
            break
    return excerpts


def _clip_text(text: str, max_chars: int) -> str:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n……\n"


def _compact_outline(preamble: str, chapters: list[dict], chapter_num: int) -> str:
    parts: list[str] = []
    if preamble.strip():
        parts.append(_clip_text(preamble.strip(), 1600))
    for chapter in chapters:
        num = chapter.get("num")
        heading = chapter.get("heading") or ""
        body = chapter.get("text") or ""
        if num == chapter_num:
            parts.append(f"{heading}（详见「本章大纲」，此处不重复）")
            continue
        if num is not None and abs(num - chapter_num) == 1:
            parts.append(_clip_text(body, 700))
            continue
        lines = body.splitlines()
        preview = ""
        for line in lines[1:]:
            if line.strip():
                preview = line.strip()[:80]
                break
        parts.append(heading + (f"\n{preview}" if preview else ""))
    return "\n\n".join(part for part in parts if part).strip()


def _outline_format_instructions() -> str:
    return (
        "输出格式：\n"
        "# 《书名》\n\n"
        "## 故事线\n\n"
        "……\n\n"
        "## 大纲\n\n"
        "### 第一章 标题\n\n"
        "……\n\n"
        "### 第二章 标题\n\n"
        "……\n"
    )


REVISE_CHUNK_HANZI = 2200
REVISE_MIN_RATIO = 0.7
CONTINUE_MIN_OUTPUT_CHARS = 800
CONTINUE_MIN_PLANNED_TOKENS = 512
AUTO_CONTINUE_RATIO = 0.85
AUTO_CONTINUE_MAX_ROUNDS = 8
AUTO_CONTINUE_MIN_ADD_HANZI = 200
AUTO_CONTINUE_MAX_STALLS = 2
AUTO_CONTINUE_DEDUPE_REVISION = (
    "检查并去除本章重复片段，只删重复的句子和段落，其余情节、措辞和未提到的部分一律不动。"
)
EXPAND_MIN_GROWTH_HANZI = 200
EXPAND_MIN_COVER = 0.7
AUTO_EXPAND_MAX_ROUNDS = 4
AUTO_EXPAND_MAX_STALLS = 2
CHAPTER_TITLE_RE = re.compile(r"^(第[一二三四五六七八九十百千零〇\d]+章[^\n]*)\n+")


def should_auto_continue(text: str, target_chars: int) -> bool:
    """True when the chapter is still short of the allowed 15% window under the target."""
    target = max(200, int(target_chars or 0))
    return count_hanzi(text) < int(target * AUTO_CONTINUE_RATIO)



def should_stop_after_dedupe(before_hanzi: int, added: int, after_hanzi: int) -> bool:
    """True when a post-continue revise removed almost all of the newly added text."""
    kept_added = after_hanzi - before_hanzi
    threshold = min(AUTO_CONTINUE_MIN_ADD_HANZI, max(0, int(added * 0.3)))
    return kept_added < threshold


async def _auto_continue_dedupe_repeats(
    *,
    book_name: str,
    cid: str,
    chapter_stored: str,
    style_ids: list[str] | None,
    temperature: float | None,
    enable_thinking: bool | None,
    target_chars: int | None,
    llm_url: str | None,
    cancel_event,
) -> AsyncIterator[StreamEvent]:
    """Run the existing revise path to drop repeated fragments after auto-continue."""
    yield StreamEvent(kind="status", message="正在检查并去除本章重复片段…")
    async for event in generate_chapter(
        book_name,
        cid,
        mode="revise",
        style_ids=style_ids,
        temperature=temperature,
        enable_thinking=enable_thinking,
        target_chars=target_chars,
        llm_url=llm_url,
        revision=AUTO_CONTINUE_DEDUPE_REVISION,
        chapter_text=chapter_stored,
        cancel_event=cancel_event,
    ):
        yield event


def _strip_leading_chapter_title(text: str) -> str:
    return CHAPTER_TITLE_RE.sub("", (text or "").lstrip(), count=1)


def extract_continue_addition(current: str, generated: str) -> str:
    """Return only genuinely new continuation text; empty if the model rewrote the chapter."""
    current = (current or "").strip()
    generated = (generated or "").strip()
    if not generated:
        return ""
    if not current:
        return generated

    if generated.startswith(current):
        return generated[len(current) :].strip()

    cur_body = _strip_leading_chapter_title(current)
    gen_body = _strip_leading_chapter_title(generated)
    if cur_body and gen_body.startswith(cur_body):
        return gen_body[len(cur_body) :].strip()

    def starts_like_opening(cur: str, gen: str) -> bool:
        head = cur[:24]
        return len(head) >= 16 and gen.startswith(head)

    if starts_like_opening(current, generated) or starts_like_opening(cur_body, gen_body):
        matcher = SequenceMatcher(None, cur_body or current, gen_body or generated, autojunk=False)
        best_end = 0
        best_b_end = 0
        for a, b, sz in matcher.get_matching_blocks():
            if sz >= 32 and a + sz >= best_end:
                best_end = a + sz
                best_b_end = b + sz
        source = cur_body or current
        dest = gen_body or generated
        if best_end >= int(len(source) * 0.7):
            return dest[best_b_end:].strip()
        return ""

    def after_tail(cur: str, gen: str) -> str | None:
        for n in (240, 160, 120, 80, 48, 32, 24, 16, 12):
            if len(cur) >= n:
                tail = cur[-n:]
                pos = gen.rfind(tail)
                if pos != -1:
                    return gen[pos + len(tail) :].strip()
        return None

    found = after_tail(current, generated)
    if found is not None:
        return found
    if cur_body != current or gen_body != generated:
        found = after_tail(cur_body, gen_body)
        if found is not None:
            return found

    ratio = SequenceMatcher(None, current[:4000], generated[:4000], autojunk=False).ratio()
    if ratio >= 0.65:
        if count_hanzi(generated) <= int(count_hanzi(current) * 1.15):
            return ""
        matcher = SequenceMatcher(None, current, generated, autojunk=False)
        best_end = 0
        best_b_end = 0
        for a, b, sz in matcher.get_matching_blocks():
            if sz >= 32 and a + sz >= best_end:
                best_end = a + sz
                best_b_end = b + sz
        if best_end >= int(len(current) * 0.7):
            return generated[best_b_end:].strip()
        return ""
    return generated


def merge_continue_text(current: str, generated: str) -> str:
    current = (current or "").rstrip()
    addition = extract_continue_addition(current, generated)
    if not addition:
        return current + ("\n" if current else "")
    if not current:
        return addition + "\n"
    return current + "\n\n" + addition + "\n"


def continue_prompt_pieces(chapter_num: int, target_chars: int, current: str, repeat_section: str) -> tuple[str, str, str]:
    needed_chars = continue_needed_chars(current, target_chars)
    task = (
        f"请从「本章已写正文」的结尾处接着往下写「第{chapter_num}章」，不要重写、复述或改写已经存在的段落，也不要再输出本章标题或从头起笔。"
        f"本次续写大约再写 {needed_chars} 字（可在正负 15% 范围内），在达到这个字数之前不要收束全章。"
    )
    requirements = (
        "1. 必须严格按「本章大纲」中的情节、人物、场景、时间点和章末收束来写，不得改换故事线，也不得把其他章节的核心事件提前写进本章。\n"
        "2. 故事要沿着完整弧线走，不要把本章写成互不关联的场景堆叠；同时承接前面已经写好、并经过修改的正文。若有「路径前情摘要」，其中已锁定的地点、谁去谁的住处、时间和人物状态不得写反。\n"
        "3. 文风样本只供参考语气、句式和节奏。禁止使用样本中的人物、情节、设定或原句，更不得用样本故事替换本章大纲。\n"
        "4. 所有人物年龄 18 岁或以上。不得出现未成年人（未满 18 岁）的色情描写。\n"
        "5. 只输出尚未写过的新段落。第一句必须紧接上文最后一句。禁止输出本章标题，禁止从开头重写或复述已有正文。不要写作说明、字数统计或思考过程。\n"
    )
    closing = (
        f"\n请现在从已有正文的结尾处继续写第{chapter_num}章，不要重复已写内容，也不要重写开头。情节仍以这段「本章大纲」为准。\n"
        f"{repeat_section}\n"
        if repeat_section
        else f"\n请现在从已有正文的结尾处继续写第{chapter_num}章，不要重复已写内容，也不要重写开头。\n"
    )
    return task, requirements, closing



def continue_needed_chars(current: str, target_chars: int) -> int:
    leftover = int(target_chars) - count_hanzi(current)
    return max(leftover, CONTINUE_MIN_OUTPUT_CHARS)


def clip_tail_hanzi(text: str, max_hanzi: int) -> str:
    """Keep a suffix of roughly max_hanzi Chinese characters."""
    text = text or ""
    if max_hanzi <= 0:
        return ""
    if count_hanzi(text) <= max_hanzi:
        return text
    hanzi = 0
    i = len(text)
    while i > 0 and hanzi < max_hanzi:
        i -= 1
        if count_hanzi(text[i]):
            hanzi += 1
    tail = text[i:]
    snap = tail.find("\n")
    if 0 <= snap < 120 and count_hanzi(tail[snap + 1 :]) >= int(max_hanzi * 0.7):
        tail = tail[snap + 1 :].lstrip("\n")
    return tail


def _continue_current_block(current: str, tail_hanzi: int | None) -> str:
    used = current
    note = ""
    if tail_hanzi is not None and count_hanzi(current) > tail_hanzi:
        used = clip_tail_hanzi(current, tail_hanzi)
        note = "（本章前文较长，此处只给出接近结尾的段落，请从最后一句接着写，不要重写或复述已有正文。）\n"
    tail = used[-200:] if len(used) > 200 else used
    return (
        f"【本章已写正文（必须原样保留，只从结尾续写）】\n{note}{used}\n\n"
        f"【续写起点：请紧接这段最后一句往后写】\n{tail}\n\n"
    )


def _clip_previous_block(previous: str, max_chars: int) -> str:
    previous = (previous or "").strip()
    if not previous or max_chars <= 0:
        return ""
    if len(previous) <= max_chars:
        return previous + "\n\n"
    return "【上一章已写正文（结尾摘录，须承接）】\n……\n" + previous[-max_chars:].lstrip() + "\n\n"


def _chapter_user_prompt(
    *,
    task: str,
    requirements: str,
    current_section: str,
    compact_outline: str,
    idea_for_prompt: str,
    previous: str,
    current_block: str,
    excerpts: list[dict],
    closing: str,
    longform: str = "",
) -> str:
    user = (
        f"{task}\n"
        f"要求：\n{requirements}\n"
        f"【本章大纲（必须落实，不可偏离）】\n{current_section}\n\n"
        f"【全书结构（只供前后对照，不要把其他章写成本章）】\n{compact_outline or '（无）'}\n\n"
        f"【思路摘要】\n{idea_for_prompt or '（无）'}\n\n"
        f"{longform or ''}"
        f"{previous}"
        f"{current_block}"
        f"【文风参考（仅语气与句式，禁止采用其中情节）】\n{_style_block(excerpts)}\n"
    )
    if closing:
        user += closing
    return user


def fit_continue_prompt(
    *,
    current: str,
    previous: str,
    current_section: str,
    compact_outline: str,
    idea_for_prompt: str,
    excerpts: list[dict],
    chapter_num: int,
    target_chars: int,
    enable_thinking: bool,
    task: str = "",
    requirements: str = "",
    closing: str = "",
    longform: str = "",
) -> tuple[list[dict], int, bool, str]:
    """Shrink a continue prompt until the remaining chapter length has a real output budget."""
    needed_chars = continue_needed_chars(current, target_chars)
    needed_tokens = max(int(needed_chars * 2.2) + 256, CONTINUE_MIN_PLANNED_TOKENS)
    if not task:
        task = (
            f"请从「本章已写正文」的结尾处接着往下写「第{chapter_num}章」，不要重写、复述或改写已经存在的段落，也不要再输出本章标题或从头起笔。"
            f"本次续写大约再写 {needed_chars} 字（可在正负 15% 范围内）。"
        )
    stages = [
        {"prev": 10_000, "tail": 2500, "styles": True, "outline": None, "longform": None},
        {"prev": 1500, "tail": 2800, "styles": True, "outline": 2500, "longform": 2200},
        {"prev": 800, "tail": 2000, "styles": True, "outline": 1600, "longform": 1400},
        {"prev": 0, "tail": 1600, "styles": False, "outline": 1000, "longform": 800},
        {"prev": 0, "tail": 1100, "styles": False, "outline": 600, "longform": 0},
    ]
    last: tuple[list[dict], int, bool, int, int] | None = None
    for index, stage in enumerate(stages):
        prev = _clip_previous_block(previous, stage["prev"])
        outline = compact_outline or ""
        if stage["outline"] is not None:
            outline = _clip_text(outline, stage["outline"])
        styles = excerpts if stage["styles"] else []
        current_block = _continue_current_block(current, stage["tail"])
        lf = longform or ""
        if stage["longform"] is not None:
            lf = _clip_text(lf, stage["longform"]) if stage["longform"] else ""
        user = _chapter_user_prompt(
            task=task,
            requirements=requirements,
            current_section=current_section,
            compact_outline=outline,
            idea_for_prompt=idea_for_prompt,
            previous=prev,
            current_block=current_block,
            excerpts=styles,
            closing=closing,
            longform=lf,
        )
        messages = [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": user},
        ]
        prompt = system_and_user(messages)
        room = available_output_tokens(prompt)
        use_thinking = bool(enable_thinking and room >= needed_tokens + THINKING_RESERVE_TOKENS)
        planned = plan_max_tokens(prompt, target_chars=needed_chars, enable_thinking=use_thinking)
        last = (messages, planned, use_thinking, room, index)
        if planned >= CONTINUE_MIN_PLANNED_TOKENS:
            already = count_hanzi(current)
            status = f"本章已有 {already} 字，本次续写按约 {needed_chars} 字重新分配了输出额度"
            if index > 0:
                status += "；提示已按上下文空间压缩，只保留接近结尾的正文"
            if enable_thinking and not use_thinking:
                status += "；为给正文留出额度，本次先不思考直接写"
            return messages, planned, use_thinking, status
    assert last is not None
    messages, planned, use_thinking, _room, _index = last
    return messages, planned, use_thinking, ""



def split_chapter_chunks(text: str, max_hanzi: int = REVISE_CHUNK_HANZI) -> list[str]:
    """Split a chapter into paragraph-packed chunks so each can get its own output budget."""
    text = (text or "").replace("\r\n", "\n")
    if not text.strip():
        return []
    if count_hanzi(text) <= max_hanzi:
        return [text]
    units: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        j = text.find("\n", i)
        if j < 0:
            units.append(text[i:])
            break
        k = j + 1
        while k < n and text[k] == "\n":
            k += 1
        units.append(text[i:k])
        i = k
    chunks: list[str] = []
    buf = ""
    for unit in units:
        pieces = [unit]
        if count_hanzi(unit) > max_hanzi:
            pieces = _split_oversize_unit(unit, max_hanzi)
        for piece in pieces:
            if buf and count_hanzi(buf + piece) > max_hanzi:
                chunks.append(buf)
                buf = piece
            else:
                buf += piece
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


def _split_oversize_unit(unit: str, max_hanzi: int) -> list[str]:
    sentences = re.split(r"(?<=[。！？!?])", unit)
    parts: list[str] = []
    buf = ""
    for sent in sentences:
        if not sent:
            continue
        if buf and count_hanzi(buf + sent) > max_hanzi:
            parts.append(buf)
            buf = sent
        else:
            buf += sent
    if buf:
        parts.append(buf)
    final: list[str] = []
    for part in parts:
        if count_hanzi(part) <= max_hanzi:
            final.append(part)
            continue
        # Hard split by characters as a last resort.
        acc = ""
        for ch in part:
            acc += ch
            if count_hanzi(acc) >= max_hanzi:
                final.append(acc)
                acc = ""
        if acc:
            final.append(acc)
    return final


def join_chapter_chunks(chunks: list[str]) -> str:
    pieces = [c.strip("\n") for c in chunks if c and c.strip()]
    if not pieces:
        return ""
    return "\n\n".join(pieces) + "\n"


def revise_needed_tokens(current: str) -> int:
    return max(int(count_hanzi(current) * 2.2) + 256, 512)


def revise_should_chunk(prompt_text: str, current: str) -> bool:
    room = available_output_tokens(prompt_text)
    return room < int(revise_needed_tokens(current) * 0.85)


def revise_result_too_short(original: str, revised: str, min_ratio: float = REVISE_MIN_RATIO) -> bool:
    old = count_hanzi(original)
    new = count_hanzi(revised)
    if old <= 0:
        return not (revised or "").strip()
    return new < int(old * min_ratio)


def revise_short_notice(original: str, revised: str, min_ratio: float = REVISE_MIN_RATIO) -> str | None:
    """A reminder when the revision is shorter. Never used to discard the new text."""
    if not revise_result_too_short(original, revised, min_ratio=min_ratio):
        return None
    old = count_hanzi(original)
    new = count_hanzi(revised)
    return f"修改后共 {new} 字，比原文少了 {max(0, old - new)} 字。若不是你想要的，请用「恢复上一版」。"


def _revise_done_extra(cid: str, current: str, text: str, min_ratio: float = REVISE_MIN_RATIO) -> dict:
    extra = {"hanzi": count_hanzi(text), "chapter": cid, "saved": True}
    notice = revise_short_notice(current, text, min_ratio=min_ratio)
    if notice:
        extra["short_warning"] = True
        extra["old_hanzi"] = count_hanzi(current)
        extra["message"] = notice
    return extra

def expand_needed_tokens(current: str, target_chars: int) -> int:
    goal = max(int(target_chars or 0), count_hanzi(current))
    return max(int(goal * 2.2) + 256, 512)


def expand_should_chunk(prompt_text: str, current: str, target_chars: int) -> bool:
    # Prefer segment-by-segment expand so the model keeps each passage's original wording.
    if count_hanzi(current) > REVISE_CHUNK_HANZI:
        return True
    room = available_output_tokens(prompt_text)
    return room < int(expand_needed_tokens(current, target_chars) * 0.85)


def _hanzi_seq(text: str) -> str:
    return "".join(HANZI_RE.findall(text or ""))


def expand_original_coverage(original: str, expanded: str) -> float:
    """How much of the original hanzi sequence is still present, in order, in the expansion."""
    a = _hanzi_seq(original)
    b = _hanzi_seq(expanded)
    if not a:
        return 1.0
    matched = sum(n for _i, _j, n in SequenceMatcher(a=a, b=b, autojunk=False).get_matching_blocks())
    return matched / len(a)


def expand_result_too_short(original: str, expanded: str) -> bool:
    """True if the result did not meaningfully grow — not a real 扩写."""
    old = count_hanzi(original)
    new = count_hanzi(expanded)
    if old <= 0:
        return not (expanded or "").strip()
    return new < old + EXPAND_MIN_GROWTH_HANZI


def expand_is_valid(original: str, expanded: str) -> bool:
    """A real expansion keeps the original wording and is clearly longer."""
    if not (expanded or "").strip():
        return False
    if expand_result_too_short(original, expanded):
        return False
    if expand_original_coverage(original, expanded) < EXPAND_MIN_COVER:
        return False
    return True


def _prefix_with_hanzi(text: str, n: int) -> str:
    """Shortest prefix of text that contains n hanzi, or the whole text."""
    text = text or ""
    found = 0
    for i, ch in enumerate(text):
        if HANZI_RE.match(ch):
            found += 1
            if found >= n:
                return text[: i + 1]
    return text


def _hanzi_char_index(text: str, n_hanzi: int) -> int:
    """Character index just after the n_hanzi-th hanzi, or len(text)."""
    found = 0
    for i, ch in enumerate(text):
        if HANZI_RE.match(ch):
            found += 1
            if found >= n_hanzi:
                return i + 1
    return len(text or "")


def _opening_anchors(text: str) -> list[str]:
    body = _strip_leading_chapter_title(text or "").strip()
    anchors: list[str] = []
    seen: set[str] = set()
    for src in (body, (text or "").strip()):
        if not src:
            continue
        for n in (80, 64, 48, 40, 32, 24):
            if count_hanzi(src) < n:
                continue
            anchor = _prefix_with_hanzi(src, n).strip()
            if anchor and anchor not in seen and count_hanzi(anchor) >= 24:
                seen.add(anchor)
                anchors.append(anchor)
    anchors.sort(key=lambda s: count_hanzi(s), reverse=True)
    return anchors


_TRAILING_CHAPTER_TITLE_RE = re.compile(
    r"(?:\n+)(?:第[一二三四五六七八九十百千零〇\d]+章[^\n]*)\s*$"
)


def _looks_like_restart_boundary(text: str, pos: int) -> bool:
    if pos <= 0:
        return False
    prev = text[pos - 1]
    if prev in "\n\r \t":
        return True
    if prev in "。！？!?…—”」』\"'）)":
        return True
    return False


def _map_hanzi_pos_to_char(text: str, hanzi_pos: int) -> int:
    found = 0
    for i, ch in enumerate(text):
        if HANZI_RE.match(ch):
            if found == hanzi_pos:
                return i
            found += 1
    return -1


def _find_anchor_after(text: str, anchor: str, start: int) -> int:
    """Index of anchor at or after start, trying exact then hanzi-sequence match."""
    if not anchor or start >= len(text):
        return -1
    pos = text.find(anchor, start)
    if pos != -1:
        return pos
    # Allow punctuation-only differences by matching the hanzi sequence.
    seq = _hanzi_seq(text)
    aseq = _hanzi_seq(anchor)
    if len(aseq) < 24:
        return -1
    start_h = count_hanzi(text[:start])
    found = seq.find(aseq, start_h)
    if found == -1:
        return -1
    return _map_hanzi_pos_to_char(text, found)


def trim_expand_restart(original: str, generated: str) -> str:
    """Cut a second telling that starts from the chapter opening to pad length.

    After finishing the expanded chapter, the model sometimes restarts from the
    beginning (original or already-expanded opening) to reach the target. The
    first telling is kept; the repeated opening and everything after it are dropped.
    A trailing chapter title that only introduces the restart is also removed.
    """
    generated = generated or ""
    if not generated.strip():
        return generated
    original = original or ""

    min_hanzi = max(200, int(count_hanzi(original) * 0.5) if original else 200)
    # Never search inside the legitimate opening itself.
    start = _hanzi_char_index(generated, min_hanzi)
    if start >= len(generated):
        return generated.rstrip() if generated.endswith(("\n", " ", "\t")) else generated

    anchors = _opening_anchors(original) + _opening_anchors(generated)
    # Dedup while keeping longest-first order.
    seen: set[str] = set()
    unique: list[str] = []
    for a in anchors:
        if a not in seen:
            seen.add(a)
            unique.append(a)

    best_pos: int | None = None
    for anchor in unique:
        pos = _find_anchor_after(generated, anchor, start)
        if pos == -1:
            continue
        long_enough = count_hanzi(anchor) >= 40
        if not long_enough and not _looks_like_restart_boundary(generated, pos):
            continue
        if best_pos is None or pos < best_pos:
            best_pos = pos

    if best_pos is None:
        return generated

    trimmed = generated[:best_pos]
    trimmed = _TRAILING_CHAPTER_TITLE_RE.sub("", trimmed).rstrip()
    # Don't cut if that would throw away almost everything (false positive).
    if count_hanzi(trimmed) < max(50, int(count_hanzi(original) * 0.8) if original else 50):
        return generated
    return trimmed



def expand_chunk_target(chunk: str, current: str, target_chars: int) -> int:
    total = max(count_hanzi(current), 1)
    goal = max(int(target_chars or 0), total)
    ratio = goal / total
    return max(int(count_hanzi(chunk) * ratio), count_hanzi(chunk) + EXPAND_MIN_GROWTH_HANZI, 200)


def expand_goal_chars(current: str, target_chars: int) -> int:
    """Aim at the user's target; if already near it, allow a modest further expansion."""
    already = count_hanzi(current)
    target = max(200, int(target_chars or 0))
    if already >= int(target * AUTO_CONTINUE_RATIO):
        return min(20000, max(target, int(already * 1.2)))
    return max(target, already)


def expand_prompt_pieces(chapter_num: int, target_chars: int, current: str, repeat_section: str) -> tuple[str, str, str]:
    goal = expand_goal_chars(current, target_chars)
    already = count_hanzi(current)
    task = (
        f"请对「第{chapter_num}章」的「本章已写正文」做扩写，不是重写，也不是续写。"
        "扩写的意思是：原文里的句子、对话和情节都必须保留，只在原句内部，或在句子、段落之间，插入更具体的描写、心理、对话、动作和环境细节，让篇幅明显变长。"
        "禁止删减、压缩、概括或改写原句，禁止另起一套故事，禁止只在章末追加一段来凑字数。"
        f"原文现有 {already} 字，扩写后全章汉字数量应大约为 {goal} 字（可在正负 15% 范围内），且必须明显多于原文。"
        "若字数还不够，继续在已有场景和节拍内部插入细节；写到本章原来的收束处必须停止。"
        "禁止为了凑字数从头再写一遍，禁止把本章情节再讲述一次，禁止在章末另起一篇。"
    )
    requirements = (
        "1. 这是扩写，不是重写。原文语句要保留在输出里，读者应能从扩写稿中认出原文；只增加，不替换、不删减。\n"
        "2. 在原有场景和事件内部补充细节，可以在现有节拍之间插入短的过渡，但不要改变情节顺序、人物关系和章末收束，也不要把其他章节的核心事件提前写进本章。\n"
        "3. 必须输出扩写后的完整本章正文（包含被保留的原文和插入的新内容），而不是只输出新增段落。\n"
        "4. 文风样本只供参考语气、句式和节奏。禁止使用样本中的人物、情节、设定或原句。\n"
        "5. 所有人物年龄 18 岁或以上。不得出现未成年人（未满 18 岁）的色情描写。\n"
        "6. 只输出扩写后的完整本章正文（可含本章标题），不要写作说明、字数统计或思考过程。\n"
        "7. 写完本章原有的结尾后立即停止。不要从开头重新写，不要重复已经写过的段落，不要为了凑字数再开一篇。\n"
    )
    closing = (
        f"\n请现在输出扩写后的第{chapter_num}章完整正文。再次确认：这是扩写不是重写，必须保留原文句子，插入细节后写到约 {goal} 字；写完原有结尾就停止，不要从头再写一遍，不要为凑字数把本章再写一次。情节仍以这段「本章大纲」为准。\n"
        f"{repeat_section}\n"
        if repeat_section
        else f"\n请现在输出扩写后的第{chapter_num}章完整正文。这是扩写不是重写，必须保留原文句子，插入细节后写到约 {goal} 字；写完原有结尾就停止，不要从头再写一遍，不要为凑字数把本章再写一次。\n"
    )
    return task, requirements, closing


def build_expand_messages(
    *,
    chapter_num: int,
    current: str,
    target_chars: int,
    current_section: str,
    compact_outline: str,
    idea_for_prompt: str,
    excerpts: list[dict],
    previous: str,
    repeat_section: str,
    longform: str = "",
) -> list[dict]:
    task, requirements, closing = expand_prompt_pieces(
        chapter_num, target_chars, current, repeat_section
    )
    current_block = (
        f"【本章已写正文（原文必须全部保留，只在原句内部或句段之间插入扩写，并输出完整的一章）】\n{current}\n\n"
    )
    user = _chapter_user_prompt(
        task=task,
        requirements=requirements,
        current_section=current_section,
        compact_outline=compact_outline or "",
        idea_for_prompt=idea_for_prompt or "",
        previous=previous or "",
        current_block=current_block,
        excerpts=excerpts,
        closing=closing,
        longform=longform or "",
    )
    return [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]


async def generate_outline(
    book_name: str,
    *,
    idea: str | None = None,
    revision: str | None = None,
    outline: str | None = None,
    style_ids: list[str] | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    llm_url: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    if idea is not None:
        books.write_idea(book_name, idea)
    idea_text = (idea if idea is not None else books.read_idea(book_name)).strip()
    settings_block = longform.setting_prompt_blocks(book_name)
    world_text = longform.world_prompt_text(book_name)
    revision_text = (revision or "").strip()
    if outline is not None:
        outline_text = outline.strip()
        if outline_text:
            books.write_outline(book_name, outline_text + "\n")
    else:
        outline_text = books.read_outline(book_name).strip()

    if revision_text:
        if not outline_text:
            yield StreamEvent(kind="error", message="请先生成或填写大纲，再按改进思路修改")
            return
    elif not idea_text:
        yield StreamEvent(kind="error", message="请先写下思路，再生成大纲")
        return

    ids = style_ids if style_ids is not None else settings.get("selected_styles") or []
    temperature = settings["temperature"] if temperature is None else temperature
    enable_thinking = settings["enable_thinking"] if enable_thinking is None else enable_thinking
    llm_url = llm_url or settings["llm_url"]

    target_chars = 2500
    if revision_text:
        target_chars = max(2500, min(count_hanzi(outline_text) + 800, 8000))

    output_tokens = int(target_chars * 2.0) + 512 + (THINKING_RESERVE_TOKENS if enable_thinking else 0)
    prompt_guess = len(idea_text) + len(revision_text) + len(outline_text) + len(settings_block) + 1600
    excerpts = _load_styles(book_name, ids, prompt_chars_guess=prompt_guess, output_tokens=output_tokens)

    if revision_text:
        user = (
            "请在「当前大纲」的基础上，按「改进思路」做修改，而不是推倒重来、另写一版大纲。\n"
            "要求：\n"
            "1. 以当前大纲为底稿。改进思路没有点名要改的书名、人物、设定、章节划分、情节和原文措辞，一律保留。\n"
            "2. 只改改进思路明确要求的部分，改动要落到具体情节上，不要只复述原文，也不要另起一套故事线。\n"
            "3. 如果改进思路需要增删或合并章节，在原有结构上调整，并保持前后情节连贯。\n"
            "4. 保留原书名，除非改进思路明确要求改名。人物、情节保持原创；所有人物年龄 18 岁或以上。\n"
            "5. 若提供了世界观或人物设定，大纲须与之相符，不要改写这些设定本身。\n"
            "6. 文风样本只供参考语气和句式，禁止用样本里的情节替换当前大纲。\n"
            "7. 只输出修订后的完整 Markdown 大纲，不要前言、修改说明或对照表。\n\n"
            f"{_outline_format_instructions()}\n"
            f"【改进思路】\n{revision_text}\n\n"
            f"【原思路】\n{idea_text or '（无）'}\n\n"
            f"{settings_block}"
            f"【当前大纲】\n{outline_text}\n\n"
            f"【文风参考】\n{_style_block(excerpts)}\n"
        )
    else:
        user = (
            "请根据下面的「思路」和「文风样本」，为一部中文长篇小说拟定完整规划。\n"
            "要求：\n"
            "1. 先给出一个合适的书名。\n"
            "2. 再写完整故事线，必须有起承转合和贯穿全书的情节弧线，不要只是场景堆叠。\n"
            "3. 然后按章节给出大纲，每一章写清主要事件、冲突与章末钩子，章数根据思路自行安排。\n"
            "4. 人物、情节保持原创；所有人物年龄 18 岁或以上。\n"
            "5. 若提供了世界观或人物设定，大纲须与之相符，不要改写这些设定本身，也不要复述成另一份世界观。\n"
            "6. 尽量贴近文风样本的语气、句式和叙事节奏，但不要抄袭样本情节或原句。\n"
            "7. 只输出下方 Markdown 结构，不要前言后记。\n\n"
            f"{_outline_format_instructions()}\n"
            f"【思路】\n{idea_text}\n\n"
            f"{settings_block}"
            f"【文风参考】\n{_style_block(excerpts)}\n"
        )
    messages = [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]
    max_tokens = plan_max_tokens(system_and_user(messages), target_chars=target_chars, enable_thinking=enable_thinking)
    logger.info(
        "outline start book=%s revise=%s styles=%s thinking=%s max_tokens=%s",
        book_name,
        bool(revision_text),
        len(excerpts),
        enable_thinking,
        max_tokens,
    )
    async for event in stream_chat_with_retry(
        messages,
        llm_url=llm_url,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "done" and event.result and event.result.content.strip():
            if revision_text:
                books.backup_outline(book_name)
            new_outline = event.result.content.strip() + "\n"
            history.record_outline_change(book_name, outline_text, new_outline, "outline_revise" if revision_text else "outline")
            books.write_outline(book_name, new_outline)
            settings["selected_styles"] = ids
            books.save_settings(book_name, settings)
            event.extra = {"hanzi": count_hanzi(event.result.content), "saved": "大纲.md"}
        yield event


async def generate_chapter(
    book_name: str,
    chapter_id: str,
    *,
    mode: str = "generate",
    style_ids: list[str] | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    target_chars: int | None = None,
    llm_url: str | None = None,
    outline: str | None = None,
    revision: str | None = None,
    chapter_text: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    cid = books.validate_chapter_id(chapter_id)
    # Ensure the file exists
    path = books.chapter_path(book_name, cid)
    if not path.exists():
        books.write_chapter(book_name, cid, "")
    if chapter_text is not None:
        incoming = chapter_text.strip()
        # Do not let an empty "generate" payload wipe a saved chapter on disk.
        if incoming or (mode or "generate").lower() in {"continue", "revise", "expand"}:
            books.write_chapter(book_name, cid, (incoming + "\n") if incoming else "")

    idea_text = books.read_idea(book_name).strip()
    if outline is not None:
        outline_text = outline.strip()
        if outline_text:
            books.write_outline(book_name, outline_text + "\n")
    else:
        outline_text = books.read_outline(book_name).strip()
    if not outline_text:
        yield StreamEvent(kind="error", message="请先确认大纲，再分章写作")
        return

    ids = style_ids if style_ids is not None else settings.get("selected_styles") or []
    temperature = settings["temperature"] if temperature is None else temperature
    enable_thinking = settings["enable_thinking"] if enable_thinking is None else bool(enable_thinking)
    target_chars = target_chars or settings["chapter_target_chars"]
    target_chars = max(200, min(int(target_chars), 20000))
    llm_url = llm_url or settings["llm_url"]

    previous = _previous_chapter_context(book_name, cid)
    longform_block = longform.load_longform_blocks(book_name, cid, for_writing=True)
    current_raw = books.read_chapter(book_name, cid)["text"].strip()
    current = chapter_text_for_llm(current_raw)
    chapter_num = int(cid)
    preamble, chapters = books.split_outline_chapters(outline_text)
    current_section, compact_outline, chapter_num = _chapter_outline_context(
        book_name, cid, outline_text, preamble, chapters, chapter_num
    )
    if not current_section:
        current_section = compact_outline or outline_text
        repeat_section = ""
    else:
        repeat_section = current_section

    output_tokens = int(target_chars * 2.2) + 256 + (THINKING_RESERVE_TOKENS if enable_thinking else 0)
    base_prompt_guess = (
        len(idea_text)
        + len(current_section)
        + len(compact_outline)
        + len(previous)
        + len(longform_block)
        + len(current)
        + len((revision or "").strip())
        + 1800
    )
    excerpts = _load_styles(
        book_name,
        ids,
        prompt_chars_guess=base_prompt_guess,
        output_tokens=output_tokens,
        max_total_chars=1200,
    )

    revision_text = (revision or "").strip()
    mode = (mode or "generate").lower()
    if revision_text or mode == "revise":
        if not current:
            yield StreamEvent(kind="error", message="请先生成本章正文，再按改进意见修改")
            return
        mode = "revise"
        task = (
            f"请在「本章已写正文」的基础上，按「改进意见」修改「第{chapter_num}章」，而不是推倒重来、另写一章。"
            "除非改进意见明确要求增删篇幅，修改后的全章汉字数量应与当前正文相近。"
        )
        current_block = f"【改进意见】\n{revision_text}\n\n【本章已写正文（底稿）】\n{current}\n\n"
        requirements = (
            "1. 以当前正文为底稿。改进意见没有点名要改的情节、人物、对话、场景和原文措辞，一律保留。\n"
            "2. 只改改进意见明确要求的部分，改动要落到具体文字上，不要只复述原文，也不要另起一套故事。\n"
            "3. 修改后仍须严格符合「本章大纲」的情节、人物、场景和章末收束；不得把其他章节的核心事件提前写进本章。\n"
            "4. 文风样本只供参考语气、句式和节奏。禁止使用样本中的人物、情节、设定或原句，更不得用样本故事替换本章大纲。\n"
            "5. 所有人物年龄 18 岁或以上。不得出现未成年人（未满 18 岁）的色情描写。\n"
            "6. 只输出修改后的完整本章正文（可含本章标题），不要写作说明、字数统计或思考过程。\n"
        )
        closing = (
            f"\n请现在输出修改后的第{chapter_num}章完整正文。再次确认：以当前正文为底稿，只按改进意见修改，情节仍以这段「本章大纲」为准。\n"
            f"{repeat_section}\n"
            if repeat_section
            else f"\n请现在输出修改后的第{chapter_num}章完整正文。以当前正文为底稿，只按改进意见修改。\n"
        )
    elif mode == "expand":
        if not current:
            yield StreamEvent(kind="error", message="请先生成本章正文，再扩写")
            return
        task, requirements, closing = expand_prompt_pieces(
            chapter_num, target_chars, current, repeat_section
        )
        current_block = (
            f"【本章已写正文（底稿，请在此基础上扩写，并输出完整的一章）】\n{current}\n\n"
        )
        target_chars = expand_goal_chars(current, target_chars)
    elif mode == "continue" and current:
        task = (
            f"请从「本章已写正文」的结尾处接着往下写「第{chapter_num}章」，不要重写、复述或改写已经存在的段落，也不要再输出本章标题或从头起笔。"
            f"本章完成后，全章汉字数量应大约为 {target_chars} 字（可在正负 15% 范围内）。"
        )
        tail = current[-200:] if len(current) > 200 else current
        current_block = (
            f"【本章已写正文（必须原样保留，只从结尾续写）】\n{current}\n\n"
            f"【续写起点：请紧接这段最后一句往后写】\n{tail}\n\n"
        )
        requirements = (
            "1. 必须严格按「本章大纲」中的情节、人物、场景、时间点和章末收束来写，不得改换故事线，也不得把其他章节的核心事件提前写进本章。\n"
            "2. 故事要沿着完整弧线走，不要把本章写成互不关联的场景堆叠；同时承接前面已经写好、并经过修改的正文。若有「路径前情摘要」，其中已锁定的地点、谁去谁的住处、时间和人物状态不得写反。\n"
            "3. 文风样本只供参考语气、句式和节奏。禁止使用样本中的人物、情节、设定或原句，更不得用样本故事替换本章大纲。\n"
            "4. 所有人物年龄 18 岁或以上。不得出现未成年人（未满 18 岁）的色情描写。\n"
            "5. 只输出本章正文（可含本章标题），不要写作说明、字数统计或思考过程。\n"
        )
        closing = (
            f"\n请现在从已有正文的结尾处继续写第{chapter_num}章，不要重复已写内容，也不要重写开头。情节仍以这段「本章大纲」为准。\n"
            f"{repeat_section}\n"
            if repeat_section
            else f"\n请现在从已有正文的结尾处继续写第{chapter_num}章，不要重复已写内容，也不要重写开头。\n"
        )
    else:
        task = (
            f"请撰写「第{chapter_num}章」的完整正文。"
            f"本章汉字数量应大约为 {target_chars} 字（可在正负 15% 范围内），写成完整的一章，不要只写开头。在达到大约这个字数之前不要收束全章或提前结束。"
        )
        current_block = ""
        requirements = (
            "1. 必须严格按「本章大纲」中的情节、人物、场景、时间点和章末收束来写，不得改换故事线，也不得把其他章节的核心事件提前写进本章。\n"
            "2. 故事要沿着完整弧线走，不要把本章写成互不关联的场景堆叠；同时承接前面已经写好、并经过修改的正文。若有「路径前情摘要」，其中已锁定的地点、谁去谁的住处、时间和人物状态不得写反。\n"
            "3. 文风样本只供参考语气、句式和节奏。禁止使用样本中的人物、情节、设定或原句，更不得用样本故事替换本章大纲。\n"
            "4. 所有人物年龄 18 岁或以上。不得出现未成年人（未满 18 岁）的色情描写。\n"
            "5. 只输出本章正文（可含本章标题），不要写作说明、字数统计或思考过程。\n"
        )
        closing = (
            f"\n请现在撰写第{chapter_num}章正文。再次确认：情节必须以这段「本章大纲」为准。\n"
            f"{repeat_section}\n"
            if repeat_section
            else ""
        )

    idea_for_prompt = idea_text if estimate_tokens(idea_text) < 1200 else idea_text[:800] + "\n……\n"

    user = _chapter_user_prompt(
        task=task,
        requirements=requirements,
        current_section=current_section,
        compact_outline=compact_outline or "",
        idea_for_prompt=idea_for_prompt,
        previous=previous,
        current_block=current_block,
        excerpts=excerpts,
        closing=closing,
        longform=longform_block,
    )

    messages = [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]
    if mode == "revise":
        target_chars = max(target_chars, count_hanzi(current))
        async for event in _revise_with_budget(
            book_name=book_name,
            cid=cid,
            current=current,
            revision_text=revision_text,
            current_section=current_section,
            compact_outline=compact_outline,
            idea_for_prompt=idea_for_prompt,
            excerpts=excerpts,
            chapter_num=chapter_num,
            messages=messages,
            target_chars=target_chars,
            enable_thinking=enable_thinking,
            temperature=temperature,
            llm_url=llm_url,
            cancel_event=cancel_event,
            longform=longform_block,
        ):
            yield event
        return

    if mode == "expand":
        async for event in _expand_with_budget(
            book_name=book_name,
            cid=cid,
            current=current,
            current_section=current_section,
            compact_outline=compact_outline,
            idea_for_prompt=idea_for_prompt,
            excerpts=excerpts,
            chapter_num=chapter_num,
            messages=messages,
            target_chars=target_chars,
            enable_thinking=enable_thinking,
            temperature=temperature,
            llm_url=llm_url,
            cancel_event=cancel_event,
            previous=previous,
            repeat_section=repeat_section,
            longform=longform_block,
        ):
            yield event
        return

    use_thinking = enable_thinking
    if mode == "continue" and current:
        task, requirements, closing = continue_prompt_pieces(
            chapter_num, target_chars, current, repeat_section
        )
        messages, max_tokens, use_thinking, status_message = fit_continue_prompt(
            current=current,
            previous=previous,
            current_section=current_section,
            compact_outline=compact_outline or "",
            idea_for_prompt=idea_for_prompt,
            excerpts=excerpts,
            chapter_num=chapter_num,
            target_chars=target_chars,
            enable_thinking=enable_thinking,
            task=task,
            requirements=requirements,
            closing=closing,
            longform=longform_block,
        )
        if max_tokens < CONTINUE_MIN_PLANNED_TOKENS:
            yield StreamEvent(
                kind="error",
                message="当前正文和上下文已经占满窗口，没有剩余额度续写。请先取消一部分文风样本后再点续写。",
            )
            return
        if status_message:
            yield StreamEvent(kind="status", message=status_message)
    else:
        max_tokens = plan_max_tokens(system_and_user(messages), target_chars=target_chars, enable_thinking=enable_thinking)
        if max_tokens < CONTINUE_MIN_PLANNED_TOKENS:
            yield StreamEvent(
                kind="error",
                message="当前提示过长，没有剩余额度生成正文。请先取消一部分文风样本后再试。",
            )
            return

    logger.info(
        "chapter start book=%s chapter=%s mode=%s thinking=%s target=%s max_tokens=%s styles=%s",
        book_name,
        cid,
        mode,
        use_thinking,
        target_chars,
        max_tokens,
        len(excerpts),
    )

    chapter_stored = current_raw if (mode == "continue" and current_raw) else ""
    chapter_text = current if (mode == "continue" and current) else ""
    did_backup = False
    stall_rounds = 0
    auto_round = 0
    final_event = None

    while True:
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            if final_event is not None:
                yield final_event
            else:
                yield StreamEvent(kind="cancelled", message="已停止生成")
            return

        if auto_round > 0:
            already = count_hanzi(chapter_text)
            yield StreamEvent(
                kind="status",
                message=f"本章已有 {already} 字，目标 {target_chars} 字，尚未写够，正在从结尾自动续写…",
            )
            task, requirements, closing = continue_prompt_pieces(
                chapter_num, target_chars, chapter_text, repeat_section
            )
            messages, max_tokens, use_thinking, status_message = fit_continue_prompt(
                current=chapter_text,
                previous=previous,
                current_section=current_section,
                compact_outline=compact_outline or "",
                idea_for_prompt=idea_for_prompt,
                excerpts=excerpts,
                chapter_num=chapter_num,
                target_chars=target_chars,
                enable_thinking=enable_thinking,
                task=task,
                requirements=requirements,
                closing=closing,
                longform=longform_block,
            )
            if max_tokens < CONTINUE_MIN_PLANNED_TOKENS:
                yield StreamEvent(
                    kind="status",
                    message="窗口已没有剩余额度继续写，先把已写内容保存。可以先取消一部分文风样本后再点续写。",
                )
                if final_event is not None:
                    yield final_event
                return
            if status_message:
                yield StreamEvent(kind="status", message=status_message)
            logger.info(
                "chapter auto-continue book=%s chapter=%s round=%s hanzi=%s target=%s max_tokens=%s",
                book_name,
                cid,
                auto_round,
                already,
                target_chars,
                max_tokens,
            )

        before_hanzi = count_hanzi(chapter_text)
        done_event = None
        async for event in stream_chat_with_retry(
            messages,
            llm_url=llm_url,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=use_thinking,
            cancel_event=cancel_event,
        ):
            if event.kind == "text":
                yield event
            elif event.kind == "done" and event.result and event.result.content.strip():
                raw = event.result.content.strip()
                if chapter_stored and (mode == "continue" or auto_round > 0):
                    text = merge_continue_text(chapter_stored, raw)
                else:
                    text = raw + "\n"
                if not did_backup:
                    if mode == "generate" and current_raw:
                        books.backup_chapter(book_name, cid)
                    did_backup = True
                history.record_chapter_change(book_name, cid, current_raw if mode == "generate" else chapter_stored, text, mode if mode in {"generate", "continue"} else "generate")
                books.write_chapter(book_name, cid, text)
                chapter_stored = text
                chapter_text = chapter_text_for_llm(text)
                event.text = text
                event.extra = {"hanzi": count_visible_hanzi(text), "chapter": cid, "saved": True}
                done_event = event
            elif event.kind in ("error", "cancelled"):
                if chapter_text and auto_round > 0 and event.kind == "error":
                    yield StreamEvent(
                        kind="status",
                        message=f"自动续写中断：{event.message}。已保存目前写好的正文。",
                    )
                    if final_event is not None:
                        yield final_event
                        return
                yield event
                return
            else:
                yield event

        if not done_event:
            if chapter_stored or chapter_text:
                kept_text = chapter_stored or chapter_text
                kept = StreamEvent(kind="done", result=ChatResult(content=kept_text), text=kept_text)
                kept.extra = {"hanzi": count_visible_hanzi(kept_text), "chapter": cid, "saved": True}
                yield kept
            return

        final_event = done_event
        added = count_hanzi(chapter_text) - before_hanzi
        if auto_round > 0 and added > 0:
            dedupe_done = None
            async for event in _auto_continue_dedupe_repeats(
                book_name=book_name,
                cid=cid,
                chapter_stored=chapter_stored,
                style_ids=ids,
                temperature=temperature,
                enable_thinking=enable_thinking,
                target_chars=target_chars,
                llm_url=llm_url,
                cancel_event=cancel_event,
            ):
                if event.kind == "done" and (event.text or "").strip():
                    dedupe_done = event
                elif event.kind == "cancelled":
                    yield event
                    return
                elif event.kind == "error":
                    yield StreamEvent(
                        kind="status",
                        message="去除重复时没有改出可用正文，已保留补字后的原稿",
                    )
                else:
                    yield event
            if dedupe_done:
                chapter_stored = dedupe_done.text
                chapter_text = chapter_text_for_llm(chapter_stored)
                final_event = dedupe_done
                if should_stop_after_dedupe(before_hanzi, added, count_hanzi(chapter_text)):
                    yield StreamEvent(
                        kind="status",
                        message="补字后的重复片段已去掉，几乎没有留下新内容，本章不再为凑字数继续补。",
                    )
                    yield final_event
                    return
        if not should_auto_continue(chapter_text, target_chars):
            yield final_event
            return

        auto_round += 1
        if auto_round > AUTO_CONTINUE_MAX_ROUNDS:
            yield StreamEvent(
                kind="status",
                message=(
                    f"已自动续写 {AUTO_CONTINUE_MAX_ROUNDS} 轮，本章现有 {count_hanzi(chapter_text)} 字，"
                    f"尚未完全达到目标 {target_chars} 字，先停在这里。可以再点续写。"
                ),
            )
            yield final_event
            return

        # The first pass of a fresh generate is allowed to be short; only later
        # rounds that add almost nothing count as a stall.
        if auto_round > 1 and added < AUTO_CONTINUE_MIN_ADD_HANZI:
            stall_rounds += 1
            if stall_rounds >= AUTO_CONTINUE_MAX_STALLS:
                yield StreamEvent(
                    kind="status",
                    message=(
                        f"自动续写没有写出足够的新内容，本章现有 {count_hanzi(chapter_text)} 字。"
                        "可以再点续写试试。"
                    ),
                )
                yield final_event
                return
        elif auto_round > 1:
            stall_rounds = 0


async def _revise_with_budget(
    *,
    book_name: str,
    cid: str,
    current: str,
    revision_text: str,
    current_section: str,
    compact_outline: str,
    idea_for_prompt: str,
    excerpts: list[dict],
    chapter_num: int,
    messages: list[dict],
    target_chars: int,
    enable_thinking: bool,
    temperature: float,
    llm_url: str | None,
    cancel_event,
    longform: str = "",
) -> AsyncIterator[StreamEvent]:
    """Revise using an output budget based on the existing chapter, chunking when a full rewrite will not fit."""
    target_chars = max(target_chars, count_hanzi(current))
    prompt = system_and_user(messages)
    if not revise_should_chunk(prompt, current):
        room = available_output_tokens(prompt)
        needed = revise_needed_tokens(current)
        use_thinking = bool(enable_thinking and room >= needed + THINKING_RESERVE_TOKENS)
        if enable_thinking and not use_thinking:
            yield StreamEvent(kind="status", message="为给修改后的正文留足字数，本次按现有正文分配额度，直接输出修改稿")
        max_tokens = plan_max_tokens(prompt, target_chars=target_chars, enable_thinking=use_thinking)
        logger.info(
            "revise one-shot book=%s chapter=%s hanzi=%s room=%s max_tokens=%s thinking=%s",
            book_name, cid, count_hanzi(current), room, max_tokens, use_thinking,
        )
        async for event in _finish_revise(
            book_name=book_name,
            cid=cid,
            current=current,
            stream=stream_chat_with_retry(
                messages,
                llm_url=llm_url,
                temperature=temperature,
                max_tokens=max_tokens,
                enable_thinking=use_thinking,
                cancel_event=cancel_event,
            ),
        ):
            yield event
        return

    chunks = split_chapter_chunks(current)
    yield StreamEvent(
        kind="status",
        message=f"本章现有 {count_hanzi(current)} 字，一次改不完整章，将按正文分成 {len(chunks)} 段分别分配额度后修改",
    )
    logger.info(
        "revise chunked book=%s chapter=%s hanzi=%s chunks=%s",
        book_name, cid, count_hanzi(current), len(chunks),
    )
    revised_parts: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        prev_tail = (revised_parts[-1] if revised_parts else "")[-80:]
        next_head = chunks[index][:80] if index < len(chunks) else ""
        chunk_messages = _revise_chunk_messages(
            chapter_num=chapter_num,
            index=index,
            total=len(chunks),
            revision_text=revision_text,
            current_section=current_section,
            compact_outline=compact_outline,
            idea_for_prompt=idea_for_prompt,
            excerpts=excerpts,
            chunk=chunk,
            prev_tail=prev_tail,
            next_head=next_head,
            longform=longform,
        )
        chunk_target = max(count_hanzi(chunk) + 200, 200)
        chunk_prompt = system_and_user(chunk_messages)
        room = available_output_tokens(chunk_prompt)
        needed = revise_needed_tokens(chunk)
        use_thinking = bool(enable_thinking and room >= needed + THINKING_RESERVE_TOKENS)
        max_tokens = plan_max_tokens(chunk_prompt, target_chars=chunk_target, enable_thinking=use_thinking)
        if max_tokens < 512:
            yield StreamEvent(kind="error", message="上下文仍然不够修改本段，请缩短文风样本或关掉部分参考后再试")
            return
        yield StreamEvent(kind="status", message=f"正在按现有正文分配额度，修改第 {index}/{len(chunks)} 段…")
        logger.info(
            "revise chunk book=%s chapter=%s %s/%s hanzi=%s room=%s max_tokens=%s thinking=%s",
            book_name, cid, index, len(chunks), count_hanzi(chunk), room, max_tokens, use_thinking,
        )
        collected = []
        cancelled = False
        failed = None
        async for event in stream_chat_with_retry(
            chunk_messages,
            llm_url=llm_url,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=use_thinking,
            cancel_event=cancel_event,
        ):
            if event.kind == "text":
                collected.append(event.text)
                yield event
            elif event.kind == "status":
                yield event
            elif event.kind == "cancelled":
                cancelled = True
                yield event
                break
            elif event.kind == "error":
                failed = event
                break
            elif event.kind == "done":
                if event.text:
                    collected = [event.text]
        if cancelled:
            return
        if failed:
            yield failed
            return
        piece = "".join(collected).strip()
        if not piece:
            logger.warning("revise chunk %s/%s empty, keeping original", index, len(chunks))
            piece = chunk
            yield StreamEvent(kind="status", message=f"第 {index} 段没有返回内容，已保留该段原文")
        revised_parts.append(piece)

    text = join_chapter_chunks(revised_parts)
    if not (text or "").strip():
        yield StreamEvent(kind="error", message="修改结果为空，已保留原文")
        return
    books.backup_chapter(book_name, cid)
    history.record_chapter_change(book_name, cid, current, text, "revise")
    books.write_chapter(book_name, cid, text)
    done = StreamEvent(kind="done", result=ChatResult(content=text), text=text)
    done.extra = _revise_done_extra(cid, current, text)
    if done.extra.get("short_warning"):
        yield StreamEvent(kind="status", message=done.extra["message"])
    yield done


def _revise_chunk_messages(
    *,
    chapter_num: int,
    index: int,
    total: int,
    revision_text: str,
    current_section: str,
    compact_outline: str,
    idea_for_prompt: str,
    excerpts: list[dict],
    chunk: str,
    prev_tail: str,
    next_head: str,
    longform: str = "",
) -> list[dict]:
    user = (
        f"请在本段原文基础上，按「改进意见」修改「第{chapter_num}章」的第 {index}/{total} 段。"
        "这是分段修改，不是重写整章。请完整输出修改后的这一段。\n"
        "要求：\n"
        "1. 只输出这一段修改后的完整正文，不要输出其他段，不要写说明或思考过程。\n"
        "2. 改进意见没有点名要改的情节、人物、对话、场景和原文措辞，一律原样保留。\n"
        "3. 若改进意见与本段无关，请原样输出本段原文。\n"
        "4. 除非改进意见要求本段增减篇幅，本段汉字数量应与原文相近。\n"
        "5. 修改后仍须符合「本章大纲」。文风样本只供参考语气。所有人物年龄 18 岁或以上。\n\n"
        f"【改进意见】\n{revision_text or '（无）'}\n\n"
        f"【本章大纲（必须仍符合）】\n{current_section}\n\n"
        f"{longform or ''}"
        f"【本段位置】第 {index} 段，共 {total} 段。\n\n"
        f"【上一段结尾（只供衔接，不要重复输出）】\n{prev_tail or '（本章开头）'}\n\n"
        f"【下一段开头（只供衔接，不要写入本段）】\n{next_head or '（本章结尾）'}\n\n"
        f"【本段原文（请在此基础上修改，并完整输出本段）】\n{chunk}\n\n"
        f"【文风参考（仅语气与句式，禁止采用其中情节）】\n{_style_block(excerpts)}\n"
    )
    return [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]



async def _expand_with_budget(
    *,
    book_name: str,
    cid: str,
    current: str,
    current_section: str,
    compact_outline: str,
    idea_for_prompt: str,
    excerpts: list[dict],
    chapter_num: int,
    messages: list[dict],
    target_chars: int,
    enable_thinking: bool,
    temperature: float,
    llm_url: str | None,
    cancel_event,
    previous: str = "",
    repeat_section: str = "",
    longform: str = "",
) -> AsyncIterator[StreamEvent]:
    """Expand in place: keep the original wording, insert detail, grow toward the target."""
    user_target = expand_goal_chars(current, target_chars)
    original = current
    best = current
    did_backup = False
    stall = 0

    for rnd in range(1, AUTO_EXPAND_MAX_ROUNDS + 1):
        goal = expand_goal_chars(best, user_target)
        messages = build_expand_messages(
            chapter_num=chapter_num,
            current=best,
            target_chars=goal,
            current_section=current_section,
            compact_outline=compact_outline,
            idea_for_prompt=idea_for_prompt,
            excerpts=excerpts,
            previous=previous,
            repeat_section=repeat_section,
            longform=longform,
        )
        prompt = system_and_user(messages)
        already = count_hanzi(best)
        yield StreamEvent(
            kind="status",
            message=(
                f"第 {rnd} 轮扩写：本章已有 {already} 字，目标约 {goal} 字。"
                "会保留原文句子，只在中间补充细节，不是整章重写"
            ),
        )

        use_chunks = expand_should_chunk(prompt, best, goal)
        result = ""
        cancelled = False
        failed = None

        if not use_chunks:
            room = available_output_tokens(prompt)
            needed = expand_needed_tokens(best, goal)
            use_thinking = bool(enable_thinking and room >= needed + THINKING_RESERVE_TOKENS)
            if enable_thinking and not use_thinking:
                yield StreamEvent(kind="status", message="为给扩写后的正文留足字数，本次按目标字数分配额度，直接输出扩写稿")
            max_tokens = plan_max_tokens(prompt, target_chars=goal, enable_thinking=use_thinking)
            if max_tokens < CONTINUE_MIN_PLANNED_TOKENS:
                failed = StreamEvent(kind="error", message="当前提示过长，没有剩余额度扩写。请先取消一部分文风样本后再试。")
            else:
                logger.info(
                    "expand one-shot book=%s chapter=%s round=%s hanzi=%s target=%s room=%s max_tokens=%s thinking=%s",
                    book_name, cid, rnd, already, goal, room, max_tokens, use_thinking,
                )
                collected: list[str] = []
                async for event in stream_chat_with_retry(
                    messages,
                    llm_url=llm_url,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    enable_thinking=use_thinking,
                    cancel_event=cancel_event,
                ):
                    if event.kind == "text":
                        collected.append(event.text)
                        yield event
                    elif event.kind == "status":
                        yield event
                    elif event.kind == "cancelled":
                        cancelled = True
                        yield event
                        break
                    elif event.kind == "error":
                        failed = event
                        break
                    elif event.kind == "done":
                        if event.text:
                            collected = [event.text]
                result = "".join(collected).strip()
                trimmed = trim_expand_restart(best, result)
                if trimmed != result:
                    logger.info(
                        "expand trim restart book=%s chapter=%s round=%s before=%s after=%s",
                        book_name, cid, rnd, count_hanzi(result), count_hanzi(trimmed),
                    )
                    yield StreamEvent(
                        kind="status",
                        message="检测到写完后又从头写起，已去掉后面重复的部分",
                    )
                    result = trimmed
        else:
            chunks = split_chapter_chunks(best)
            yield StreamEvent(
                kind="status",
                message=f"本章现有 {already} 字，将按正文分成 {len(chunks)} 段分别扩写，每段都保留原句并补足细节，目标约 {goal} 字",
            )
            logger.info(
                "expand chunked book=%s chapter=%s round=%s hanzi=%s target=%s chunks=%s",
                book_name, cid, rnd, already, goal, len(chunks),
            )
            expanded_parts: list[str] = []
            for index, chunk in enumerate(chunks, 1):
                prev_tail = (expanded_parts[-1] if expanded_parts else "")[-80:]
                next_head = chunks[index][:80] if index < len(chunks) else ""
                chunk_target = expand_chunk_target(chunk, best, goal)
                chunk_messages = _expand_chunk_messages(
                    chapter_num=chapter_num,
                    index=index,
                    total=len(chunks),
                    current_section=current_section,
                    compact_outline=compact_outline,
                    idea_for_prompt=idea_for_prompt,
                    excerpts=excerpts,
                    chunk=chunk,
                    prev_tail=prev_tail,
                    next_head=next_head,
                    chunk_target=chunk_target,
                    longform=longform,
                )
                chunk_prompt = system_and_user(chunk_messages)
                room = available_output_tokens(chunk_prompt)
                needed = expand_needed_tokens(chunk, chunk_target)
                use_thinking = bool(enable_thinking and room >= needed + THINKING_RESERVE_TOKENS)
                max_tokens = plan_max_tokens(chunk_prompt, target_chars=chunk_target, enable_thinking=use_thinking)
                if max_tokens < 512:
                    failed = StreamEvent(kind="error", message="上下文仍然不够扩写本段，请缩短文风样本或关掉部分参考后再试")
                    break
                yield StreamEvent(
                    kind="status",
                    message=f"正在扩写第 {index}/{len(chunks)} 段（保留原句，本段目标约 {chunk_target} 字）…",
                )
                logger.info(
                    "expand chunk book=%s chapter=%s r%s %s/%s hanzi=%s target=%s room=%s max_tokens=%s thinking=%s",
                    book_name, cid, rnd, index, len(chunks), count_hanzi(chunk), chunk_target, room, max_tokens, use_thinking,
                )
                collected = []
                async for event in stream_chat_with_retry(
                    chunk_messages,
                    llm_url=llm_url,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    enable_thinking=use_thinking,
                    cancel_event=cancel_event,
                ):
                    if event.kind == "text":
                        collected.append(event.text)
                        yield event
                    elif event.kind == "status":
                        yield event
                    elif event.kind == "cancelled":
                        cancelled = True
                        yield event
                        break
                    elif event.kind == "error":
                        failed = event
                        break
                    elif event.kind == "done":
                        if event.text:
                            collected = [event.text]
                if cancelled or failed:
                    break
                piece = "".join(collected).strip()
                trimmed_piece = trim_expand_restart(chunk, piece)
                if trimmed_piece != piece:
                    logger.info(
                        "expand trim restart chunk book=%s chapter=%s r%s %s/%s before=%s after=%s",
                        book_name, cid, rnd, index, len(chunks),
                        count_hanzi(piece), count_hanzi(trimmed_piece),
                    )
                    yield StreamEvent(
                        kind="status",
                        message=f"第 {index} 段写完后又从头写起，已去掉后面重复的部分",
                    )
                    piece = trimmed_piece
                if not expand_is_valid(chunk, piece):
                    logger.warning(
                        "expand chunk %s/%s invalid (hanzi %s vs %s, cover %.2f), keeping original",
                        index, len(chunks), count_hanzi(piece), count_hanzi(chunk),
                        expand_original_coverage(chunk, piece),
                    )
                    piece = chunk
                    yield StreamEvent(
                        kind="status",
                        message=f"第 {index} 段没有真正扩写（须保留原句并明显增加字数），已保留该段原文",
                    )
                expanded_parts.append(piece)
            if not cancelled and not failed:
                result = join_chapter_chunks(expanded_parts).strip()

        if cancelled:
            return
        if failed:
            if did_backup:
                yield StreamEvent(
                    kind="status",
                    message=f"后续扩写出错，已保留已经扩写到 {count_hanzi(best)} 字的正文",
                )
                done = StreamEvent(kind="done", result=ChatResult(content=best), text=best)
                done.extra = {"hanzi": count_hanzi(best), "chapter": cid, "saved": True}
                yield done
            else:
                yield failed
            return

        if not expand_is_valid(best, result):
            cover = expand_original_coverage(best, result)
            new_n, old_n = count_hanzi(result), count_hanzi(best)
            if expand_result_too_short(best, result):
                reason = "没有明显增加字数"
            else:
                reason = "没有保留原文，像是重写而不是扩写"
            yield StreamEvent(
                kind="status",
                message=(
                    f"第 {rnd} 轮{reason}（{old_n}→{new_n} 字，原文保留 {cover:.0%}），"
                    "已留下更长且保留原文的一版"
                ),
            )
            logger.info(
                "expand round rejected book=%s chapter=%s round=%s old=%s new=%s cover=%.2f",
                book_name, cid, rnd, old_n, new_n, cover,
            )
            stall += 1
            if stall >= AUTO_EXPAND_MAX_STALLS:
                break
            continue

        if not did_backup:
            books.backup_chapter(book_name, cid)
            did_backup = True
        if not result.endswith("\n"):
            result = result + "\n"
        best = result
        books.write_chapter(book_name, cid, best)
        logger.info(
            "expand accepted book=%s chapter=%s round=%s hanzi=%s target=%s cover=%.2f",
            book_name, cid, rnd, count_hanzi(best), user_target, expand_original_coverage(original, best),
        )
        stall = 0
        if not should_auto_continue(best, user_target):
            break
        yield StreamEvent(
            kind="status",
            message=f"扩写后本章现有 {count_hanzi(best)} 字，尚未达到目标 {user_target} 字，继续按原文扩写…",
        )

    if not did_backup:
        yield StreamEvent(
            kind="error",
            message="扩写结果没有真正扩写：必须保留原文句子，并且明显增加字数。已保留原文，请再试一次",
        )
        return
    if should_auto_continue(best, user_target):
        yield StreamEvent(
            kind="status",
            message=(
                f"已扩写到 {count_hanzi(best)} 字，尚未完全达到目标 {user_target} 字。"
                "可以再点「扩写本章」"
            ),
        )
    history.record_chapter_change(book_name, cid, original, best, "expand")
    done = StreamEvent(kind="done", result=ChatResult(content=best), text=best)
    done.extra = {"hanzi": count_hanzi(best), "chapter": cid, "saved": True}
    yield done


def _expand_chunk_messages(
    *,
    chapter_num: int,
    index: int,
    total: int,
    current_section: str,
    compact_outline: str,
    idea_for_prompt: str,
    excerpts: list[dict],
    chunk: str,
    prev_tail: str,
    next_head: str,
    chunk_target: int,
    longform: str = "",
) -> list[dict]:
    already = count_hanzi(chunk)
    user = (
        f"请对「第{chapter_num}章」第 {index}/{total} 段做扩写，不是重写，也不是只在段末另写一段。"
        f"本段原文有 {already} 字，扩写后本段汉字数量应大约为 {chunk_target} 字（可在正负 15% 范围内），且必须明显多于原文。"
        "原文句子必须保留，只在原句内部或句子之间插入描写、心理、对话、动作和环境细节。\n"
        "要求：\n"
        "1. 只输出这一段扩写后的完整正文，不要输出其他段，不要写说明或思考过程。\n"
        "2. 这是扩写不是重写：本段原句要保留在输出里，只增加，不替换、不删减、不压缩、不概括。\n"
        "3. 不要只在本段结尾追加一段无关的新情节来凑字数，也不要把其他段或其他章节的内容写进来。\n"
        "4. 写完本段原有结尾后立即停止，不要从本段开头再写一遍来凑字数，也不要把本段内容重复输出。\n"
        "5. 扩写后仍须符合「本章大纲」。文风样本只供参考语气。所有人物年龄 18 岁或以上。\n\n"
        f"【本章大纲（必须仍符合）】\n{current_section}\n\n"
        f"{longform or ''}"
        f"【本段位置】第 {index} 段，共 {total} 段。本段目标约 {chunk_target} 字。\n\n"
        f"【上一段结尾（只供衔接，不要重复输出）】\n{prev_tail or '（本章开头）'}\n\n"
        f"【下一段开头（只供衔接，不要写入本段）】\n{next_head or '（本章结尾）'}\n\n"
        f"【本段原文（必须全部保留，只在此基础上插入扩写，并完整输出本段）】\n{chunk}\n\n"
        f"【文风参考（仅语气与句式，禁止采用其中情节）】\n{_style_block(excerpts)}\n"
    )
    return [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]



async def _finish_revise(
    *,
    book_name: str,
    cid: str,
    current: str,
    stream: AsyncIterator[StreamEvent],
    min_ratio: float = REVISE_MIN_RATIO,
    empty_message: str = "模型没有返回修改后的正文，已保留原文",
    short_template: str | None = None,
) -> AsyncIterator[StreamEvent]:
    collected: list[str] = []
    async for event in stream:
        if event.kind == "text":
            collected.append(event.text)
            yield event
            continue
        if event.kind in {"status", "error", "cancelled"}:
            yield event
            if event.kind in {"error", "cancelled"}:
                return
            continue
        if event.kind != "done":
            yield event
            continue
        text = (event.text or (event.result.content if event.result else "") or "".join(collected)).strip()
        if not text:
            yield StreamEvent(kind="error", message=empty_message)
            return
        text = text + "\n"
        books.backup_chapter(book_name, cid)
        history.record_chapter_change(book_name, cid, current, text, "revise")
        books.write_chapter(book_name, cid, text)
        event.text = text
        event.extra = _revise_done_extra(cid, current, text, min_ratio=min_ratio)
        if short_template and event.extra.get("short_warning"):
            event.extra["message"] = short_template.format(new=count_hanzi(text), old=count_hanzi(current))
        if event.result:
            event.result.content = text
        if event.extra.get("short_warning"):
            yield StreamEvent(kind="status", message=event.extra["message"])
        yield event


def system_and_user(messages: list[dict]) -> str:
    return "\n".join(m.get("content") or "" for m in messages)


def _chapter_outline_context(book_name, cid, outline_text, preamble, chapters, chapter_num):
    """Resolve this chapter's outline section and a path-only compact outline."""
    from app.tree import (
        display_num_for_chapter,
        outline_section_for_chapter,
        path_outline_chapters,
    )

    display_num = display_num_for_chapter(book_name, cid, chapter_num)
    current_section = outline_section_for_chapter(book_name, cid, outline_text)
    if not current_section:
        current_section = books.outline_chapter_section(outline_text, display_num)
    path_chapters = path_outline_chapters(book_name, cid, chapters) if chapters else None
    outline_chapters = path_chapters if path_chapters is not None else chapters
    if outline_chapters:
        compact_outline = _compact_outline(preamble, outline_chapters, display_num)
    else:
        compact_outline = _clip_text(outline_text, 3500) if not chapters else _compact_outline(preamble, chapters, display_num)
    return current_section, compact_outline, display_num


def _previous_chapter_context(book_name: str, chapter_id: str) -> str:
    current_num = int(chapter_id)
    candidates: list[str] = []
    try:
        from app.tree import ancestor_chapter_ids

        ancestors = ancestor_chapter_ids(book_name, chapter_id)
    except Exception:
        ancestors = None
    if ancestors is not None:
        candidates = list(ancestors)
    else:
        candidates = [f"{num:02d}" if len(str(num)) <= 2 else str(num) for num in range(current_num - 1, 0, -1)]

    parts = []
    # Include up to two preceding chapters, preferring the immediately previous one in full.
    budget_chars = 3500
    for cid in candidates:
        path = books.chapter_path(book_name, cid)
        if not path.exists():
            continue
        text = chapter_text_for_llm(path.read_text(encoding="utf-8-sig"))
        if not text:
            continue
        try:
            num = int(cid)
        except ValueError:
            num = cid
        if len(text) > budget_chars:
            text = "……\n" + text[-budget_chars:]
        parts.append(f"【第{num}章已写正文（以作者修订稿为准，必须承接）】\n{text}")
        budget_chars = 1500
        if len(parts) >= 2:
            break
    parts.reverse()
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n\n"


async def generate_next_outline_chapter(
    book_name: str,
    *,
    idea: str | None = None,
    outline: str | None = None,
    from_chapter: str | None = None,
    style_ids: list[str] | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    llm_url: str | None = None,
    cancel_event=None,
    pacing_note: str | None = None,
    allow_ending_marker: bool = False,
) -> AsyncIterator[StreamEvent]:
    """Append only the next chapter's outline on the current plot branch."""
    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    if idea is not None:
        books.write_idea(book_name, idea)
    if outline is not None and outline.strip():
        books.write_outline(book_name, outline.strip() + "\n")

    try:
        spec = longform.next_outline_spec(book_name, from_chapter)
    except BookError as exc:
        yield StreamEvent(kind="error", message=str(exc))
        return

    idea_text = (spec.get("idea") or books.read_idea(book_name)).strip()
    if not idea_text and not (spec.get("path_outline") or "").strip():
        yield StreamEvent(kind="error", message="请先写下思路，再按章写大纲")
        return

    ids = style_ids if style_ids is not None else settings.get("selected_styles") or []
    temperature = settings["temperature"] if temperature is None else temperature
    enable_thinking = settings["enable_thinking"] if enable_thinking is None else enable_thinking
    llm_url = llm_url or settings["llm_url"]
    settings_block = longform.setting_prompt_blocks(book_name, spec.get("parent_id") or from_chapter)
    existing = spec.get("outline") or ""
    path_outline = spec.get("path_outline") or existing
    target_chars = 800 if not spec["is_first"] else 900
    output_tokens = int(target_chars * 2.0) + 512 + (THINKING_RESERVE_TOKENS if enable_thinking else 0)
    excerpts = _load_styles(
        book_name,
        ids,
        prompt_chars_guess=len(idea_text) + len(path_outline) + len(settings_block) + 800,
        output_tokens=output_tokens,
        max_total_chars=800,
    )
    heading = spec["heading_prefix"]
    branch_note = f"本章属于「{spec['branch']}」这条线，不要写其他结局的情节。" if spec.get("branch") else "只沿着当前这条线往下写，不要另开结局分支。"
    if spec["is_first"]:
        user = (
            "请根据「思路」为这部中文长篇小说只写第一章的大纲，不要一次写完全书。\n"
            "要求：\n"
            "1. 可以先写书名和「## 大纲」标题，然后只写「第一章」这一节。\n"
            "2. 第一章写清主要事件、冲突与章末钩子。人物、情节保持原创；所有人物年龄 18 岁或以上。\n"
            "3. 若提供了世界观或人物设定，须与之相符，不要改写这些设定本身。\n"
            "4. 不要写第二章及之后的章节。只输出 Markdown，不要前言后记。\n\n"
            "输出格式：\n"
            "# 《书名》\n\n"
            "## 大纲\n\n"
            f"{heading} 标题\n\n"
            "……\n\n"
            f"【思路】\n{idea_text or '（无）'}\n\n"
            f"{settings_block}"
            f"【文风参考】\n{_style_block(excerpts)}\n"
        )
    else:
        user = (
            f"请只写「下一章」的大纲，追加在已有大纲之后，不要重写或改动已经写好的章节。\n"
            "要求：\n"
            f"1. 只输出下一章这一节，标题以「{heading}」开头，后接本章标题。不要输出书名、故事线或其他章节。\n"
            f"2. {branch_note}\n"
            "3. 承接已经写好的各章，写清本章主要事件、冲突与章末钩子。不要重复上一章已发生的核心事件。\n"
            "4. 人物、情节保持原创；所有人物年龄 18 岁或以上。若提供了世界观或人物设定，须与之相符，不要改写这些设定本身。\n"
            "5. 只输出这一章的 Markdown 小节，不要前言、说明或完整大纲。\n\n"
            f"【思路】\n{idea_text or '（无）'}\n\n"
            f"{settings_block}"
            f"【当前这条线上已有的大纲（不得改写，只供承接）】\n{path_outline or '（无）'}\n\n"
            f"【文风参考】\n{_style_block(excerpts)}\n"
        )
    if allow_ending_marker or (pacing_note or "").strip():
        note = (pacing_note or "").strip()
        extra_rules = ""
        if allow_ending_marker:
            extra_rules = (
                (f"{note}\n" if note else "")
                + f"若本章已是当前这条线的收尾，请在本节末单独一行写上 {longform.ENDING_MARKER}。"
                "若本章还不是收尾，不要写该标记，章末留下钩子。不要一次写出后面多章，也不要另开新的结局分支。\n"
            )
        else:
            extra_rules = note + "\n"
        user = user.rstrip() + "\n\n【收尾约定】\n" + extra_rules
    messages = [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]
    max_tokens = plan_max_tokens(system_and_user(messages), target_chars=target_chars, enable_thinking=enable_thinking)
    logger.info(
        "outline-next start book=%s first=%s num=%s branch=%s thinking=%s",
        book_name,
        spec["is_first"],
        spec["num"],
        spec.get("branch"),
        enable_thinking,
    )
    async for event in stream_chat_with_retry(
        messages,
        llm_url=llm_url,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "done" and event.result and event.result.content.strip():
            generated = event.result.content.strip()
            section = longform.isolate_new_outline_section(generated, existing)
            if not section.strip():
                yield StreamEvent(kind="error", message="未能写出独立的下一章大纲，已保留原大纲")
                return
            if allow_ending_marker and longform.outline_section_is_ending(section):
                section = longform.ensure_ending_marker(section)
            if existing.strip():
                books.backup_outline(book_name)
            merged = longform.append_outline_section(existing, section)
            # If the model omitted the required heading, prefix it.
            if existing.strip() and not books.CHAPTER_HEADING_RE.search(section):
                section = f"{heading} 待拟标题\n\n{section.strip()}\n"
                merged = longform.append_outline_section(existing, section)
            final_outline = merged if merged.endswith("\n") else merged + "\n"
            history.record_outline_change(book_name, existing, final_outline, "outline_next")
            books.write_outline(book_name, final_outline)
            settings["selected_styles"] = ids
            books.save_settings(book_name, settings)
            full = books.read_outline(book_name)
            event.text = full
            if event.result:
                event.result.content = full
            event.extra = {
                "hanzi": count_hanzi(full),
                "saved": "大纲.md",
                "appended": True,
                "section": section,
                "outline": full,
                "num": spec.get("num"),
                "is_ending": longform.outline_section_is_ending(section),
            }
        yield event


async def generate_outline_until_end(
    book_name: str,
    *,
    idea: str | None = None,
    outline: str | None = None,
    from_chapter: str | None = None,
    target_chapters: int | None = None,
    target_book_chars: int | None = None,
    style_ids: list[str] | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    llm_url: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    """Keep appending the next outline chapter on the current branch until it ends or hits a cap."""
    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    if idea is not None:
        books.write_idea(book_name, idea)
    if outline is not None and outline.strip():
        books.write_outline(book_name, outline.strip() + "\n")

    from app.tree import get_book_tree

    tree = get_book_tree(book_name)
    if tree.get("has_forks") and not from_chapter:
        yield StreamEvent(kind="error", message="当前大纲已有分叉，请先在情节脉络中点选要继续的那条线")
        return

    chapter_target = settings.get("chapter_target_chars") or 2000
    pending_outline = outline
    written_new = 0
    last_outline = books.read_outline(book_name)

    while True:
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            yield StreamEvent(
                kind="cancelled",
                message="已停止，已写好的大纲都保留了",
                text=last_outline,
                extra={"outline": last_outline, "written_new": written_new, "hanzi": count_hanzi(last_outline)},
            )
            return
        try:
            spec = longform.next_outline_spec(book_name, from_chapter)
        except BookError as exc:
            yield StreamEvent(kind="error", message=str(exc))
            return

        current_num = max(0, int(spec.get("num") or 1) - 1)
        plan = longform.outline_until_plan(
            current_num,
            target_chapters=target_chapters,
            target_book_chars=target_book_chars,
            chapter_target_chars=chapter_target,
            written_new=written_new,
        )
        if plan["already_complete"]:
            full = books.read_outline(book_name)
            yield StreamEvent(
                kind="done",
                text=full,
                extra={
                    "reason": "already_complete",
                    "message": "当前这条线已经达到目标章数，没有再往下写",
                    "outline": full,
                    "hanzi": count_hanzi(full),
                    "written_new": written_new,
                },
            )
            return
        if plan["safety_hit"]:
            full = books.read_outline(book_name)
            yield StreamEvent(
                kind="done",
                text=full,
                extra={
                    "reason": "safety",
                    "message": f"已连续写了{written_new}章，先停在这里，以免一直写下去。如果还要继续，再点一次即可",
                    "outline": full,
                    "hanzi": count_hanzi(full),
                    "written_new": written_new,
                },
            )
            return

        yield StreamEvent(kind="status", message=f"正在写第{plan['next_num']}章大纲…")
        chapter_done = None
        async for event in generate_next_outline_chapter(
            book_name,
            idea=None,
            outline=pending_outline,
            from_chapter=from_chapter,
            style_ids=style_ids,
            temperature=temperature,
            enable_thinking=enable_thinking,
            llm_url=llm_url,
            cancel_event=cancel_event,
            pacing_note=plan["pacing_note"],
            allow_ending_marker=True,
        ):
            pending_outline = None
            if event.kind == "done":
                chapter_done = event
            elif event.kind == "error":
                yield event
                return
            elif event.kind == "cancelled":
                kept = books.read_outline(book_name) or last_outline
                yield StreamEvent(
                    kind="cancelled",
                    message="已停止，已写好的大纲都保留了",
                    text=kept,
                    extra={"outline": kept, "written_new": written_new, "hanzi": count_hanzi(kept)},
                )
                return
            else:
                yield event

        if chapter_done is None:
            yield StreamEvent(kind="error", message="写下一章大纲时没有得到结果，已写好的大纲都保留了")
            return

        written_new += 1
        last_outline = chapter_done.text or books.read_outline(book_name)
        section = (chapter_done.extra or {}).get("section") or ""
        is_ending = longform.outline_section_is_ending(section)
        reached_cap = bool(plan["limit_num"] and plan["next_num"] >= plan["limit_num"])
        yield StreamEvent(
            kind="chapter",
            text=last_outline,
            extra={
                "outline": last_outline,
                "section": section,
                "num": plan["next_num"],
                "hanzi": count_hanzi(last_outline),
                "is_ending": is_ending,
                "written_new": written_new,
            },
        )
        if is_ending:
            yield StreamEvent(
                kind="done",
                text=last_outline,
                extra={
                    "reason": "ended",
                    "message": f"这条线的大纲已经写到收尾，共追加了{written_new}章",
                    "outline": last_outline,
                    "hanzi": count_hanzi(last_outline),
                    "written_new": written_new,
                },
            )
            return
        if reached_cap:
            yield StreamEvent(
                kind="done",
                text=last_outline,
                extra={
                    "reason": "cap",
                    "message": f"已写到目标上限，共追加了{written_new}章",
                    "outline": last_outline,
                    "hanzi": count_hanzi(last_outline),
                    "written_new": written_new,
                },
            )
            return



def preceding_bodies_for_synopsis(book_name: str, plan: dict) -> list[dict]:
    """Already-written chapters on this path that should be summarised before the next empty one.

    Any chapter missing a synopsis is included, in path order. The immediate
    predecessor is always included, so an edited previous chapter is reflected.
    """
    from app import synopsis as synopsis_store

    next_id = ((plan or {}).get("next") or {}).get("id")
    preceding: list[dict] = []
    for item in (plan or {}).get("items") or []:
        if next_id and item.get("id") == next_id:
            break
        if item.get("has_body"):
            preceding.append(item)
    if not preceding:
        return []
    needed_ids = {
        item["id"]
        for item in preceding
        if not (synopsis_store.read_synopsis(book_name, item["id"]) or "").strip()
    }
    needed_ids.add(preceding[-1]["id"])
    return [item for item in preceding if item["id"] in needed_ids]


async def _refresh_synopsis_for_until(
    *,
    book_name: str,
    cid: str,
    num: int,
    chapter_text: str | None = None,
    temperature: float | None = None,
    llm_url: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    """Update the path synopsis without streaming its draft into the chapter body."""
    yield StreamEvent(
        kind="status",
        message=f"正在根据第{num}章正文更新前情摘要…",
        extra={"chapter": cid, "num": num},
    )
    async for event in refresh_path_synopsis(
        book_name,
        cid,
        chapter_text=chapter_text,
        temperature=temperature,
        enable_thinking=False,
        llm_url=llm_url,
        cancel_event=cancel_event,
    ):
        if event.kind == "cancelled":
            yield event
            return
        if event.kind == "error":
            yield StreamEvent(
                kind="status",
                message=f"第{num}章摘要没有更新成功，仍继续往后写",
                extra={"chapter": cid, "num": num},
            )
            return
        if event.kind == "done":
            yield StreamEvent(
                kind="status",
                message=f"第{num}章前情摘要已更新",
                extra={"chapter": cid, "num": num},
            )
            return
        if event.kind == "status":
            extra = dict(event.extra or {})
            extra.update({"chapter": cid, "num": num})
            event.extra = extra
            yield event


async def generate_chapters_until_end(
    book_name: str,
    *,
    from_chapter: str | None = None,
    target_chapters: int | None = None,
    target_book_chars: int | None = None,
    style_ids: list[str] | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    target_chars: int | None = None,
    llm_url: str | None = None,
    outline: str | None = None,
    cancel_event=None,
    on_chapter=None,
) -> AsyncIterator[StreamEvent]:
    """Write empty chapter bodies along the current branch until the line or a cap ends."""
    books.ensure_book(book_name)
    if outline is not None and outline.strip():
        books.write_outline(book_name, outline.strip() + "\n")
    if not books.read_outline(book_name).strip():
        yield StreamEvent(kind="error", message="请先确认大纲，再分章写作")
        return

    from app.tree import get_book_tree

    tree = get_book_tree(book_name)
    if not (tree.get("nodes") or []):
        yield StreamEvent(kind="error", message="请先确认大纲，再分章写作")
        return
    if tree.get("has_forks") and not from_chapter:
        yield StreamEvent(kind="error", message="当前大纲已有分叉，请先在情节脉络中点选要继续的那条线")
        return

    written_new = 0
    last_chapter = None
    last_text = ""
    prior_synopsis_ready = False

    def _done(reason: str, message: str, plan: dict) -> StreamEvent:
        return StreamEvent(
            kind="done",
            text=last_text,
            extra={
                "reason": reason,
                "message": message,
                "written_new": written_new,
                "skipped": plan.get("skipped") or 0,
                "path_hanzi": plan.get("path_hanzi") or 0,
                "chapter": last_chapter,
                "stopped_at_fork": bool(plan.get("stopped_at_fork")),
            },
        )

    while True:
        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            yield StreamEvent(
                kind="cancelled",
                message="已停止，已写好的正文都保留了",
                text=last_text,
                extra={"written_new": written_new, "chapter": last_chapter},
            )
            return
        try:
            plan = longform.body_until_plan(
                book_name,
                from_chapter=from_chapter,
                target_chapters=target_chapters,
                target_book_chars=target_book_chars,
                chapter_target_chars=target_chars or (books.load_settings(book_name).get("chapter_target_chars") or 2000),
                written_new=written_new,
            )
        except books.BookError as exc:
            yield StreamEvent(kind="error", message=str(exc))
            return
        if plan.get("from_chapter"):
            from_chapter = plan["from_chapter"]

        if not prior_synopsis_ready:
            prior_synopsis_ready = True
            for item in preceding_bodies_for_synopsis(book_name, plan):
                if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                    yield StreamEvent(
                        kind="cancelled",
                        message="已停止，已写好的正文都保留了",
                        text=last_text,
                        extra={"written_new": written_new, "chapter": last_chapter},
                    )
                    return
                async for event in _refresh_synopsis_for_until(
                    book_name=book_name,
                    cid=item["id"],
                    num=item["num"],
                    temperature=temperature,
                    llm_url=llm_url,
                    cancel_event=cancel_event,
                ):
                    if event.kind == "cancelled":
                        yield StreamEvent(
                            kind="cancelled",
                            message="已停止，已写好的正文都保留了",
                            text=last_text,
                            extra={"written_new": written_new, "chapter": last_chapter or item["id"]},
                        )
                        return
                    yield event

        if plan["safety_hit"]:
            yield _done(
                "safety",
                f"已连续写了{written_new}章，先停在这里，以免一直写下去。如果还要继续，再点一次即可",
                plan,
            )
            return
        if plan["already_complete"] or plan["next"] is None:
            reason = plan.get("complete_reason") or "all_written"
            fork_note = "其他结局请点选对应的线再跑一次。" if plan.get("stopped_at_fork") else ""
            if written_new == 0 and reason == "all_written":
                message = "当前这条线上的章节都已有正文，没有再写。" + fork_note
            elif written_new == 0 and reason == "chars":
                message = "当前这条线的字数已达到目标，没有再写。" + fork_note
            elif written_new == 0 and reason == "cap":
                message = "当前这条线已经达到目标章数，没有再写。" + fork_note
            elif written_new == 0 and reason == "empty_path":
                message = "当前这条线上还没有可写的章节大纲，请先把大纲写完。"
            elif reason == "chars":
                message = f"已达到全书大概字数，共写了{written_new}章。" + fork_note
            elif reason == "cap":
                message = f"已写到目标章数，共写了{written_new}章。" + fork_note
            elif plan.get("stopped_at_fork"):
                message = f"已写到分叉点，共写了{written_new}章。其他结局请点选对应的线再跑一次。"
            else:
                message = f"这条线的正文已经按大纲写完，共写了{written_new}章。"
            yield _done(reason, message.strip(), plan)
            return

        item = plan["next"]
        cid = item["id"]
        num = item["num"]
        title = item.get("title") or f"第{num}章"
        if longform.chapter_has_body(book_name, cid):
            continue
        if callable(on_chapter):
            try:
                on_chapter(cid)
            except Exception:
                logger.exception("failed to attach chapter job %s", cid)
        yield StreamEvent(
            kind="status",
            message=f"正在写第{num}章正文…",
            extra={"chapter": cid, "num": num, "title": title},
        )
        chapter_done = None
        async for event in generate_chapter(
            book_name,
            cid,
            mode="generate",
            style_ids=style_ids,
            temperature=temperature,
            enable_thinking=enable_thinking,
            target_chars=target_chars,
            llm_url=llm_url,
            outline=None,
            chapter_text=None,
            cancel_event=cancel_event,
        ):
            if event.kind == "done":
                chapter_done = event
            elif event.kind == "error":
                yield event
                return
            elif event.kind == "cancelled":
                kept = last_text
                yield StreamEvent(
                    kind="cancelled",
                    message="已停止，已写好的正文都保留了",
                    text=kept,
                    extra={"written_new": written_new, "chapter": last_chapter or cid},
                )
                return
            else:
                if event.kind in {"text", "status"} and not (event.extra or {}).get("chapter"):
                    extra = dict(event.extra or {})
                    extra.update({"chapter": cid, "num": num, "title": title})
                    event.extra = extra
                yield event

        if chapter_done is None or not (chapter_done.text or "").strip():
            yield StreamEvent(kind="error", message=f"写第{num}章时没有得到结果，已写好的正文都保留了")
            return

        written_new += 1
        last_chapter = cid
        last_text = chapter_done.text or ""
        extra = dict(chapter_done.extra or {})
        extra.update(
            {
                "chapter": cid,
                "num": num,
                "title": title,
                "hanzi": extra.get("hanzi") or count_visible_hanzi(last_text),
                "written_new": written_new,
                "skipped": plan.get("skipped") or 0,
            }
        )
        yield StreamEvent(kind="chapter", text=last_text, extra=extra)
        async for event in _refresh_synopsis_for_until(
            book_name=book_name,
            cid=cid,
            num=num,
            chapter_text=last_text,
            temperature=temperature,
            llm_url=llm_url,
            cancel_event=cancel_event,
        ):
            if event.kind == "cancelled":
                yield StreamEvent(
                    kind="cancelled",
                    message="已停止，已写好的正文都保留了",
                    text=last_text,
                    extra={"written_new": written_new, "chapter": last_chapter or cid},
                )
                return
            yield event


async def generate_this_outline_chapter(
    book_name: str,
    *,
    chapter_id: str | None = None,
    notes: str | None = None,
    idea: str | None = None,
    outline: str | None = None,
    style_ids: list[str] | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    llm_url: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    """Fill or rewrite only the selected chapter's outline section on the current path."""
    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    if idea is not None:
        books.write_idea(book_name, idea)
    if outline is not None and outline.strip():
        books.write_outline(book_name, outline.strip() + "\n")

    try:
        spec = longform.this_outline_spec(book_name, chapter_id, notes or "")
    except BookError as exc:
        yield StreamEvent(kind="error", message=str(exc))
        return

    idea_text = (spec.get("idea") or books.read_idea(book_name)).strip()
    notes_text = (spec.get("notes") or "").strip()
    if not idea_text and not notes_text:
        yield StreamEvent(kind="error", message="请先写下思路，再写本章大纲")
        return

    ids = style_ids if style_ids is not None else settings.get("selected_styles") or []
    temperature = settings["temperature"] if temperature is None else temperature
    enable_thinking = settings["enable_thinking"] if enable_thinking is None else enable_thinking
    llm_url = llm_url or settings["llm_url"]
    settings_block = longform.setting_prompt_blocks(book_name, chapter_id)
    existing = spec.get("outline") or ""
    path_outline = spec.get("path_outline") or ""
    old_section = spec.get("section") or ""
    heading = spec["heading"]
    branch = spec.get("branch")
    direction = (spec.get("direction") or "").strip()
    branch_note = (
        f"本章属于「{branch}」这条线。只写这一条线，不要写其他结局的情节。"
        if branch
        else "只沿着当前这条线写，不要另开结局分支。"
    )
    direction_block = f"【本章走向】\n{direction}\n\n" if direction else ""
    notes_block = f"【本章思路】\n{notes_text}\n\n" if notes_text else ""
    target_chars = 800
    output_tokens = int(target_chars * 2.0) + 512 + (THINKING_RESERVE_TOKENS if enable_thinking else 0)
    excerpts = _load_styles(
        book_name,
        ids,
        prompt_chars_guess=len(idea_text) + len(path_outline) + len(settings_block) + len(notes_text) + 800,
        output_tokens=output_tokens,
        max_total_chars=800,
    )
    user = (
        f"请只写「{heading}」这一章的大纲，不要改动其他章节，也不要另写新的一章。\n"
        "要求：\n"
        f"1. 只输出这一章这一节，标题沿用「{heading}」。不要输出书名、故事线或其他章节。\n"
        f"2. {branch_note}\n"
        "3. 结合思路、世界观、人物设定、分叉点之前已有的大纲，以及本章的走向（若有），写清本章主要事件、冲突与章末钩子。\n"
        "4. 若这一节目前是「（待补充）」或内容很短，请补写完整；若已有草稿，在其基础上充实，保留仍成立的部分。\n"
        "5. 人物、情节保持原创；所有人物年龄 18 岁或以上。若提供了世界观或人物设定，须与之相符，不要改写这些设定本身。不要前言、说明或完整大纲。\n\n"
        f"【思路】\n{idea_text or '（无）'}\n\n"
        f"{settings_block}"
        f"{direction_block}"
        f"{notes_block}"
        f"【分叉点之前、当前这条线上已有的大纲（不得改写，只供承接）】\n{path_outline or '（无）'}\n\n"
        f"【本章现有大纲（在此基础上写，不要另起一章）】\n{old_section or '（待补充）'}\n\n"
        f"【文风参考】\n{_style_block(excerpts)}\n"
    )
    messages = [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]
    max_tokens = plan_max_tokens(system_and_user(messages), target_chars=target_chars, enable_thinking=enable_thinking)
    logger.info(
        "outline-section start book=%s chapter=%s heading=%s branch=%s thinking=%s",
        book_name,
        chapter_id,
        heading,
        branch,
        enable_thinking,
    )
    async for event in stream_chat_with_retry(
        messages,
        llm_url=llm_url,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "done" and event.result and event.result.content.strip():
            generated = event.result.content.strip()
            section = longform.isolate_new_outline_section(generated, "")
            if not section.strip():
                section = generated.strip() + "\n"
            if not books.CHAPTER_HEADING_RE.search(section):
                section = f"{heading}\n\n{section.strip()}\n"
            section = longform.keep_branch_marker(old_section, section)
            if not section.strip():
                yield StreamEvent(kind="error", message="未能写出本章大纲，已保留原大纲")
                return
            if existing.strip():
                books.backup_outline(book_name)
            if old_section.strip():
                merged = longform.replace_outline_section(existing, old_section, section)
            else:
                # empty placeholder: try to insert after path, otherwise append
                merged = longform.replace_outline_section(existing, old_section, section)
            final_outline = merged if merged.endswith("\n") else merged + "\n"
            history.record_outline_change(book_name, existing, final_outline, "outline_section")
            books.write_outline(book_name, final_outline)
            settings["selected_styles"] = ids
            books.save_settings(book_name, settings)
            full = books.read_outline(book_name)
            event.text = full
            if event.result:
                event.result.content = full
            event.extra = {
                "hanzi": count_hanzi(full),
                "saved": "大纲.md",
                "appended": False,
                "section": section,
                "outline": full,
                "chapter_id": spec.get("chapter_id"),
            }
        yield event


async def generate_world_draft(
    book_name: str,
    *,
    idea: str | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    llm_url: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    if idea is not None:
        books.write_idea(book_name, idea)
    idea_text = (idea if idea is not None else books.read_idea(book_name)).strip()
    if not idea_text:
        yield StreamEvent(kind="error", message="请先写下思路，再起草世界观")
        return
    current = books.read_world(book_name).strip()
    temperature = settings["temperature"] if temperature is None else temperature
    enable_thinking = settings["enable_thinking"] if enable_thinking is None else enable_thinking
    llm_url = llm_url or settings["llm_url"]
    target_chars = 2000
    accumulated = current
    started_with = current
    auto_round = 0
    stall_rounds = 0
    final_event = None

    while True:
        if auto_round == 0:
            user = (
                "请根据下面的「思路」，为这部原创小说起草一份完整的世界观文档。\n"
                "要求：\n"
                "1. 只写设定：时代背景、世界规则、重要地点、势力或社会关系、需要前后一致的禁忌与前提。\n"
                "2. 不要写章节大纲，也不要写成故事梗概。文风简洁，便于之后写大纲和正文时对照。\n"
                "3. 人物只在与世界设定直接相关时略提，不要展开人物小传。\n"
                "4. 所有人物年龄 18 岁或以上。只输出世界观正文，不要前言后记。\n"
                f"5. 请把这份设定写完整，大约 {target_chars} 字，在写完之前不要提前收束。\n"
            )
            if current:
                user += (
                    "6. 已有一份世界观草稿时，在其基础上整理补全，保留仍成立的设定，不要无故推翻。\n\n"
                    f"【当前世界观】\n{current}\n\n"
                )
            else:
                user += "\n"
            user += f"【思路】\n{idea_text}\n"
        else:
            user = (
                "下面这份世界观草稿还没写完整。请从停下的地方继续补写，把它写完整。\n"
                "要求：\n"
                "1. 不要重复已经写过的段落，不要另起一份，也不要重写开头。\n"
                "2. 只补还缺的设定：时代背景、世界规则、重要地点、势力或社会关系、需要前后一致的禁忌与前提。\n"
                "3. 不要写成章节大纲或故事梗概。所有人物年龄 18 岁或以上。只输出续写的正文。\n\n"
                f"【思路】\n{idea_text}\n\n"
                f"【当前世界观草稿】\n{accumulated}\n"
            )
            yield StreamEvent(kind="status", message="世界观还没写完，正在继续补全…")

        messages = [
            {"role": "system", "content": SYSTEM_RULES},
            {"role": "user", "content": user},
        ]
        needed = target_chars if auto_round == 0 else max(target_chars - count_hanzi(accumulated), 400)
        max_tokens = plan_max_tokens(system_and_user(messages), target_chars=needed, enable_thinking=enable_thinking)
        done_event = None
        before = count_hanzi(accumulated)
        async for event in stream_chat_with_retry(
            messages,
            llm_url=llm_url,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=enable_thinking,
            cancel_event=cancel_event,
        ):
            if event.kind == "done" and event.result and event.result.content.strip():
                chunk = event.result.content.strip()
                if auto_round == 0:
                    accumulated = chunk
                else:
                    accumulated = (accumulated.rstrip() + "\n" + chunk).strip()
                books.write_world(book_name, accumulated + "\n")
                done_event = event
            elif event.kind in ("error", "cancelled"):
                if accumulated and accumulated != started_with and event.kind == "error":
                    yield StreamEvent(kind="status", message=f"补写中断：{event.message}。已保存目前写好的世界观。")
                    if final_event is not None:
                        yield final_event
                        return
                yield event
                return
            else:
                yield event

        if not done_event:
            if accumulated:
                kept = StreamEvent(kind="done", result=ChatResult(content=accumulated), text=accumulated)
                kept.extra = {"hanzi": count_hanzi(accumulated), "saved": "世界观.md"}
                yield kept
            return

        final_event = StreamEvent(kind="done", result=ChatResult(content=accumulated), text=accumulated)
        final_event.extra = {"hanzi": count_hanzi(accumulated), "saved": "世界观.md"}
        added = count_hanzi(accumulated) - before
        if not should_auto_continue(accumulated, target_chars):
            yield final_event
            return

        auto_round += 1
        if auto_round > AUTO_CONTINUE_MAX_ROUNDS:
            yield StreamEvent(
                kind="status",
                message=f"已自动续写 {AUTO_CONTINUE_MAX_ROUNDS} 轮，世界观现有 {count_hanzi(accumulated)} 字，先停在这里。可以再点一次「从思路草稿」继续补全。",
            )
            yield final_event
            return
        if auto_round > 1 and added < max(80, AUTO_CONTINUE_MIN_ADD_HANZI // 2):
            stall_rounds += 1
            if stall_rounds >= AUTO_CONTINUE_MAX_STALLS:
                yield StreamEvent(
                    kind="status",
                    message=f"自动补写没有写出足够的新内容，世界观现有 {count_hanzi(accumulated)} 字。可以再点一次继续补全。",
                )
                yield final_event
                return
        elif auto_round > 1:
            stall_rounds = 0


async def generate_character_cards(
    book_name: str,
    *,
    idea: str | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    llm_url: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    """Draft light character cards from 思路 (and 世界观 if present). Does not overwrite existing names."""
    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    if idea is not None:
        books.write_idea(book_name, idea)
    idea_text = (idea if idea is not None else books.read_idea(book_name)).strip()
    if not idea_text:
        yield StreamEvent(kind="error", message="请先写下思路，再生成人物")
        return
    from app import characters as character_store

    existing = character_store.list_characters(book_name)
    existing_names = [item.get("name") or "" for item in existing if item.get("name")]
    world_text = longform.world_prompt_text(book_name)
    world_block = f"【世界观】\n{world_text}\n\n" if world_text else ""
    temperature = settings["temperature"] if temperature is None else temperature
    enable_thinking = settings["enable_thinking"] if enable_thinking is None else enable_thinking
    llm_url = llm_url or settings["llm_url"]
    existing_block = ""
    if existing_names:
        existing_block = (
            "【已经有的人物（不要改写，也不要再用这些名字；可以只补全新的人）】\n"
            + "、".join(existing_names)
            + "\n\n"
        )
    user = (
        "请根据下面的「思路」列出这部原创小说的主要人物，写成轻量人物卡。\n"
        "要求：\n"
        "1. 每人只需「名称」和「基础设定」（身份、性格、与本书相关的固定设定）。不要写当前状态，不要写补丁，不要写剧情大纲。\n"
        "2. 只列主要人物，大约 3 到 8 人即可，不要把所有路过的角色都写进去。\n"
        "3. 所有人物年龄 18 岁或以上。人物、情节保持原创。若提供了世界观，须与之相符。\n"
        "4. 若已经有人物，不要重复那些名字，也不要改写他们；没有新人就返回空列表。\n"
        "5. 只输出 JSON，不要前言后记。格式如下：\n"
        "{\n"
        '  "characters": [\n'
        '    {"name": "姓名", "basics": "身份、性格、固定设定……"},\n'
        "  ]\n"
        "}\n\n"
        f"【思路】\n{idea_text}\n\n"
        f"{world_block}"
        f"{existing_block}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]
    max_tokens = plan_max_tokens(system_and_user(messages), target_chars=1200, enable_thinking=enable_thinking)
    logger.info("characters-draft start book=%s existing=%s thinking=%s", book_name, len(existing_names), enable_thinking)
    async for event in stream_chat_with_retry(
        messages,
        llm_url=llm_url,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "done" and event.result and event.result.content.strip():
            cards = longform.parse_character_cards(event.result.content)
            added = []
            skipped = []
            known = {n.strip() for n in existing_names if n.strip()}
            for card in cards:
                name = (card.get("name") or "").strip()
                if not name:
                    continue
                if name in known:
                    skipped.append(name)
                    continue
                try:
                    item = character_store.add_character(book_name, name, card.get("basics") or "")
                    added.append(item)
                    known.add(name)
                except books.BookError:
                    skipped.append(name)
            if not added and not cards:
                yield StreamEvent(kind="error", message="未能从思路里整理出人物卡，请再试一次，或到人物页手动添加")
                return
            items = character_store.list_characters(book_name)
            event.extra = {
                "added": added,
                "skipped": skipped,
                "characters": items,
                "count": len(added),
            }
            event.text = ""
            if event.result:
                event.result.content = ""
        yield event


async def check_consistency(
    book_name: str,
    chapter_id: str,
    *,
    chapter_text: str | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = None,
    llm_url: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    """Compare this chapter with worldview and character cards. Never edits prose or cards."""
    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    cid = books.validate_chapter_id(chapter_id)
    if chapter_text is not None:
        current = chapter_text_for_llm(chapter_text)
    else:
        try:
            current = chapter_text_for_llm(books.read_chapter(book_name, cid)["text"])
        except BookError:
            current = ""
    if not current:
        yield StreamEvent(kind="error", message="请先写下或生成本章正文，再检查一致性")
        return
    from app import characters as character_store

    known = character_store.list_characters(book_name, cid, inclusive=False)
    world_text = longform.world_prompt_text(book_name)
    char_text = longform.character_prompt_text(book_name, cid, inclusive=False)
    if not world_text and not char_text:
        yield StreamEvent(kind="error", message="还没有世界观或人物卡，请先写设定再检查一致性")
        return
    temperature = settings["temperature"] if temperature is None else temperature
    enable_thinking = settings["enable_thinking"] if enable_thinking is None else enable_thinking
    llm_url = llm_url or settings["llm_url"]
    user = (
        "请对照「世界观」和「人物卡」，检查「本章正文」是否出现与设定不符的地方。\n"
        "要求：\n"
        "1. 只列出明确的冲突或前后不一致，不要写赞美、摘要或修改后的正文。\n"
        "2. 不要擅自改正文，也不要直接改人物卡或世界观。\n"
        "3. 若本章写出了人物状态的新变化，可在 proposed_patches 里建议补丁，等作者确认后再写入。\n"
        "4. 若没有冲突，conflicts 与 proposed_patches 都输出空数组。\n"
        "5. 只输出一个 JSON 对象，不要 Markdown 前言。格式如下：\n"
        '{"conflicts":[{"text":"冲突说明","related_character":"人名或空字符串"}],'
        '"proposed_patches":[{"character_id":"c1","character_name":"人名","chapter_id":"'
        + cid
        + '","text":"建议写入的状态变化"}]}\n\n'
        f"【世界观】\n{world_text or '（无）'}\n\n"
        f"【人物（基础设定与写本章之前沿本线累积的状态）】\n{char_text or '（无）'}\n\n"
        f"【本章正文】\n{current}\n"
    )
    messages = [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]
    max_tokens = plan_max_tokens(system_and_user(messages), target_chars=1200, enable_thinking=enable_thinking)
    async for event in stream_chat_with_retry(
        messages,
        llm_url=llm_url,
        temperature=min(temperature, 0.4),
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "done" and event.result:
            parsed = longform.parse_consistency_output(event.result.content, known)
            # Force chapter_id on proposals; never write them here.
            for item in parsed["proposed_patches"]:
                if not item.get("chapter_id"):
                    item["chapter_id"] = cid
            event.extra = {
                "conflicts": parsed["conflicts"],
                "proposed_patches": parsed["proposed_patches"],
                "has_issues": parsed["has_issues"],
                "written": False,
            }
        yield event


async def refresh_path_synopsis(
    book_name: str,
    chapter_id: str,
    *,
    chapter_text: str | None = None,
    temperature: float | None = None,
    enable_thinking: bool | None = False,
    llm_url: str | None = None,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    """Update the rolling synopsis for the path ending at this chapter. Does not rewrite chapter text."""
    from app import synopsis as synopsis_store

    books.ensure_book(book_name)
    settings = books.load_settings(book_name)
    cid = books.validate_chapter_id(chapter_id)
    if chapter_text is not None:
        current = chapter_text_for_llm(chapter_text)
    else:
        try:
            current = chapter_text_for_llm(books.read_chapter(book_name, cid)["text"])
        except BookError:
            current = ""
    if not current:
        yield StreamEvent(kind="error", message="本章还没有正文，无法更新路径摘要")
        return
    previous_summary, source_id = synopsis_store.nearest_path_synopsis(book_name, cid, include_self=False)
    temperature = settings["temperature"] if temperature is None else temperature
    llm_url = llm_url or settings["llm_url"]
    # Keep this short and cheap; never use a large thinking budget by default.
    use_thinking = bool(enable_thinking)
    clipped = current if len(current) <= 6000 else current[:2000] + "\n……\n" + current[-3000:]
    user = (
        "请根据「上一份路径摘要」和「本章正文」，更新这条情节线的滚动前情摘要。\n"
        "要求：\n"
        "1. 只概括当前这条线已经发生、对后续写作仍重要的情节与人物状态，不要写其他结局或分支。\n"
        "2. 这是给作者和模型看的备忘，不是小说正文。控制在 400 到 800 字，条理清楚。\n"
        "3. 必须写清已锁定、下一章不能写反的事实：地点（谁在谁的住处、谁请谁来）、时间、在场人物，以及本章结束时的状态。\n"
        "4. 不要复述大段原文，不要输出标题以外的说明。只输出摘要正文。\n\n"
        f"【上一份路径摘要】\n{previous_summary or '（尚无，本章之前的内容请从本章正文中归纳需要保留的前情）'}\n\n"
        f"【本章正文】\n{clipped}\n"
    )
    messages = [
        {"role": "system", "content": SYSTEM_RULES},
        {"role": "user", "content": user},
    ]
    max_tokens = plan_max_tokens(system_and_user(messages), target_chars=800, enable_thinking=use_thinking)
    async for event in stream_chat_with_retry(
        messages,
        llm_url=llm_url,
        temperature=min(temperature, 0.5),
        max_tokens=max_tokens,
        enable_thinking=use_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "done" and event.result and event.result.content.strip():
            text = event.result.content.strip()
            saved = synopsis_store.write_synopsis(book_name, cid, text)
            # Guarantee the chapter body is untouched: we never call write_chapter here.
            event.extra = {
                "hanzi": saved["hanzi"],
                "chapter_id": cid,
                "text": saved["text"],
                "source": source_id,
                "saved": "路径摘要.json",
            }
        yield event
