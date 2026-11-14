from app.books import (
    BookError,
    create_book,
    list_chapters,
    read_chapter,
    restore_saved_chapter,
    save_chapter_snapshot,
    write_chapter,
)
import pytest


def test_save_and_restore_chapter_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_WRITER_ARTICLE_ROOT", str(tmp_path))
    create_book("试书")
    write_chapter("试书", "01", "满意的一章正文。\n")
    saved = save_chapter_snapshot("试书", "01")
    assert saved["ok"] is True
    assert saved["hanzi"] >= 1
    assert [item["id"] for item in list_chapters("试书")] == ["01"]
    write_chapter("试书", "01", "后来被覆盖的正文。\n")
    restored = restore_saved_chapter("试书", "01")
    assert "满意的一章正文" in restored["text"]
    assert "后来被覆盖的正文" not in restored["text"]


def test_save_empty_chapter_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_WRITER_ARTICLE_ROOT", str(tmp_path))
    create_book("试书")
    write_chapter("试书", "01", "")
    with pytest.raises(BookError, match="没有可保存的正文"):
        save_chapter_snapshot("试书", "01")
