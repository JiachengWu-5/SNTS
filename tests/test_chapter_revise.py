from app.books import (
    BookError,
    backup_chapter,
    create_book,
    list_chapters,
    restore_previous_chapter,
    write_chapter,
)
import pytest


def test_backup_and_restore_chapter(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_WRITER_ARTICLE_ROOT", str(tmp_path))
    create_book("试书")
    write_chapter("试书", "01", "第一版正文。\n")
    assert backup_chapter("试书", "01") is True
    write_chapter("试书", "01", "第二版正文。\n")
    assert [item["id"] for item in list_chapters("试书")] == ["01"]
    first = restore_previous_chapter("试书", "01")
    assert "第一版正文" in first["text"]
    assert "第二版正文" not in first["text"]
    second = restore_previous_chapter("试书", "01")
    assert "第二版正文" in second["text"]


def test_restore_without_backup_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_WRITER_ARTICLE_ROOT", str(tmp_path))
    create_book("试书")
    write_chapter("试书", "01", "只有一版。\n")
    with pytest.raises(BookError, match="没有可恢复的上一版正文"):
        restore_previous_chapter("试书", "01")
