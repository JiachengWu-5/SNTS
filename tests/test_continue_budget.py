from app.generate import (
    CONTINUE_MIN_PLANNED_TOKENS,
    SYSTEM_RULES,
    _chapter_user_prompt,
    _continue_current_block,
    clip_tail_hanzi,
    continue_needed_chars,
    fit_continue_prompt,
    system_and_user,
)
from app.llm import available_output_tokens
from app.textutil import count_hanzi


def _prompt_parts():
    current = ("前段铺垫。" * 20 + "中间发生了一些事。") * 50 + "这是本章目前的最后一句。"
    assert count_hanzi(current) >= 3500
    previous = "【第一章已写正文（以作者修订稿为准，必须承接）】\n" + ("上一章内容。" * 700)
    excerpts = [{"name": f"样本{i}", "text": "语气句子。" * 40} for i in range(12)]
    return dict(
        current=current,
        previous=previous,
        current_section="第二章应写主角继续调查并在章末留下线索。" * 8,
        compact_outline="故事线\n" + ("各章摘要。" * 200),
        idea_for_prompt="一部当代小说。" * 20,
        excerpts=excerpts,
        chapter_num=2,
        target_chars=5000,
        enable_thinking=True,
        requirements="1. 只从结尾续写。\n",
        closing="请现在从已有正文的结尾处继续写。\n",
    )


def test_continue_needed_chars_uses_remaining_or_minimum():
    assert continue_needed_chars("字" * 1000, 5000) == 4000
    assert continue_needed_chars("字" * 4800, 5000) == 800
    assert continue_needed_chars("字" * 6000, 5000) == 800


def test_clip_tail_hanzi_keeps_ending():
    text = "开头段落。" + ("中间内容。" * 100) + "这是结尾句。"
    tail = clip_tail_hanzi(text, 30)
    assert "这是结尾句" in tail
    assert "开头段落" not in tail
    assert count_hanzi(tail) <= 40
    assert count_hanzi(tail) >= 20


def test_long_continue_reallocates_output_budget():
    parts = _prompt_parts()
    # Long enough that a full-chapter prompt cannot leave a real output budget,
    # even with a 64k window. The fitted prompt must still keep the ending.
    parts["current"] = ("前段铺垫。" * 20 + "中间发生了一些事。") * 400 + "这是本章目前的最后一句。"
    naive_user = _chapter_user_prompt(
        task="请续写本章。",
        requirements=parts["requirements"],
        current_section=parts["current_section"],
        compact_outline=parts["compact_outline"],
        idea_for_prompt=parts["idea_for_prompt"],
        previous=parts["previous"] + "\n\n",
        current_block=_continue_current_block(parts["current"], None),
        excerpts=parts["excerpts"],
        closing=parts["closing"],
    )
    naive_prompt = SYSTEM_RULES + "\n" + naive_user
    naive_room = available_output_tokens(naive_prompt)

    messages, planned, use_thinking, status = fit_continue_prompt(**parts)
    prompt = system_and_user(messages)
    assert "这是本章目前的最后一句" in prompt
    assert planned >= CONTINUE_MIN_PLANNED_TOKENS
    assert planned <= available_output_tokens(prompt)
    assert planned > naive_room
    assert "重新分配了输出额度" in status
    if available_output_tokens(prompt) < continue_needed_chars(parts["current"], 5000) * 2.2 + 4096:
        assert use_thinking is False


def test_short_continue_keeps_full_current_text():
    messages, planned, _thinking, status = fit_continue_prompt(
        current="短短的一章开头，到此为止。",
        previous="",
        current_section="本章写相遇。",
        compact_outline="第一章 相遇",
        idea_for_prompt="思路",
        excerpts=[],
        chapter_num=1,
        target_chars=3000,
        enable_thinking=False,
        requirements="1. 只从结尾续写。\n",
        closing="请继续写。\n",
    )
    prompt = system_and_user(messages)
    assert "短短的一章开头，到此为止。" in prompt
    assert planned >= CONTINUE_MIN_PLANNED_TOKENS
    assert "压缩" not in status


def test_continue_at_target_still_gets_a_budget():
    current = "字" * 5000 + "结束句。"
    messages, planned, _thinking, status = fit_continue_prompt(
        current=current,
        previous="前一章。" * 400,
        current_section="本章继续。",
        compact_outline="大纲" * 100,
        idea_for_prompt="思路",
        excerpts=[{"name": "s", "text": "样本" * 50}] * 8,
        chapter_num=2,
        target_chars=5000,
        enable_thinking=True,
        requirements="1. 只从结尾续写。\n",
        closing="请继续写。\n",
    )
    prompt = system_and_user(messages)
    assert "结束句" in prompt
    assert planned >= CONTINUE_MIN_PLANNED_TOKENS
    assert "约 800 字" in status
