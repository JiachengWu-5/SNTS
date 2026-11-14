from app.generate import (
    EXPAND_MIN_GROWTH_HANZI,
    _expand_chunk_messages,
    build_expand_messages,
    expand_chunk_target,
    expand_goal_chars,
    expand_is_valid,
    expand_needed_tokens,
    expand_original_coverage,
    expand_prompt_pieces,
    expand_result_too_short,
    expand_should_chunk,
    revise_result_too_short,
    trim_expand_restart,
)
from app.llm import available_output_tokens, plan_max_tokens
from app.textutil import count_hanzi


def test_expand_goal_aims_at_user_target():
    current = "字" * 3324
    assert expand_goal_chars(current, 10000) == 10000


def test_expand_goal_allows_modest_growth_when_already_near_target():
    current = "字" * 9000
    assert expand_goal_chars(current, 10000) == 10800


def test_expand_goal_caps_at_20000():
    current = "字" * 18000
    assert expand_goal_chars(current, 10000) == 20000


def test_expand_chunk_target_is_proportional():
    current = "甲" * 400 + "乙" * 600
    chunk_a = "甲" * 400
    chunk_b = "乙" * 600
    assert expand_chunk_target(chunk_a, current, 3000) == 1200
    assert expand_chunk_target(chunk_b, current, 3000) == 1800


def test_expand_result_rejects_no_real_growth():
    original = "字" * 1000
    assert expand_result_too_short(original, "字" * 999) is True
    assert expand_result_too_short(original, "字" * 1000) is True
    assert expand_result_too_short(original, "字" * (1000 + EXPAND_MIN_GROWTH_HANZI - 1)) is True
    assert expand_result_too_short(original, "字" * (1000 + EXPAND_MIN_GROWTH_HANZI)) is False
    assert expand_result_too_short(original, "字" * 1500) is False


def test_revise_guard_still_allows_modest_shortening():
    original = "字" * 1000
    assert revise_result_too_short(original, "字" * 800) is False
    assert revise_result_too_short(original, "字" * 300) is True


def test_expand_needed_tokens_uses_target_not_just_current():
    current = "字" * 3000
    needed = expand_needed_tokens(current, 10000)
    assert needed >= int(10000 * 2.2)
    assert needed > expand_needed_tokens(current, 3000)


def test_long_expand_should_chunk():
    current = "字" * 40000
    prompt = "请扩写本章。\n" + current
    assert expand_should_chunk(prompt, current, 50000) is True
    room = available_output_tokens(prompt)
    assert room < expand_needed_tokens(current, 50000)


def test_medium_chapter_prefers_chunk_even_when_context_fits():
    current = "字" * 3000
    prompt = "请扩写本章。\n" + current
    assert expand_should_chunk(prompt, current, 10000) is True


def test_short_expand_fits_in_one_shot():
    current = "字" * 400
    prompt = "请扩写本章。\n" + current
    assert expand_should_chunk(prompt, current, 2000) is False
    planned = plan_max_tokens(prompt, target_chars=2000, enable_thinking=False)
    assert planned > 0
    assert planned >= int(2000 * 1.5) or planned == available_output_tokens(prompt)


def _insert_details(original: str, filler: str, times: int) -> str:
    """Simulate a true 扩写: keep every original sentence, insert detail between them."""
    parts = original.split("。")
    out = []
    for i, part in enumerate(parts):
        if not part:
            continue
        out.append(part + "。")
        if i < times:
            out.append(filler)
    return "".join(out)


def test_coverage_high_when_original_sentences_are_kept():
    original = "他放下杯子，没有再说话。门外下起了雨。她没有回头。" * 8
    filler = "指节还在发白，茶已经凉了，走廊里只剩下雨落在屋檐上的声音。"
    expanded = _insert_details(original, filler, 30)
    assert expand_original_coverage(original, expanded) >= 0.95
    assert count_hanzi(expanded) >= count_hanzi(original) + EXPAND_MIN_GROWTH_HANZI
    assert expand_is_valid(original, expanded) is True


def test_coverage_low_on_rewrite():
    original = "他放下杯子，没有再说话。门外下起了雨。她没有回头。" * 8
    rewrite = "房间里只剩下时钟的声音。两人之间已经没有什么可说。窗外的夜色越来越深。" * 12
    assert count_hanzi(rewrite) > count_hanzi(original) + EXPAND_MIN_GROWTH_HANZI
    assert expand_original_coverage(original, rewrite) < 0.5
    assert expand_is_valid(original, rewrite) is False


def test_same_length_rewrite_is_invalid():
    original = "他放下杯子，没有再说话。门外下起了雨。" * 20
    rewrite = "房间里只剩下时钟的声音。两人之间没有什么可说。" * 20
    # even if coincidentally similar length, a rewrite is not 扩写
    assert expand_is_valid(original, rewrite) is False
    assert expand_is_valid(original, original) is False


def test_expand_prompt_is_keep_and_insert_not_rewrite():
    current = "他放下杯子，没有再说话。" * 20
    task, requirements, closing = expand_prompt_pieces(4, 10000, current, "第4章大纲")
    blob = task + requirements + closing
    assert "不是重写" in task
    assert "不是续写" in task
    assert "10000" in task
    assert str(count_hanzi(current)) in task
    assert "必须保留" in blob or "都必须保留" in blob
    assert "禁止删减" in task
    assert "第4章大纲" in closing
    messages = build_expand_messages(
        chapter_num=4,
        current=current,
        target_chars=10000,
        current_section="第4章大纲",
        compact_outline="",
        idea_for_prompt="",
        excerpts=[],
        previous="",
        repeat_section="第4章大纲",
    )
    user = messages[1]["content"]
    assert "原文必须全部保留" in user
    assert "不是重写" in user



def _sample_original() -> str:
    """A short chapter of unique sentences, so the opening is not reused later."""
    return (
        "他放下杯子，没有再说话。"
        "窗外的雨忽然密了起来。"
        "她把外套重新扣好，站在门边。"
        "走廊的灯忽明忽暗，像随时会熄。"
        "茶已经凉透了，杯沿还留着一圈淡痕。"
        "远处传来电梯闭合的声音。"
        "他想起早上那通没有说完的电话。"
        "她终于转身，却没有看他。"
        "雨水流过窗玻璃，把街灯拉成一条一条的线。"
        "房间里只剩下时钟，一下一下，不紧不慢。"
        "他把那张纸条翻过来，字迹已经被茶水晕开。"
        "她伸手按住门把，停了停，还是推开了门。"
        "冷风涌进来，把桌上的信封吹到了地面。"
        "他没有去捡。"
        "门在她身后轻轻关上，楼道里只剩下雨声。"
    )


def _sample_expanded() -> str:
    filler = "指节还在发白，走廊里只剩下雨落在屋檐上的声音。"
    return _insert_details(_sample_original(), filler, 20)


def test_trim_cuts_restart_from_original_opening():
    original = _sample_original()
    expanded = _sample_expanded()
    generated = expanded.rstrip() + "\n\n" + original
    trimmed = trim_expand_restart(original, generated)
    assert trimmed == expanded.rstrip()
    assert expand_is_valid(original, trimmed) is True
    assert count_hanzi(trimmed) < count_hanzi(generated)
    assert count_hanzi(original) >= 200


def test_trim_cuts_self_repetition_of_expanded_opening():
    original = _sample_original()
    expanded = _sample_expanded()
    generated = expanded.rstrip() + "\n\n" + expanded
    trimmed = trim_expand_restart(original, generated)
    assert trimmed == expanded.rstrip()
    assert expand_is_valid(original, trimmed) is True


def test_trim_strips_chapter_title_that_introduces_restart():
    original = _sample_original()
    expanded = _sample_expanded()
    generated = expanded.rstrip() + "\n\n第二章 雨夜\n" + original
    trimmed = trim_expand_restart(original, generated)
    assert trimmed == expanded.rstrip()
    assert "第二章" not in trimmed


def test_trim_leaves_clean_expand_unchanged():
    original = _sample_original()
    expanded = _sample_expanded()
    assert trim_expand_restart(original, expanded) == expanded
    assert trim_expand_restart(original, expanded + "\n") in (expanded, expanded + "\n")


def test_short_echo_in_the_middle_is_not_a_restart():
    original = _sample_original()
    # A short phrase from early in the chapter is echoed later, far below the 24-hanzi anchor.
    filler = "茶已经凉了，他放下杯子，指节还在发白，走廊里只剩下雨声。"
    expanded = _insert_details(original, filler, 20)
    assert trim_expand_restart(original, expanded) == expanded
    assert expand_is_valid(original, expanded) is True


def test_expand_prompt_forbids_restart_from_beginning():
    current = "他放下杯子，没有再说话。" * 20
    task, requirements, closing = expand_prompt_pieces(4, 10000, current, "第4章大纲")
    blob = task + requirements + closing
    assert "从头再写一遍" in blob
    assert "写完原有结尾就停止" in closing
    assert "在达到大约这个字数之前不要收束全章" not in blob
    assert "不要从开头重新写" in requirements
    messages = _expand_chunk_messages(
        chapter_num=4,
        index=1,
        total=2,
        current_section="第4章大纲",
        compact_outline="",
        idea_for_prompt="",
        excerpts=[],
        chunk=current,
        prev_tail="",
        next_head="下一段",
        chunk_target=2000,
    )
    user = messages[1]["content"]
    assert "不要从本段开头再写一遍" in user
