from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import books, characters, illustrations, modelswap
from app.generate import chapter_text_for_llm, check_consistency, generate_chapter
from app.llm import ChatResult, StreamEvent
from app.main import app, jobs
from app.textutil import (
    count_visible_hanzi,
    find_illustration_ids,
    illustration_marker,
    strip_illustration_markers,
)


@pytest.fixture
def client(article_root):
    return TestClient(app)


def test_marker_strip_and_insert():
    marker = illustration_marker("i0001")
    text = f"窗边只剩一层薄暮。{marker}她没有回头。"
    assert marker in text
    stripped = strip_illustration_markers(text)
    assert marker not in stripped
    assert "窗边只剩一层薄暮。" in stripped
    assert "她没有回头。" in stripped
    assert find_illustration_ids(text) == ["i0001"]
    assert chapter_text_for_llm(text) == stripped.strip()
    assert count_visible_hanzi(text) == count_visible_hanzi(stripped)
    assert count_visible_hanzi(text) == count_visible_hanzi("窗边只剩一层薄暮。她没有回头。")


def test_appearance_field_roundtrip(article_root):
    books.create_book("外形书")
    item = characters.add_character("外形书", "林晚", "潮汐电台的守夜人", "短发，深青色大衣，左耳一枚小银环")
    assert item["appearance"] == "短发，深青色大衣，左耳一枚小银环"
    loaded = characters.list_characters("外形书")
    assert loaded[0]["appearance"] == "短发，深青色大衣，左耳一枚小银环"
    updated = characters.update_character("外形书", item["id"], appearance="改成束发，旧羊毛围巾")
    assert updated["appearance"] == "改成束发，旧羊毛围巾"
    assert updated["basics"] == "潮汐电台的守夜人"
    # writing prompt includes appearance lightly, and does not error when missing
    prompt = characters.format_character_prompt(characters.list_characters("外形书"))
    assert "外形" in prompt
    assert "旧羊毛围巾" in prompt
    other = characters.add_character("外形书", "陈朔", "旧友")
    assert other["appearance"] == ""
    with pytest.raises(books.BookError, match="已有同名人物"):
        characters.add_character("外形书", "林晚", "另一个")


def test_appearance_api_roundtrip(client, article_root):
    books.create_book("接口外形")
    created = client.post(
        "/api/books/接口外形/characters",
        json={"name": "林晚", "basics": "守夜人", "appearance": "短发，青大衣"},
    )
    assert created.status_code == 200
    cid = created.json()["id"]
    listed = client.get("/api/books/接口外形/characters")
    assert listed.status_code == 200
    assert listed.json()["characters"][0]["appearance"] == "短发，青大衣"
    updated = client.put(
        f"/api/books/接口外形/characters/{cid}",
        json={"appearance": "改成束发"},
    )
    assert updated.status_code == 200
    assert updated.json()["appearance"] == "改成束发"
    assert updated.json()["basics"] == "守夜人"


def test_underage_sexual_refusal_does_not_generate(client, article_root, monkeypatch):
    books.create_book("安全书")
    books.write_outline("安全书", "# 《安全书》\n\n### 第一章 开端\n\n开始。\n")
    called = {"swap": 0, "image": 0}

    async def fake_swap(llm_url=None):
        called["swap"] += 1
        return {"writing_model": False, "comfyui": True, "illustration_session": True}

    async def fake_image(prompt, **kwargs):
        called["image"] += 1
        return b"PNG", "fake"

    monkeypatch.setattr(modelswap, "ensure_image_model", fake_swap)
    monkeypatch.setattr(modelswap, "generate_image", fake_image)

    source = "十六岁的她被按在床上，开始了性交。"
    prompt = "一个未成年少女的裸体，正在做爱。"
    res = client.post(
        "/api/illustrations/安全书/generate",
        json={"chapter": "01", "prompt": prompt, "source_excerpt": source},
    )
    assert res.status_code == 400
    assert "18" in res.json()["detail"]
    assert called["swap"] == 0
    assert called["image"] == 0

    # Adult sexual content is allowed to reach generate (still mocked).
    adult = client.post(
        "/api/illustrations/安全书/generate",
        json={
            "chapter": "01",
            "prompt": "两名明确为成年的恋人在昏暗卧室里拥抱亲吻，衣衫不整。",
            "source_excerpt": "二十八岁的她把丈夫拉到床边。",
        },
    )
    assert adult.status_code == 200
    assert called["swap"] == 1
    assert called["image"] == 1
    item = adult.json()
    assert item["id"].startswith("i")
    path = illustrations.image_path("安全书", item)
    assert path.exists()


def test_describe_and_generate_validation(client, article_root, monkeypatch):
    books.create_book("校验书")
    books.write_outline("校验书", "# 《校验书》\n\n### 第一章 开端\n\n开始。\n")

    empty = client.post("/api/illustrations/校验书/describe", json={"chapter": "01", "text": "   "})
    assert empty.status_code == 400
    assert "正文" in empty.json()["detail"]

    missing_prompt = client.post(
        "/api/illustrations/校验书/generate",
        json={"chapter": "01", "prompt": "  "},
    )
    assert missing_prompt.status_code == 400
    assert "画面说明" in missing_prompt.json()["detail"]

    async def fake_chat(*args, **kwargs):
        return ChatResult(content="暮色里的窗台，短发女人披着深青大衣，侧脸对着潮水。")

    monkeypatch.setattr("app.illustrations.chat", fake_chat) if False else None
    monkeypatch.setattr("app.llm.chat", fake_chat)

    described = client.post(
        "/api/illustrations/校验书/describe",
        json={"chapter": "01", "text": "林晚站在窗边，看潮水一遍遍拍上堤岸。"},
    )
    assert described.status_code == 200
    assert "窗台" in described.json()["prompt"] or "潮" in described.json()["prompt"]

    # If an illustration session is already active, describe must refuse.
    modelswap.state.session_active = True
    try:
        blocked = client.post(
            "/api/illustrations/校验书/describe",
            json={"chapter": "01", "text": "林晚站在窗边。"},
        )
        assert blocked.status_code == 400
        assert "出图结束" in blocked.json()["detail"]
    finally:
        modelswap.state.session_active = False

    # Busy writing job blocks generate (and must not swap).
    called = {"swap": 0}

    async def fake_swap(llm_url=None):
        called["swap"] += 1
        return {}

    monkeypatch.setattr(modelswap, "ensure_image_model", fake_swap)
    jobs._cancel = asyncio.Event()
    jobs._job_id = "busy"
    jobs._book = "校验书"
    jobs._chapter = "01"
    try:
        busy = client.post(
            "/api/illustrations/校验书/generate",
            json={"chapter": "01", "prompt": "窗边的暮色，安静的潮声。"},
        )
        assert busy.status_code == 400
        assert "正文" in busy.json()["detail"]
        assert called["swap"] == 0
    finally:
        jobs.finish("busy")


def test_generate_and_revise_prompts_strip_markers(article_root, monkeypatch):
    books.create_book("标记书")
    books.write_idea("标记书", "海边的故事")
    books.write_outline(
        "标记书",
        "# 《标记书》\n\n### 第一章 开端\n\n窗边的黄昏。\n",
    )
    marker = illustration_marker("i0001")
    body = f"窗边只剩一层薄暮。{marker}她没有回头，把灯捻得更暗。"
    books.write_chapter("标记书", "01", body + "\n")

    captured: list[list[dict]] = []

    async def fake_stream(messages, **kwargs):
        captured.append(messages)
        yield StreamEvent(kind="done", result=ChatResult(content="她把灯捻得更暗，房间里只剩下潮声。\n"), text="她把灯捻得更暗，房间里只剩下潮声。\n")

    monkeypatch.setattr("app.generate.stream_chat_with_retry", fake_stream)

    async def run_revise():
        events = []
        async for event in generate_chapter(
            "标记书",
            "01",
            mode="revise",
            revision="把结尾再收一点。",
            chapter_text=body,
            enable_thinking=False,
        ):
            events.append(event)
        return events

    asyncio.run(run_revise())
    assert captured
    blob = "\n".join(msg.get("content", "") for msgs in captured for msg in msgs)
    assert "【插图:" not in blob
    assert "i0001" not in blob
    assert "窗边只剩一层薄暮" in blob
    assert "她没有回头" in blob

    captured.clear()

    async def run_consistency():
        events = []
        async for event in check_consistency(
            "标记书",
            "01",
            chapter_text=body,
            enable_thinking=False,
        ):
            events.append(event)
        return events

    # Consistency refuses without world/characters. Add a world so the prompt is built.
    books.write_world("标记书", "一座临海小城，潮汐按时刻到来。")
    asyncio.run(run_consistency())
    assert captured
    blob = "\n".join(msg.get("content", "") for msgs in captured for msg in msgs)
    assert "【插图:" not in blob
    assert "窗边只剩一层薄暮" in blob


def test_list_insert_delete_illustration(article_root):
    books.create_book("插图书")
    books.write_chapter("插图书", "01", "原文第一句。\n")
    item = illustrations.save_illustration(
        "插图书",
        "01",
        b"\x89PNG-fake",
        prompt="窗边的暮色",
        source_excerpt="原文第一句。",
    )
    listed = illustrations.list_illustrations("插图书", "01")
    assert len(listed) == 1
    assert listed[0]["id"] == item["id"]
    assert listed[0]["inserted"] is False
    info = illustrations.insert_marker_info("插图书", item["id"])
    assert info["marker"] == illustration_marker(item["id"])
    assert illustrations.get_illustration("插图书", item["id"])["inserted"] is True

    marker = info["marker"]
    books.write_chapter("插图书", "01", f"原文第一句。{marker}下一段。\n")
    deleted = illustrations.delete_illustration("插图书", item["id"])
    assert deleted["ok"] is True
    assert marker not in (deleted["text"] or "")
    assert "原文第一句。" in deleted["text"]
    assert illustrations.list_illustrations("插图书", "01") == []


def test_builtin_workflow_is_qwen2511_txt2img():
    graph, source = modelswap.resolve_workflow("窗边的暮色，短发女人披着青大衣。", seed=42)
    assert source == "builtin-qwen-image-edit-2511-t2i"
    types = {node["class_type"] for node in graph.values()}
    assert "TextEncodeQwenImageEditPlus" in types
    assert "UNETLoader" in types
    assert "CLIPLoader" in types
    assert "VAELoader" in types
    assert "KSampler" in types
    assert "LoadImage" not in types
    encode = next(
        node
        for node in graph.values()
        if node["class_type"] == "TextEncodeQwenImageEditPlus"
        and node.get("_meta", {}).get("title") == "Positive"
    )
    assert encode["inputs"]["prompt"] == "窗边的暮色，短发女人披着青大衣。"
    clip = next(node for node in graph.values() if node["class_type"] == "CLIPLoader")
    assert clip["inputs"]["clip_name"] == "qwen_2.5_vl_7b_fp8_scaled.safetensors"
    assert clip["inputs"]["type"] == "qwen_image"


def test_configured_img2img_workflow_is_rejected(tmp_path, monkeypatch):
    path = tmp_path / "Qwen_Image_2.1_Image_to_Image.json"
    path.write_text(
        """{
          "1": {"class_type": "LoadImage", "inputs": {"image": "source.png"}},
          "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "edit this", "clip": ["3", 0]}},
          "3": {"class_type": "CLIPLoader", "inputs": {"clip_name": "x", "type": "qwen_image"}},
          "4": {"class_type": "KSampler", "inputs": {"seed": 1, "latent_image": ["1", 0]}}
        }""",
        encoding="utf-8",
    )
    monkeypatch.setenv("NOVEL_WRITER_COMFY_WORKFLOW", str(path))
    with pytest.raises(books.BookError, match="图生图"):
        modelswap.resolve_workflow("任何说明", seed=1)


def test_chapter_wordcount_ignores_markers(article_root):
    books.create_book("字数书")
    marker = illustration_marker("i0001")
    body = f"窗边只剩一层薄暮。{marker}她没有回头。"
    written = books.write_chapter("字数书", "01", body)
    plain = books.write_chapter  # noqa: F841
    expected = count_visible_hanzi("窗边只剩一层薄暮。她没有回头。")
    assert written["hanzi"] == expected
    assert books.read_chapter("字数书", "01")["hanzi"] == expected
    assert books.list_chapters("字数书")[0]["hanzi"] == expected


def test_models_status_endpoint(client, article_root, monkeypatch):
    monkeypatch.setattr(modelswap, "llama_is_up", lambda llm_url=None: True)
    monkeypatch.setattr(modelswap, "comfy_is_up", lambda: False)
    modelswap.state.session_active = False
    res = client.get("/api/models/status")
    assert res.status_code == 200
    data = res.json()
    assert data["writing_model"] is True
    assert data["comfyui"] is False
    assert data["illustration_session"] is False
    assert data["writing_busy"] is False
