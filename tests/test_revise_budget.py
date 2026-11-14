from app.generate import (
    join_chapter_chunks,
    revise_needed_tokens,
    revise_result_too_short,
    revise_short_notice,
    _revise_done_extra,
    revise_should_chunk,
    split_chapter_chunks,
)
from app.llm import available_output_tokens, plan_max_tokens
from app.textutil import count_hanzi


def test_split_and_join_preserves_hanzi():
    paragraphs = [f"第{i}段。" + "正文内容" * 80 for i in range(1, 8)]
    text = "\n\n".join(paragraphs) + "\n"
    chunks = split_chapter_chunks(text, max_hanzi=400)
    assert len(chunks) >= 3
    assert all(count_hanzi(c) <= 400 or count_hanzi(c) < 500 for c in chunks)
    joined = join_chapter_chunks(chunks)
    assert count_hanzi(joined) == count_hanzi(text)
    for i in range(1, 8):
        assert f"第{i}段" in joined


def test_long_chapter_revise_should_chunk():
    current = "字" * 40000
    prompt = "请修改本章。\n" + current
    assert revise_should_chunk(prompt, current) is True
    room = available_output_tokens(prompt)
    assert room < revise_needed_tokens(current)


def test_short_chapter_revise_fits_in_one_shot():
    current = "字" * 400
    prompt = "请修改本章。\n" + current
    assert revise_should_chunk(prompt, current) is False
    planned = plan_max_tokens(prompt, target_chars=400, enable_thinking=False)
    assert planned >= revise_needed_tokens(current) * 0.85


def test_revise_result_too_short_guard():
    original = "字" * 1000
    assert revise_result_too_short(original, "字" * 300) is True
    assert revise_result_too_short(original, "字" * 800) is False


def test_plan_max_tokens_does_not_invent_room():
    prompt = "字" * 10000
    planned = plan_max_tokens(prompt, target_chars=7000, enable_thinking=True, context_tokens=16384)
    room = available_output_tokens(prompt, 16384)
    assert planned <= room


def test_revise_short_notice_does_not_discard():
    original = "字" * 1000
    short = "字" * 300
    assert revise_result_too_short(original, short) is True
    notice = revise_short_notice(original, short)
    assert notice is not None
    assert "300" in notice
    assert "700" in notice
    assert "恢复上一版" in notice
    assert revise_short_notice(original, "字" * 800) is None
    extra = _revise_done_extra("03", original, short)
    assert extra["saved"] is True
    assert extra["short_warning"] is True
    assert extra["hanzi"] == 300
    assert extra["old_hanzi"] == 1000
    assert "恢复上一版" in extra["message"]
