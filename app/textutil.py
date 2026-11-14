"""Shared text helpers. 漢字 are counted in the CJK Unified Ideographs range."""

from __future__ import annotations

import re

HANZI_RE = re.compile(r"[\u4E00-\u9FFF]")


def count_hanzi(text: str | None) -> int:
    if not text:
        return 0
    return len(HANZI_RE.findall(text))


def estimate_tokens(text: str | None) -> int:
    """Rough token estimate without loading a tokenizer."""
    if not text:
        return 0
    hanzi = count_hanzi(text)
    other = max(len(text) - hanzi, 0)
    return int(hanzi * 1.6 + other * 0.4) + 1


def first_heading(text: str | None) -> str:
    if not text:
        return ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^#+\s*", "", line)
        return line[:80]
    return ""


ILLUST_MARKER_RE = re.compile(r"【插图:([A-Za-z0-9_-]+)】")


def illustration_marker(illust_id: str) -> str:
    return f"【插图:{illust_id}】"


def strip_illustration_markers(text: str | None) -> str:
    if not text:
        return ""
    cleaned = ILLUST_MARKER_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def find_illustration_ids(text: str | None) -> list[str]:
    if not text:
        return []
    return ILLUST_MARKER_RE.findall(text)


def count_visible_hanzi(text: str | None) -> int:
    return count_hanzi(strip_illustration_markers(text))

