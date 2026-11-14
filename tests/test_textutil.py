from app.textutil import count_hanzi, estimate_tokens, first_heading


def test_count_hanzi_only_cjk_unified():
    assert count_hanzi("") == 0
    assert count_hanzi(None) == 0  # type: ignore[arg-type]
    assert count_hanzi("Hello 世界 123") == 2
    assert count_hanzi("只写汉字。Punctuation and ABC don't count.") == 4
    # Hiragana / punctuation should not be counted.
    assert count_hanzi("こんにちは。") == 0


def test_estimate_tokens_positive():
    assert estimate_tokens("这是一段中文") > 0
    assert estimate_tokens("") == 0


def test_first_heading():
    assert first_heading("# 《书名》\n\n正文") == "《书名》"
    assert first_heading("   \n第一章 开始") == "第一章 开始"
    assert first_heading("") == ""
