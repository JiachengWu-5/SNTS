from app.generate import (
    extract_continue_addition,
    merge_continue_text,
    should_auto_continue,
)


def test_should_auto_continue_respects_15_percent_window():
    assert should_auto_continue("字" * 3324, 10000) is True
    assert should_auto_continue("字" * 8499, 10000) is True
    assert should_auto_continue("字" * 8500, 10000) is False
    assert should_auto_continue("字" * 9000, 10000) is False
    assert should_auto_continue("字" * 200, 200) is False


def test_extract_keeps_pure_continuation():
    current = "天色将晚，他推开木门走了出去。"
    generated = "门外的雨已经小了，石阶上积着一层薄水。"
    assert extract_continue_addition(current, generated) == generated
    merged = merge_continue_text(current, generated)
    assert merged.startswith(current)
    assert "门外的雨已经小了" in merged
    assert merged.count("他推开木门") == 1


def test_extract_strips_full_current_prefix():
    current = "开头一段话。" * 20 + "这是目前的最后一句。"
    addition = "新写的下一段从这里开始。" * 5
    generated = current + "\n\n" + addition
    assert extract_continue_addition(current, generated) == addition
    merged = merge_continue_text(current, generated)
    assert merged.count("这是目前的最后一句") == 1
    assert "新写的下一段从这里开始" in merged


def test_extract_uses_ending_anchor():
    current = "前文内容。" * 30 + "他把杯子放下，没有再说话。"
    addition = "过了很久，对座的人才轻轻应了一声。"
    generated = "他把杯子放下，没有再说话。" + addition
    assert extract_continue_addition(current, generated) == addition


def test_extract_drops_same_length_rewrite():
    current = "这是本章开头的第一句，故事从这里慢慢展开。" * 15
    # Rewrite that starts the same way and is not meaningfully longer.
    generated = current[:80] + "随后情节被重新叙述了一遍，人物还是那些人。" * 10
    generated = generated[: len(current)]
    assert extract_continue_addition(current, generated) == ""
    merged = merge_continue_text(current, generated)
    assert merged.strip() == current.strip()


def test_extract_keeps_new_ending_after_rewrite():
    opening = "这是本章开头的第一句，夜晚的街道很安静。"
    body = "他沿着墙根走了很久，终于看见那扇还亮着灯的窗户。" * 8
    current = opening + body
    addition = "窗里忽然传来一声轻咳，他停住了脚步。"
    generated = current + addition
    assert extract_continue_addition(current, generated) == addition


def test_extract_strips_chapter_title_and_keeps_new_ending():
    current = "第三章 夜雨\n\n" + "他站在廊下听雨。" * 20 + "雨声忽然密了起来。"
    addition = "有人在巷口喊了他的名字。"
    generated = "第三章 夜雨\n\n" + "他站在廊下听雨。" * 20 + "雨声忽然密了起来。" + addition
    assert extract_continue_addition(current, generated) == addition
    merged = merge_continue_text(current, generated)
    assert merged.count("第三章") == 1
    assert "喊了他的名字" in merged


def test_merge_does_not_replace_chapter_with_rewrite():
    current = "原有正文从这里开始，并且已经写了不少内容。" * 20
    generated = current[:80] + "这是从头再写的另一版，情节几乎一样。" * 18
    merged = merge_continue_text(current, generated)
    assert "原有正文从这里开始" in merged
    assert merged.strip().startswith("原有正文从这里开始")
    # Must not be a replacement that drops the original middle/end.
    assert "并且已经写了不少内容" in merged
