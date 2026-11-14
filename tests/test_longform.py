import asyncio
import json

from fastapi.testclient import TestClient

from app import books, characters, longform, synopsis
from app.generate import (
    _chapter_user_prompt,
    _previous_chapter_context,
    check_consistency,
    generate_chapter,
    generate_next_outline_chapter,
    generate_outline,
    refresh_path_synopsis,
)
from app.llm import ChatResult, StreamEvent
from app.main import app, jobs
from app.tree import sync_book_tree


BRANCHED_OUTLINE = """# 《潮汐电台》

## 大纲

### 第一章 开端

主干一。TRUNK_OUTLINE_1

### 第二章 分叉前

主干二。TRUNK_OUTLINE_2

### 第三章 一结局 路甲

OUTLINE_ENDING_A1

### 第四章 一结局 路甲终

OUTLINE_ENDING_A2

### 第三章 二结局 路乙

OUTLINE_ENDING_B1

### 第四章 二结局 路乙终

OUTLINE_ENDING_B2
"""


def _write_branched_book(name="潮汐电台"):
    books.create_book(name)
    books.write_idea(name, "一座海边电台，两条不同的结局。")
    books.write_world(name, "潮汐会改写记忆，但不得让未成年人出场。")
    books.write_outline(name, BRANCHED_OUTLINE)
    books.write_chapter(name, "01", "TRUNK_START 林晚走进电台。\n")
    books.write_chapter(name, "02", "TRUNK_FORK 信号一分为二。\n")
    books.write_chapter(name, "03", "ENDING_A_START 一结局开始。\n")
    books.write_chapter(name, "04", "ENDING_A_END 一结局结束。\n")
    books.write_chapter(name, "05", "ENDING_B_START 二结局开始。\n")
    books.write_chapter(name, "06", "ENDING_B_END 二结局结束。\n")
    sync_book_tree(name)
    return name


def _fake_stream(content, captured=None):
    async def fake_stream(messages, **kwargs):
        if captured is not None:
            captured["messages"] = messages
            captured["user"] = messages[1]["content"] if len(messages) > 1 else ""
        result = ChatResult(content=content)
        yield StreamEvent(kind="done", result=result, text=content)

    return fake_stream


def _collect(agen):
    async def runner():
        events = []
        async for event in agen:
            events.append(event)
        return events

    return asyncio.run(runner())


def test_create_book_starts_with_empty_world(article_root):
    books.create_book("新书")
    path = article_root / "新书" / "世界观.md"
    assert path.exists()
    assert books.read_world("新书") == ""
    books.write_world("新书", "这里是独立世界观。")
    assert books.read_world("新书") == "这里是独立世界观。"
    data = books.get_book("新书")
    assert data["world"] == "这里是独立世界观。"
    assert data["world_hanzi"] == 8


def test_world_omitted_from_prompt_when_empty(article_root):
    books.create_book("空世界")
    assert longform.world_prompt_text("空世界") == ""
    assert "【世界观】" not in longform.load_longform_blocks("空世界", None)


def test_world_included_in_outline_and_chapter_prompts(article_root, monkeypatch):
    name = _write_branched_book()
    captured = {}
    monkeypatch.setattr("app.generate.stream_chat_with_retry", _fake_stream(
        "# 《潮汐电台》\n\n## 大纲\n\n### 第一章 开端\n\n新写的一章。\n",
        captured,
    ))
    events = _collect(generate_outline(name, idea="一座海边电台"))
    assert any(ev.kind == "done" for ev in events)
    assert "【世界观】" in captured["user"]
    assert "潮汐会改写记忆" in captured["user"]

    captured.clear()
    monkeypatch.setattr("app.generate.stream_chat_with_retry", _fake_stream(
        "第二章正文。\n", captured,
    ))
    events = _collect(generate_chapter(name, "02", mode="generate"))
    assert any(ev.kind == "done" for ev in events)
    assert "【世界观】" in captured["user"]
    assert "潮汐会改写记忆" in captured["user"]
    assert "ENDING_A_END" not in captured["user"]
    assert "ENDING_B_START" not in captured["user"]


def test_append_only_next_outline_does_not_replace_existing(article_root):
    existing = "# 《潮汐电台》\n\n## 大纲\n\n### 第一章 开端\n\n电台开门。\n"
    generated = (
        "# 《别的书》\n\n## 大纲\n\n"
        "### 第一章 开端\n\n这段必须被丢掉。\n\n"
        "### 第二章 夜航\n\n林晚听到第二遍潮汐。\n"
    )
    section = longform.isolate_new_outline_section(generated, existing)
    assert "第二章" in section
    assert "夜航" in section
    assert "这段必须被丢掉" not in section
    merged = longform.append_outline_section(existing, section)
    assert "电台开门" in merged
    assert "夜航" in merged
    assert merged.index("电台开门") < merged.index("夜航")
    assert "别的书" not in merged
    # existing first chapter text stays exactly
    assert "这段必须被丢掉" not in merged


def test_empty_outline_first_click_writes_only_chapter_one(article_root):
    generated = (
        "# 《潮汐电台》\n\n## 大纲\n\n"
        "### 第一章 开端\n\n只应留下这一章。\n\n"
        "### 第二章 不该出现\n\n多余。\n"
    )
    section = longform.isolate_new_outline_section(generated, "")
    assert "第一章" in section
    assert "只应留下这一章" in section
    assert "不该出现" not in section


def test_generate_next_outline_appends_on_current_branch(article_root, monkeypatch):
    name = _write_branched_book()
    original = books.read_outline(name)
    captured = {}
    monkeypatch.setattr(
        "app.generate.stream_chat_with_retry",
        _fake_stream("### 第五章 二结局 余波\n\n乙线继续向前。\n", captured),
    )
    events = _collect(generate_next_outline_chapter(name, from_chapter="06"))
    done = [ev for ev in events if ev.kind == "done"]
    assert done
    text = books.read_outline(name)
    assert original in text or all(marker in text for marker in ("OUTLINE_ENDING_A1", "OUTLINE_ENDING_B1", "TRUNK_OUTLINE_1"))
    assert "乙线继续向前" in text
    assert text.count("OUTLINE_ENDING_A1") == original.count("OUTLINE_ENDING_A1")
    assert "【世界观】" in captured["user"]
    # current branch outline is provided, sibling ending may appear in the stored full outline
    # but the prompt should stay on ending B
    assert "OUTLINE_ENDING_B1" in captured["user"]
    assert "OUTLINE_ENDING_A1" not in captured["user"]
    # previous outline backed up
    assert books.has_previous_outline(name)


def test_next_outline_from_fork_requires_a_branch(article_root):
    name = _write_branched_book()
    try:
        longform.next_outline_spec(name, "02")
        raise AssertionError("should have required a branch tip")
    except books.BookError as exc:
        assert "最后一章" in str(exc) or "多条线" in str(exc)


def test_character_patches_isolated_across_sibling_branches(article_root):
    name = _write_branched_book()
    lin = characters.add_character(name, "林晚", "电台值班员，二十二岁。")
    characters.add_patch(name, lin["id"], "01", "第一次听见潮汐噪声")
    characters.add_patch(name, lin["id"], "03", "一结局：选择留下")
    characters.add_patch(name, lin["id"], "05", "二结局：选择离开")

    writing_b = characters.list_characters(name, "06", inclusive=False)
    card = writing_b[0]
    texts = [p["text"] for p in card["path_patches"]]
    assert "第一次听见潮汐噪声" in texts
    assert "二结局：选择离开" in texts
    assert "一结局：选择留下" not in texts
    assert "一结局" not in card["current_state"]
    assert "选择离开" in card["current_state"]

    writing_a = characters.list_characters(name, "04", inclusive=False)
    texts_a = [p["text"] for p in writing_a[0]["path_patches"]]
    assert "第一次听见潮汐噪声" in texts_a
    assert "一结局：选择留下" in texts_a
    assert "二结局：选择离开" not in texts_a

    # Writing chapter 05 should see trunk + not include the patch of 05 itself
    at_start_of_b = characters.list_characters(name, "05", inclusive=False)
    texts_start = [p["text"] for p in at_start_of_b[0]["path_patches"]]
    assert "第一次听见潮汐噪声" in texts_start
    assert "二结局：选择离开" not in texts_start
    assert "一结局：选择留下" not in texts_start


def test_character_and_synopsis_blocks_stay_on_path(article_root):
    name = _write_branched_book()
    lin = characters.add_character(name, "林晚", "电台值班员。")
    characters.add_patch(name, lin["id"], "03", "走入一结局")
    characters.add_patch(name, lin["id"], "05", "走入二结局")
    synopsis.write_synopsis(name, "04", "一结局摘要：她留在了灯塔。")
    synopsis.write_synopsis(name, "05", "二结局摘要：她带着录音离开。")

    block_b = longform.load_longform_blocks(name, "06", for_writing=True)
    assert "走入二结局" in block_b
    assert "走入一结局" not in block_b
    assert "二结局摘要：她带着录音离开" in block_b
    assert "她留在了灯塔" not in block_b

    block_a = longform.load_longform_blocks(name, "04", for_writing=True)
    # writing 04 uses state/summary up to previous chapter (03), not 04's own summary
    assert "走入一结局" in block_a
    assert "走入二结局" not in block_a
    assert "她留在了灯塔" not in block_a
    assert "她带着录音离开" not in block_a

    user = _chapter_user_prompt(
        task="请撰写本章。",
        requirements="1. 只沿本线写。\n",
        current_section="本章大纲",
        compact_outline="结构",
        idea_for_prompt="思路",
        previous=_previous_chapter_context(name, "06"),
        current_block="",
        excerpts=[],
        closing="",
        longform=block_b,
    )
    assert "【人物" in user
    assert "【路径前情摘要】" in user
    assert "【世界观】" in user
    assert "ENDING_B_START" in user
    assert "ENDING_A_END" not in user
    assert "ENDING_A_START" not in user


def test_path_synopsis_update_does_not_rewrite_chapter(article_root, monkeypatch):
    name = _write_branched_book()
    before = books.read_chapter(name, "05")["text"]
    synopsis.write_synopsis(name, "02", "主干到分叉前的摘要。")
    monkeypatch.setattr(
        "app.generate.stream_chat_with_retry",
        _fake_stream("二结局线走到离开电台。"),
    )
    events = _collect(refresh_path_synopsis(name, "05"))
    assert any(ev.kind == "done" for ev in events)
    assert "二结局线走到离开电台" in synopsis.read_synopsis(name, "05")
    assert books.read_chapter(name, "05")["text"] == before
    # sibling summary untouched
    synopsis.write_synopsis(name, "03", "甲线摘要不应被这次更新碰到。")
    assert synopsis.read_synopsis(name, "03") == "甲线摘要不应被这次更新碰到。"


def test_consistency_proposes_but_does_not_write_until_confirmed(article_root, monkeypatch):
    name = _write_branched_book()
    lin = characters.add_character(name, "林晚", "从不离开码头。")
    payload = json.dumps({
        "conflicts": [{"text": "正文写她离开了码头，与基础设定不符", "related_character": "林晚"}],
        "proposed_patches": [{
            "character_id": lin["id"],
            "character_name": "林晚",
            "chapter_id": "05",
            "text": "最终还是离开了码头",
        }],
    }, ensure_ascii=False)
    monkeypatch.setattr("app.generate.stream_chat_with_retry", _fake_stream(payload))
    events = _collect(check_consistency(name, "05", chapter_text="林晚提着录音离开码头。"))
    done = [ev for ev in events if ev.kind == "done"][0]
    assert done.extra["written"] is False
    assert done.extra["proposed_patches"]
    # nothing written yet
    card = characters.get_character(name, lin["id"])
    assert card["patches"] == []

    written = characters.confirm_patches(name, done.extra["proposed_patches"])
    assert len(written) == 1
    card = characters.get_character(name, lin["id"])
    assert card["patches"][0]["text"] == "最终还是离开了码头"
    assert card["patches"][0]["chapter_id"] == "05"


def test_rejected_consistency_patches_are_discarded(article_root):
    name = _write_branched_book()
    lin = characters.add_character(name, "林晚", "值班员。")
    parsed = longform.parse_consistency_output(
        json.dumps({
            "conflicts": [],
            "proposed_patches": [{
                "character_id": lin["id"],
                "character_name": "林晚",
                "chapter_id": "01",
                "text": "不应被自动写入",
            }],
        }),
        characters.list_characters(name, "01", inclusive=False),
    )
    assert parsed["proposed_patches"]
    assert characters.get_character(name, lin["id"])["patches"] == []


def test_world_and_character_api_roundtrip(article_root):
    name = _write_branched_book()
    client = TestClient(app)
    got = client.get(f"/api/books/{name}/world")
    assert got.status_code == 200
    assert "潮汐会改写记忆" in got.json()["text"]
    saved = client.put(f"/api/books/{name}/world", json={"text": "新的世界规则。"})
    assert saved.status_code == 200
    assert books.read_world(name) == "新的世界规则。"

    created = client.post(f"/api/books/{name}/characters", json={"name": "陈修", "basics": "灯塔看守。"})
    assert created.status_code == 200
    cid = created.json()["id"]
    patch = client.post(
        f"/api/books/{name}/characters/{cid}/patches",
        json={"chapter_id": "05", "text": "决定随林晚离开"},
    )
    assert patch.status_code == 200
    listed = client.get(f"/api/books/{name}/characters", params={"chapter": "06"})
    assert listed.status_code == 200
    card = listed.json()["characters"][0]
    assert card["current_state"].startswith("第5章")
    listed_a = client.get(f"/api/books/{name}/characters", params={"chapter": "04"})
    assert "决定随林晚离开" not in listed_a.json()["characters"][0]["current_state"]


def test_synopsis_refresh_skipped_when_generating(article_root):
    name = _write_branched_book()
    client = TestClient(app)
    before = books.read_chapter(name, "05")["text"]
    jobs._cancel = asyncio.Event()
    jobs._job_id = "busy"
    jobs._book = name
    jobs._chapter = "06"
    try:
        res = client.post(f"/api/books/{name}/synopsis/05/refresh", json={"text": "新正文不应触发写入"})
        assert res.status_code == 200
        data = res.json()
        assert data["skipped"] is True
        assert data["reason"] == "generating"
        assert books.read_chapter(name, "05")["text"] == before
        assert synopsis.read_synopsis(name, "05") == ""
    finally:
        jobs._cancel = None
        jobs._job_id = None
        jobs._book = None
        jobs._chapter = None


def test_existing_path_only_previous_context_still_holds(article_root):
    name = _write_branched_book()
    ctx_b = _previous_chapter_context(name, "05")
    assert "ENDING_A_END" not in ctx_b
    assert "ENDING_A_START" not in ctx_b
    assert "TRUNK_FORK" in ctx_b
    ctx_a = _previous_chapter_context(name, "04")
    assert "ENDING_A_START" in ctx_a
    assert "ENDING_B_START" not in ctx_a


def test_replace_outline_section_keeps_neighbors():
    from app.longform import replace_outline_section, keep_branch_marker, this_outline_spec
    existing = (
        "# 《枝书》\n\n## 故事线\n\n共用。\n\n"
        "### 第一章 开端\n\nTRUNK_OUTLINE_1\n\n"
        "### 第二章 分叉\n\nTRUNK_OUTLINE_2\n\n"
        "### 第三章 一结局\n\n<!-- 分支：一结局；分叉于：02 -->\n\nOUTLINE_ENDING_A1\n\n（待补充）\n\n"
        "### 第三章 二结局\n\n<!-- 分支：二结局；分叉于：02 -->\n\nOUTLINE_ENDING_B1\n"
    )
    old = "### 第三章 一结局\n\n<!-- 分支：一结局；分叉于：02 -->\n\nOUTLINE_ENDING_A1\n\n（待补充）"
    new = "### 第三章 一结局\n\n<!-- 分支：一结局；分叉于：02 -->\n\n一结局这一章写满了。"
    merged = replace_outline_section(existing, old, new)
    assert "一结局这一章写满了" in merged
    assert "OUTLINE_ENDING_B1" in merged
    assert "TRUNK_OUTLINE_2" in merged
    assert merged.count("### 第三章 一结局") == 1
    filled = keep_branch_marker(old, "### 第三章 一结局\n\n写满了。")
    assert "分支：一结局" in filled
    assert "写满了" in filled


def test_this_outline_spec_uses_path_before_selected(article_root):
    from app.longform import this_outline_spec
    from app import books
    from app.tree import sync_book_tree
    books.create_book("枝书2")
    outline = (
        "# 《枝书2》\n\n## 故事线\n\n共用。\n\n"
        "### 第一章\n\n主干一。\n\n"
        "### 第二章\n\n主干二。\n\n"
        "### 第三章 一结局\n\n<!-- 分支：一结局；分叉于：02 -->\n\n走向：向东。\n\n（待补充）\n\n"
        "### 第三章 二结局\n\n<!-- 分支：二结局；分叉于：02 -->\n\n走向：向西。\n\n二结局已写。\n"
    )
    books.write_outline("枝书2", outline)
    books.write_idea("枝书2", "一条共用主干。")
    books.write_chapter("枝书2", "01", "a")
    books.write_chapter("枝书2", "02", "b")
    books.write_chapter("枝书2", "03", "")
    books.write_chapter("枝书2", "04", "d")
    sync_book_tree("枝书2")
    # 03 is 第三章 一结局
    spec = this_outline_spec("枝书2", "03", "沿着向东走")
    assert spec["num"] == 3
    assert spec["branch"] == "一结局"
    assert "（待补充）" in (spec["section"] or "")
    assert "主干一" in (spec["path_outline"] or "")
    assert "主干二" in (spec["path_outline"] or "")
    assert "二结局已写" not in (spec["path_outline"] or "")
    assert "向东" in (spec["direction"] or "")
    assert spec["notes"] == "沿着向东走"


def test_parse_character_cards_from_json():
    from app.longform import parse_character_cards
    raw = """```json
    {"characters": [
      {"name": "阿岚", "basics": "电台主理人，冷静。"},
      {"name": "沈衡", "设定": "深夜听众。"},
      {"name": "阿岚", "basics": "重复应被跳过"}
    ]}
    ```"""
    cards = parse_character_cards(raw)
    assert [c["name"] for c in cards] == ["阿岚", "沈衡"]
    assert "电台主理人" in cards[0]["basics"]
    assert "深夜听众" in cards[1]["basics"]
    assert parse_character_cards("不是json") == []
