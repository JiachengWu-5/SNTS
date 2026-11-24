import json

import pytest

from app import books
from app.config import DEFAULT_SETTINGS


def test_create_and_roundtrip(article_root):
    created = books.create_book("示例作品")
    assert created["name"] == "示例作品"
    path = article_root / "示例作品"
    assert (path / "思路.md").exists()
    assert (path / "大纲.md").exists()
    assert (path / "style").is_dir()
    assert (path / "chapters").is_dir()
    assert (path / "settings.json").exists()

    books.write_idea("示例作品", "这是思路内容")
    books.write_outline("示例作品", "# 《标题》\n\n故事线")
    books.write_chapter("示例作品", "1", "第一章正文汉字")
    data = books.get_book("示例作品")
    assert data["idea"] == "这是思路内容"
    assert data["idea_hanzi"] == 6
    assert data["outline_hanzi"] == 5
    assert data["chapters"][0]["id"] == "01"
    assert data["chapters"][0]["hanzi"] == 7

    listed = books.list_books()
    assert any(item["name"] == "示例作品" for item in listed)


def test_reject_reserved_and_path_names(article_root):
    with pytest.raises(books.BookError):
        books.create_book("template")
    with pytest.raises(books.BookError):
        books.create_book("..")
    with pytest.raises(books.BookError):
        books.create_book("a/b")
    with pytest.raises(books.BookError):
        books.create_book("a\\b")
    with pytest.raises(books.BookError):
        books.get_book("不存在的书")


def test_add_chapter_slot_appends_outline(article_root):
    books.create_book("示例")
    books.write_outline("示例", "# 《示例》\n\n## 大纲\n")
    result = books.add_chapter_slot("示例", "出发")
    assert result["id"] == "01"
    outline = books.read_outline("示例")
    assert "第1章 出发" in outline
    assert (article_root / "示例" / "chapters" / "01.md").exists()
    second = books.add_chapter_slot("示例")
    assert second["id"] == "02"


def test_settings_merge_and_clamp(article_root):
    books.create_book("设定")
    saved = books.save_settings("设定", {"temperature": 9, "chapter_target_chars": 50, "enable_thinking": True})
    assert saved["temperature"] == 2.0
    assert saved["chapter_target_chars"] == 200
    assert saved["enable_thinking"] is True
    raw = json.loads((article_root / "设定" / "settings.json").read_text(encoding="utf-8"))
    assert "llm_url" in raw
    assert raw.get("selected_styles") or DEFAULT_SETTINGS["selected_styles"]


def test_template_dir_is_not_listed_as_book(article_root):
    books.create_book("正书")
    names = [item["name"] for item in books.list_books()]
    assert "template" not in names
    assert "正书" in names
