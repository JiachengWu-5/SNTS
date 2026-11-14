from app.books import (
    backup_chapter,
    backup_outline,
    create_book,
    restore_previous_chapter,
    restore_saved_chapter,
    save_chapter_snapshot,
    write_chapter,
    write_outline,
)
from app.history import (
    capture_chapter,
    list_chapter_history,
    list_outline_history,
    record_chapter_change,
    restore_chapter_entry,
    restore_outline_entry,
    save_outline_snapshot,
)


def test_record_and_restore_keeps_current(article_root):
    create_book("试书")
    write_chapter("试书", "01", "第一版正文，比较长的一章。\n")
    record_chapter_change("试书", "01", "第一版正文，比较长的一章。", "第二版被改短了。", "revise")
    items = list_chapter_history("试书", "01")["items"]
    kinds = [item["kind"] for item in items]
    assert "revise" in kinds
    assert any(item["kind"] in {"prior", "revise"} for item in items)
    # newest first: revise then prior
    assert items[0]["kind"] == "revise"
    long_id = next(item["id"] for item in items if "第一版正文" in item["preview"] or item["kind"] == "prior")
    write_chapter("试书", "01", "第二版被改短了。\n")
    restored = restore_chapter_entry("试书", "01", long_id)
    assert "第一版正文" in restored["text"]
    after = list_chapter_history("试书", "01")["items"]
    assert any("第二版被改短了" in (item.get("preview") or "") for item in after)


def test_identical_snapshot_is_not_duplicated(article_root):
    create_book("试书")
    write_chapter("试书", "01", "同一段正文。\n")
    capture_chapter("试书", "01", "同一段正文。", "save")
    capture_chapter("试书", "01", "同一段正文。", "save")
    items = list_chapter_history("试书", "01")["items"]
    assert len(items) == 1


def test_imports_prev_and_saved(article_root):
    create_book("试书")
    write_chapter("试书", "12", "现在的正文。\n")
    backup_chapter("试书", "12")
    write_chapter("试书", "12", "后来的正文。\n")
    save_chapter_snapshot("试书", "12")
    write_chapter("试书", "12", "又改过的正文。\n")
    items = list_chapter_history("试书", "12")["items"]
    kinds = {item["kind"] for item in items}
    assert "prev" in kinds or "prior" in kinds
    assert "saved" in kinds or "save" in kinds
    texts = " ".join(item["preview"] for item in items)
    assert "现在的正文" in texts
    assert "后来的正文" in texts


def test_autosave_style_put_does_not_create_history_via_write(article_root):
    create_book("试书")
    write_chapter("试书", "01", "边写边改。\n")
    write_chapter("试书", "01", "又改了一个字。\n")
    items = list_chapter_history("试书", "01")["items"]
    assert items == []


def test_outline_save_and_restore(article_root):
    create_book("试书")
    write_outline("试书", "第一章 开始\n\n大纲甲。\n")
    save_outline_snapshot("试书")
    write_outline("试书", "第一章 开始\n\n大纲乙。\n")
    backup_outline("试书")
    write_outline("试书", "第一章 开始\n\n大纲丙。\n")
    items = list_outline_history("试书")["items"]
    assert items
    first_id = next(item["id"] for item in items if "大纲甲" in item["preview"])
    restored = restore_outline_entry("试书", first_id)
    assert "大纲甲" in restored["text"]
    after = list_outline_history("试书")["items"]
    assert after[0]["kind"] == "restore"
