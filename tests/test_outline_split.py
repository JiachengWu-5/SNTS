from app.books import (
    outline_chapter_section,
    outline_has_chapter,
    parse_chapter_token,
    split_outline_chapters,
)


def test_parse_chapter_token():
    assert parse_chapter_token("1") == 1
    assert parse_chapter_token("01") == 1
    assert parse_chapter_token("一") == 1
    assert parse_chapter_token("十") == 10
    assert parse_chapter_token("十一") == 11
    assert parse_chapter_token("二十") == 20
    assert parse_chapter_token("二十一") == 21


def test_prefers_real_chapter_over_placeholder():
    outline = """# 《试书》

## 故事线

一条长线。

## 大纲

### 第一章 雨夜

这里有完整情节，人物和章末钩子。

### 第二章 后事

后一章的事。

### 第1章 待拟标题

（待补充）
"""
    assert outline_has_chapter(outline, 1)
    assert outline_has_chapter(outline, 2)
    section = outline_chapter_section(outline, 1)
    assert "雨夜" in section
    assert "完整情节" in section
    assert "待拟标题" not in section
    preamble, chapters = split_outline_chapters(outline)
    assert "故事线" in preamble
    assert [ch["num"] for ch in chapters] == [1, 2, 1]
