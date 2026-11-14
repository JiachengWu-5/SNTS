from app.llm import _strip_think_tags, plan_max_tokens
from app.config import THINKING_RESERVE_TOKENS


def test_thinking_on_reserves_body_tokens():
    prompt = "请写一章。" * 20
    off = plan_max_tokens(prompt, target_chars=2000, enable_thinking=False)
    on = plan_max_tokens(prompt, target_chars=2000, enable_thinking=True)
    assert on >= off
    assert on >= THINKING_RESERVE_TOKENS
    # Even with a long prompt, we still plan a positive budget.
    long_prompt = "字" * 8000
    planned = plan_max_tokens(long_prompt, target_chars=2000, enable_thinking=True, context_tokens=16384)
    assert planned >= 256


def test_strip_think_tags_drops_reasoning_blocks():
    raw = "<think>secret</think>\n# 标题\n正文"
    assert "secret" not in _strip_think_tags(raw)
    assert "正文" in _strip_think_tags(raw)
    assert _strip_think_tags("<think>only thinking") == ""
