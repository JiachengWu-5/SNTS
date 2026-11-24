"""FastAPI app: JSON APIs plus static frontend, bound to 127.0.0.1 only."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import books, style
from app.config import DEFAULT_LLM_URL, HOST, PORT
from app.generate import (
    check_consistency,
    generate_chapter,
    generate_chapters_until_end,
    generate_next_outline_chapter,
    generate_outline_until_end,
    generate_this_outline_chapter,
    generate_outline,
    generate_world_draft,
    generate_character_cards,
    refresh_path_synopsis,
)
from app.llm import check_health
from app.style import StyleExistsError, StyleSafetyError
from app import webbook
from app.webstyle import StyleFetchError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("novel_writer")

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR.parent / "static"
APP_STARTED_AT = int(time.time())

app = FastAPI(title="本地小说写作", version="0.1.0")


class CreateBookIn(BaseModel):
    name: str


class TextIn(BaseModel):
    text: str = ""


class SettingsIn(BaseModel):
    llm_url: str | None = None
    temperature: float | None = None
    chapter_target_chars: int | None = None
    enable_thinking: bool | None = None
    selected_styles: list[str] | None = None


class StylePasteIn(BaseModel):
    text: str
    filename: str | None = None


class StyleFromUrlIn(BaseModel):
    url: str
    force: bool = False


class AddChapterIn(BaseModel):
    title: str = ""


class AddBranchIn(BaseModel):
    from_chapter: str
    branch_name: str
    idea: str = ""
    count: int = 1


class OutlineGenerateIn(BaseModel):
    idea: str | None = None
    revision: str | None = None
    outline: str | None = None
    style_ids: list[str] | None = None
    temperature: float | None = None
    enable_thinking: bool | None = True
    llm_url: str | None = None
    from_chapter: str | None = None
    chapter_id: str | None = None
    notes: str | None = None
    target_chapters: int | None = None
    target_book_chars: int | None = None


class WorldGenerateIn(BaseModel):
    idea: str | None = None
    temperature: float | None = None
    enable_thinking: bool | None = True
    llm_url: str | None = None


class CharacterIn(BaseModel):
    name: str
    basics: str = ""
    appearance: str = ""


class CharacterUpdateIn(BaseModel):
    name: str | None = None
    basics: str | None = None
    appearance: str | None = None


class PatchIn(BaseModel):
    chapter_id: str
    text: str


class PatchUpdateIn(BaseModel):
    chapter_id: str | None = None
    text: str | None = None


class ConfirmPatchesIn(BaseModel):
    patches: list[dict] = Field(default_factory=list)


class ConsistencyIn(BaseModel):
    chapter: str
    text: str | None = None
    temperature: float | None = None
    enable_thinking: bool | None = True
    llm_url: str | None = None


class ChaptersUntilIn(BaseModel):
    from_chapter: str | None = None
    target_chapters: int | None = None
    target_book_chars: int | None = None
    style_ids: list[str] | None = None
    temperature: float | None = None
    enable_thinking: bool | None = None
    target_chars: int | None = None
    llm_url: str | None = None
    outline: str | None = None


class ChapterGenerateIn(BaseModel):
    chapter: str
    mode: str = Field(default="generate")
    style_ids: list[str] | None = None
    temperature: float | None = None
    enable_thinking: bool | None = None
    target_chars: int | None = None
    llm_url: str | None = None
    outline: str | None = None
    revision: str | None = None
    text: str | None = None


class IllustrateDescribeIn(BaseModel):
    chapter: str
    text: str = ""
    llm_url: str | None = None
    temperature: float | None = None


class IllustrateGenerateIn(BaseModel):
    chapter: str
    prompt: str
    source_excerpt: str = ""
    llm_url: str | None = None


class IllustrateFinishIn(BaseModel):
    llm_url: str | None = None


class JobManager:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._job_id: str | None = None
        self._cancel: asyncio.Event | None = None
        self._book: str | None = None
        self._chapter: str | None = None

    async def start(self, *, book: str | None = None, chapter: str | None = None) -> tuple[str, asyncio.Event]:
        async with self._lock:
            if self._cancel is not None:
                self._cancel.set()
            job_id = uuid.uuid4().hex[:12]
            event = asyncio.Event()
            self._job_id = job_id
            self._cancel = event
            self._book = book
            self._chapter = chapter
            return job_id, event

    def cancel(self, job_id: str | None = None) -> bool:
        if self._cancel is None:
            return False
        if job_id and self._job_id and job_id != self._job_id:
            return False
        self._cancel.set()
        return True

    def set_chapter(self, chapter: str | None) -> None:
        self._chapter = chapter

    def finish(self, job_id: str) -> None:
        if self._job_id == job_id:
            self._job_id = None
            self._cancel = None
            self._book = None
            self._chapter = None

    def is_chapter_generating(self, book: str, chapter_id: str) -> bool:
        if self._cancel is None or not self._chapter or not book:
            return False
        if self._book != book:
            return False
        try:
            current = books.validate_chapter_id(self._chapter)
            wanted = books.validate_chapter_id(chapter_id)
        except books.BookError:
            return str(self._chapter) == str(chapter_id)
        return current == wanted

    def is_busy(self) -> bool:
        return self._cancel is not None

    @property
    def current(self) -> str | None:
        return self._job_id


jobs = JobManager()


def _book_exc(exc: Exception) -> HTTPException:
    if isinstance(exc, books.BookError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, StyleExistsError):
        return HTTPException(status_code=409, detail={"code": "exists", "name": exc.name, "message": str(exc)})
    if isinstance(exc, (StyleSafetyError, StyleFetchError)):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc


@app.get("/api/health")
async def api_health(llm_url: str | None = None):
    llm = await check_health(llm_url or DEFAULT_LLM_URL)
    return {
        "ok": True,
        "app": "novel-writer",
        "host": HOST,
        "port": PORT,
        "llm": llm,
        "started_at": APP_STARTED_AT,
    }


@app.get("/api/books")
def api_list_books():
    return {"books": books.list_books()}


@app.post("/api/books")
def api_create_book(body: CreateBookIn):
    try:
        return books.create_book(body.name)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}")
def api_get_book(name: str):
    try:
        return books.get_book(name)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/idea")
def api_get_idea(name: str):
    try:
        text = books.read_idea(name)
        from app.textutil import count_hanzi

        return {"text": text, "hanzi": count_hanzi(text)}
    except books.BookError as exc:
        raise _book_exc(exc)


@app.put("/api/books/{name}/idea")
def api_put_idea(name: str, body: TextIn):
    try:
        return books.write_idea(name, body.text)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/outline")
def api_get_outline(name: str):
    try:
        text = books.read_outline(name)
        from app.textutil import count_hanzi

        return {"text": text, "hanzi": count_hanzi(text)}
    except books.BookError as exc:
        raise _book_exc(exc)


@app.put("/api/books/{name}/outline")
def api_put_outline(name: str, body: TextIn):
    try:
        return books.write_outline(name, body.text)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/outline/restore")
def api_restore_outline(name: str):
    try:
        return books.restore_previous_outline(name)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/outline/history")
def api_list_outline_history(name: str):
    try:
        from app import history
        return history.list_outline_history(name)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/outline/save")
def api_save_outline(name: str, body: TextIn | None = None):
    try:
        from app import history
        text = body.text if body is not None else None
        return history.save_outline_snapshot(name, text)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/outline/history/{entry_id}")
def api_get_outline_history_entry(name: str, entry_id: str):
    try:
        from app import history
        return history.read_outline_entry(name, entry_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/outline/history/{entry_id}/restore")
def api_restore_outline_history_entry(name: str, entry_id: str):
    try:
        from app import history
        return history.restore_outline_entry(name, entry_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/world")
def api_get_world(name: str):
    try:
        text = books.read_world(name)
        from app.textutil import count_hanzi

        return {"text": text, "hanzi": count_hanzi(text)}
    except books.BookError as exc:
        raise _book_exc(exc)


@app.put("/api/books/{name}/world")
def api_put_world(name: str, body: TextIn):
    try:
        return books.write_world(name, body.text)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/characters")
def api_list_characters(name: str, chapter: str | None = None, inclusive: bool = False):
    try:
        from app import characters as character_store

        books.ensure_book(name)
        items = character_store.list_characters(name, chapter, inclusive=inclusive)
        return {"characters": items}
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/characters")
def api_add_character(name: str, body: CharacterIn):
    try:
        from app import characters as character_store

        item = character_store.add_character(name, body.name, body.basics, body.appearance)
        return item
    except books.BookError as exc:
        raise _book_exc(exc)


@app.put("/api/books/{name}/characters/{character_id}")
def api_update_character(name: str, character_id: str, body: CharacterUpdateIn):
    try:
        from app import characters as character_store

        return character_store.update_character(
            name, character_id, char_name=body.name, basics=body.basics, appearance=body.appearance
        )
    except books.BookError as exc:
        raise _book_exc(exc)


@app.delete("/api/books/{name}/characters/{character_id}")
def api_delete_character(name: str, character_id: str):
    try:
        from app import characters as character_store

        return character_store.delete_character(name, character_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/characters/{character_id}/patches")
def api_add_patch(name: str, character_id: str, body: PatchIn):
    try:
        from app import characters as character_store

        return character_store.add_patch(name, character_id, body.chapter_id, body.text)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.put("/api/books/{name}/characters/{character_id}/patches/{patch_id}")
def api_update_patch(name: str, character_id: str, patch_id: str, body: PatchUpdateIn):
    try:
        from app import characters as character_store

        return character_store.update_patch(
            name, character_id, patch_id, text=body.text, chapter_id=body.chapter_id
        )
    except books.BookError as exc:
        raise _book_exc(exc)


@app.delete("/api/books/{name}/characters/{character_id}/patches/{patch_id}")
def api_delete_patch(name: str, character_id: str, patch_id: str):
    try:
        from app import characters as character_store

        return character_store.delete_patch(name, character_id, patch_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/characters/patches/confirm")
def api_confirm_patches(name: str, body: ConfirmPatchesIn):
    try:
        from app import characters as character_store

        written = character_store.confirm_patches(name, body.patches)
        chapter = ""
        if body.patches:
            chapter = str(body.patches[0].get("chapter_id") or "") or None
        items = character_store.list_characters(name, chapter, inclusive=True) if chapter else character_store.list_characters(name)
        return {"ok": True, "written": written, "characters": items}
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/synopsis")
def api_get_synopsis(name: str, chapter: str):
    try:
        from app import synopsis as synopsis_store

        books.ensure_book(name)
        text = synopsis_store.read_synopsis(name, chapter)
        from app.textutil import count_hanzi

        nearest, source = synopsis_store.nearest_path_synopsis(name, chapter, include_self=True)
        return {"text": text, "hanzi": count_hanzi(text), "nearest": nearest, "source": source, "chapter_id": chapter}
    except books.BookError as exc:
        raise _book_exc(exc)


@app.put("/api/books/{name}/synopsis/{chapter_id}")
def api_put_synopsis(name: str, chapter_id: str, body: TextIn):
    try:
        from app import synopsis as synopsis_store

        return synopsis_store.write_synopsis(name, chapter_id, body.text)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/settings")
def api_get_settings(name: str):
    try:
        return books.load_settings(name)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.put("/api/books/{name}/settings")
def api_put_settings(name: str, body: SettingsIn):
    try:
        payload = {k: v for k, v in body.model_dump().items() if v is not None}
        return books.save_settings(name, payload)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/chapters")
def api_list_chapters(name: str):
    try:
        return {"chapters": books.list_chapters(name)}
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/chapters")
def api_add_chapter(name: str, body: AddChapterIn):
    try:
        return books.add_chapter_slot(name, body.title)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/chapters/{chapter_id}")
def api_get_chapter(name: str, chapter_id: str):
    try:
        return books.read_chapter(name, chapter_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.put("/api/books/{name}/chapters/{chapter_id}")
def api_put_chapter(name: str, chapter_id: str, body: TextIn):
    try:
        return books.write_chapter(name, chapter_id, body.text)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/chapters/{chapter_id}/restore")
def api_restore_chapter(name: str, chapter_id: str):
    try:
        return books.restore_previous_chapter(name, chapter_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/chapters/{chapter_id}/save")
def api_save_chapter(name: str, chapter_id: str, body: TextIn | None = None):
    try:
        text = body.text if body is not None else None
        return books.save_chapter_snapshot(name, chapter_id, text)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/chapters/{chapter_id}/restore-saved")
def api_restore_saved_chapter(name: str, chapter_id: str):
    try:
        return books.restore_saved_chapter(name, chapter_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/chapters/{chapter_id}/history")
def api_list_chapter_history(name: str, chapter_id: str):
    try:
        from app import history
        return history.list_chapter_history(name, chapter_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/chapters/{chapter_id}/history/{entry_id}")
def api_get_chapter_history_entry(name: str, chapter_id: str, entry_id: str):
    try:
        from app import history
        return history.read_chapter_entry(name, chapter_id, entry_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/chapters/{chapter_id}/history/{entry_id}/restore")
def api_restore_chapter_history_entry(name: str, chapter_id: str, entry_id: str):
    try:
        from app import history
        return history.restore_chapter_entry(name, chapter_id, entry_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/tree")
def api_get_tree(name: str):
    try:
        from app.tree import get_book_tree

        return get_book_tree(name)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/tree/branches")
def api_add_branch(name: str, body: AddBranchIn):
    try:
        from app.tree import add_ending_branch

        return add_ending_branch(
            name,
            body.from_chapter,
            body.branch_name,
            idea=body.idea or "",
            chapter_count=body.count,
        )
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/tree/nodes/{node_id}/open")
def api_open_tree_node(name: str, node_id: str):
    try:
        from app.tree import open_tree_node

        return open_tree_node(name, node_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.delete("/api/books/{name}/chapters/{chapter_id}")
def api_delete_chapter(name: str, chapter_id: str):
    try:
        if jobs.is_chapter_generating(name, chapter_id):
            raise HTTPException(status_code=400, detail="本章正在生成，请等生成结束后再删除")
        return books.delete_chapter(name, chapter_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/books/{name}/styles")
def api_list_styles(name: str):
    try:
        books.ensure_book(name)
        return style.list_samples(name)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/styles")
def api_paste_style(name: str, body: StylePasteIn):
    try:
        return style.save_pasted_style(name, body.text, body.filename)
    except (books.BookError, StyleSafetyError) as exc:
        raise _book_exc(exc)




@app.post("/api/books/{name}/styles/from-url")
async def api_style_from_url(name: str, body: StyleFromUrlIn):
    try:
        books.ensure_book(name)
        return await style.save_style_from_url(name, body.url, force=body.force)
    except (books.BookError, StyleSafetyError, StyleFetchError, StyleExistsError) as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/styles/from-book-url")
async def api_style_from_book_url(name: str, body: StyleFromUrlIn):
    try:
        books.ensure_book(name)
    except books.BookError as exc:
        raise _book_exc(exc)

    async def events():
        try:
            result = None
            async for event in webbook.iter_extract_all(body.url):
                kind = event.get("kind")
                if kind == "status":
                    yield _sse("status", {"message": event.get("message") or "正在抓取…"})
                elif kind == "catalog":
                    from app.config import template_dir

                    existing = template_dir() / f"{event['book_name']}.txt"
                    if existing.exists() and not body.force:
                        yield _sse(
                            "exists",
                            {
                                "name": existing.name,
                                "message": f"文风样本「{existing.name}」已存在，要覆盖吗？",
                                "book_name": event["book_name"],
                                "total": event.get("total") or 0,
                            },
                        )
                        return
                    yield _sse(
                        "status",
                        {"message": f"正在抓取《{event['book_name']}》，共 {event.get('total') or 0} 章…"},
                    )
                elif kind == "progress":
                    yield _sse(
                        "status",
                        {
                            "message": f"正在抓取 {event.get('current')}/{event.get('total')}：{event.get('title') or ''}",
                            "current": event.get("current"),
                            "total": event.get("total"),
                        },
                    )
                elif kind == "result":
                    result = event
            if result:
                saved = style.save_template_book(result, force=body.force)
                yield _sse("done", saved)
        except StyleExistsError as exc:
            yield _sse("exists", {"name": exc.name, "message": str(exc)})
        except (books.BookError, StyleSafetyError, StyleFetchError) as exc:
            yield _sse("error", {"message": str(exc)})
        except Exception:
            logger.exception("whole-book style fetch failed")
            yield _sse("error", {"message": "抓取全本时出错，请稍后重试"})

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/styles/upload")
async def api_upload_style(name: str, file: UploadFile = File(...)):
    try:
        data = await file.read()
        return style.save_uploaded_style(name, file.filename or "upload.txt", data)
    except (books.BookError, StyleSafetyError) as exc:
        raise _book_exc(exc)


@app.post("/api/books/{name}/generate/outline")
async def api_generate_outline(name: str, body: OutlineGenerateIn):
    job_id, cancel = await jobs.start(book=name)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "outline"})
            async for event in generate_outline(
                name,
                idea=body.idea,
                revision=body.revision,
                outline=body.outline,
                style_ids=body.style_ids,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                llm_url=body.llm_url,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("outline generation failed")
            yield _sse("error", {"message": "生成大纲时出错，请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/generate/outline-next")
async def api_generate_next_outline(name: str, body: OutlineGenerateIn):
    job_id, cancel = await jobs.start(book=name)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "outline-next"})
            async for event in generate_next_outline_chapter(
                name,
                idea=body.idea,
                outline=body.outline,
                from_chapter=body.from_chapter,
                style_ids=body.style_ids,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                llm_url=body.llm_url,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("next outline generation failed")
            yield _sse("error", {"message": "写下一章大纲时出错，请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/generate/outline-until")
async def api_generate_outline_until(name: str, body: OutlineGenerateIn):
    job_id, cancel = await jobs.start(book=name)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "outline-until"})
            async for event in generate_outline_until_end(
                name,
                idea=body.idea,
                outline=body.outline,
                from_chapter=body.from_chapter,
                target_chapters=body.target_chapters,
                target_book_chars=body.target_book_chars,
                style_ids=body.style_ids,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                llm_url=body.llm_url,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("outline-until generation failed")
            yield _sse("error", {"message": "一键写完大纲时出错，已写好的部分都保留了。请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/generate/outline-section")
async def api_generate_this_outline(name: str, body: OutlineGenerateIn):
    job_id, cancel = await jobs.start(book=name)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "outline-section"})
            async for event in generate_this_outline_chapter(
                name,
                chapter_id=body.chapter_id or body.from_chapter,
                notes=body.notes or body.revision,
                idea=body.idea,
                outline=body.outline,
                style_ids=body.style_ids,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                llm_url=body.llm_url,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("this outline generation failed")
            yield _sse("error", {"message": "写本章大纲时出错，请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())



@app.post("/api/books/{name}/generate/characters")
async def api_generate_characters(name: str, body: WorldGenerateIn):
    job_id, cancel = await jobs.start(book=name)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "characters"})
            async for event in generate_character_cards(
                name,
                idea=body.idea,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                llm_url=body.llm_url,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("character generation failed")
            yield _sse("error", {"message": "生成人物时出错，请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/generate/world")
async def api_generate_world(name: str, body: WorldGenerateIn):
    job_id, cancel = await jobs.start(book=name)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "world"})
            async for event in generate_world_draft(
                name,
                idea=body.idea,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                llm_url=body.llm_url,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("world generation failed")
            yield _sse("error", {"message": "起草世界观时出错，请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/generate/consistency")
async def api_check_consistency(name: str, body: ConsistencyIn):
    job_id, cancel = await jobs.start(book=name, chapter=body.chapter)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "consistency", "chapter": body.chapter})
            async for event in check_consistency(
                name,
                body.chapter,
                chapter_text=body.text,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                llm_url=body.llm_url,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("consistency check failed")
            yield _sse("error", {"message": "检查一致性时出错，请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/synopsis/{chapter_id}/refresh")
async def api_refresh_synopsis(name: str, chapter_id: str, body: TextIn | None = None):
    if jobs.is_busy():
        return {"ok": False, "skipped": True, "reason": "generating"}
    job_id, cancel = await jobs.start(book=name, chapter=chapter_id)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "synopsis"})
            text = body.text if body is not None else None
            async for event in refresh_path_synopsis(
                name,
                chapter_id,
                chapter_text=text,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("synopsis refresh failed")
            yield _sse("error", {"message": "更新路径摘要时出错，请稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/generate/chapters-until")
async def api_generate_chapters_until(name: str, body: ChaptersUntilIn):
    job_id, cancel = await jobs.start(book=name)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "chapters-until"})
            async for event in generate_chapters_until_end(
                name,
                from_chapter=body.from_chapter,
                target_chapters=body.target_chapters,
                target_book_chars=body.target_book_chars,
                style_ids=body.style_ids,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                target_chars=body.target_chars,
                llm_url=body.llm_url,
                outline=body.outline,
                cancel_event=cancel,
                on_chapter=jobs.set_chapter,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("chapters-until generation failed")
            yield _sse("error", {"message": "一键写完正文时出错，已写好的部分都保留了。请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/books/{name}/generate/chapter")
async def api_generate_chapter(name: str, body: ChapterGenerateIn):
    job_id, cancel = await jobs.start(book=name, chapter=body.chapter)

    async def events():
        try:
            yield _sse("meta", {"job_id": job_id, "kind": "chapter", "chapter": body.chapter})
            async for event in generate_chapter(
                name,
                body.chapter,
                mode=body.mode,
                style_ids=body.style_ids,
                temperature=body.temperature,
                enable_thinking=body.enable_thinking,
                target_chars=body.target_chars,
                llm_url=body.llm_url,
                outline=body.outline,
                revision=body.revision,
                chapter_text=body.text,
                cancel_event=cancel,
            ):
                yield _event_to_sse(event)
        except Exception:
            logger.exception("chapter generation failed")
            yield _sse("error", {"message": "生成章节时出错，请查看服务窗口或稍后重试"})
        finally:
            jobs.finish(job_id)

    return StreamingResponse(events(), media_type="text/event-stream", headers=_sse_headers())


@app.post("/api/generate/cancel")
def api_cancel(job_id: str | None = None):
    ok = jobs.cancel(job_id)
    return {"ok": ok, "job_id": job_id or jobs.current}


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _event_to_sse(event) -> str:
    if event.kind == "text":
        return _sse("text", {"text": event.text})
    if event.kind == "thinking":
        return _sse("status", {"message": "正在思考…"})
    if event.kind == "status":
        payload = {"message": event.message}
        payload.update(event.extra or {})
        return _sse("status", payload)
    if event.kind == "cancelled":
        payload = {"message": event.message or "已停止生成"}
        payload.update(event.extra or {})
        if event.text:
            payload.setdefault("outline", event.text)
            payload.setdefault("text", event.text)
        return _sse("cancelled", payload)
    if event.kind == "error":
        return _sse("error", {"message": event.message})
    if event.kind == "chapter":
        payload = {"text": event.text}
        payload.update(event.extra or {})
        return _sse("chapter", payload)
    if event.kind == "done":
        payload = {"text": event.text, "hanzi": event.result.hanzi if event.result else 0}
        payload.update(event.extra or {})
        if event.result:
            payload["retried_without_thinking"] = event.result.retried_without_thinking
        return _sse("done", payload)
    return _sse("status", {"message": event.kind})


@app.get("/api/models/status")
async def api_models_status(llm_url: str | None = None):
    from app import modelswap

    status = modelswap.session_status(llm_url or DEFAULT_LLM_URL)
    status["writing_busy"] = jobs.is_busy()
    status["ok"] = True
    return status


@app.get("/api/books/{name}/export/pdf")
def api_export_book_pdf(name: str, chapter: str | None = None):
    """Standalone PDF export. Do not route this through the job manager."""
    from urllib.parse import quote

    from app import export_pdf

    try:
        path, download_name = export_pdf.export_book_pdf(name, chapter_id=chapter)
        headers = {
            "Content-Disposition": "attachment; filename=\"book.pdf\"; filename*=UTF-8''" + quote(download_name)
        }
        return FileResponse(path, media_type="application/pdf", headers=headers)
    except books.BookError as exc:
        raise _book_exc(exc)
    except Exception:
        logger.exception("book pdf export failed")
        raise HTTPException(status_code=400, detail="导出 PDF 时出错，请稍后重试")


@app.post("/api/books/{name}/export/pdf")
def api_export_book_pdf_post(name: str, chapter: str | None = None):
    return api_export_book_pdf(name, chapter)


@app.get("/api/illustrations/{name}")
def api_list_illustrations(name: str, chapter: str | None = None):
    from app import illustrations

    try:
        items = illustrations.list_illustrations(name, chapter)
        return {"illustrations": items, "chapter": chapter}
    except books.BookError as exc:
        raise _book_exc(exc)


@app.get("/api/illustrations/{name}/{illust_id}/image")
def api_get_illustration_image(name: str, illust_id: str):
    from app import illustrations

    try:
        item = illustrations.get_illustration(name, illust_id)
        path = illustrations.image_path(name, item)
        if not path.exists():
            raise HTTPException(status_code=404, detail="找不到该插图文件")
        return FileResponse(path, media_type="image/png", filename=item.get("filename") or f"{illust_id}.png")
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/illustrations/{name}/describe")
async def api_describe_illustration(name: str, body: IllustrateDescribeIn):
    from app import illustrations, modelswap

    if jobs.is_busy():
        raise HTTPException(status_code=400, detail="正在生成正文，请等结束后再写画面说明")
    if modelswap.state.session_active:
        raise HTTPException(
            status_code=400,
            detail="请先点「出图结束」，再让写作模型写新的画面说明。若只是改说明再出图，可以直接点「确认出图」。",
        )
    job_id, cancel = await jobs.start(book=name, chapter=body.chapter)
    try:
        return await illustrations.describe_scene(
            name,
            body.chapter,
            body.text,
            llm_url=body.llm_url,
            temperature=body.temperature,
            cancel_event=cancel,
        )
    except books.BookError as exc:
        raise _book_exc(exc)
    finally:
        jobs.finish(job_id)


@app.post("/api/illustrations/{name}/generate")
async def api_generate_illustration(name: str, body: IllustrateGenerateIn):
    from app import illustrations, modelswap

    if jobs.is_busy():
        raise HTTPException(status_code=400, detail="正在生成正文，请等结束后再出图")
    reason = illustrations.underage_sexual_refusal_reason(body.source_excerpt or "", body.prompt or "")
    if reason:
        raise HTTPException(status_code=400, detail=reason)
    prompt = (body.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="请先确认画面说明，再出图")
    if not (body.chapter or "").strip():
        raise HTTPException(status_code=400, detail="请先选择章节")
    # Safety already passed — only now may the GPU-resident model be swapped.
    job_id, cancel = await jobs.start(book=name, chapter=body.chapter)
    llm_url = body.llm_url or DEFAULT_LLM_URL
    try:
        modelswap.state.book = name
        await modelswap.ensure_image_model(llm_url)
        if cancel.is_set():
            raise HTTPException(status_code=400, detail="已停止出图")
        png, source = await modelswap.generate_image(prompt)
        item = illustrations.save_illustration(
            name,
            body.chapter,
            png,
            prompt=prompt,
            source_excerpt=body.source_excerpt or "",
        )
        item["workflow"] = source
        return item
    except Exception as exc:
        logger.exception("illustration generation failed")
        # If ComfyUI never became ready, put the writing model back.
        if not (modelswap.state.session_active and modelswap.comfy_is_up()):
            try:
                await modelswap.restore_writing_model(llm_url)
            except Exception:
                logger.exception("failed to restore writing model after illustration error")
        if isinstance(exc, books.BookError):
            raise _book_exc(exc)
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status_code=400, detail="出图时出错，请查看本机出图窗口或稍后重试") from exc
    finally:
        jobs.finish(job_id)


@app.post("/api/illustrations/{name}/finish-session")
async def api_finish_illustration_session(name: str, body: IllustrateFinishIn | None = None):
    from app import modelswap

    if jobs.is_busy() and modelswap.state.session_active:
        # A generate may still be running; do not yank the image model mid-job.
        raise HTTPException(status_code=400, detail="正在出图，请等这张完成后再结束出图")
    try:
        status = await modelswap.finish_session((body.llm_url if body else None) or DEFAULT_LLM_URL)
        return {"ok": True, **status}
    except books.BookError as exc:
        raise _book_exc(exc)
    except Exception:
        logger.exception("finish illustration session failed")
        raise HTTPException(status_code=400, detail="结束出图时未能重新加载写作模型，请稍后重试")


@app.delete("/api/illustrations/{name}/{illust_id}")
def api_delete_illustration(name: str, illust_id: str):
    from app import illustrations

    try:
        return illustrations.delete_illustration(name, illust_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/illustrations/{name}/{illust_id}/insert-marker")
def api_insert_illustration_marker(name: str, illust_id: str):
    from app import illustrations

    try:
        return illustrations.insert_marker_info(name, illust_id)
    except books.BookError as exc:
        raise _book_exc(exc)


@app.post("/api/server/reload")
def api_reload_server():
    if jobs.is_busy():
        raise HTTPException(status_code=409, detail="正在生成中，请先等当前任务结束，或点停止后再重新加载")
    try:
        from app import modelswap

        if modelswap.session_status().get("illustration_session") or modelswap.session_status().get("session_active"):
            raise HTTPException(status_code=409, detail="正在出图，请先等配图结束后再重新加载")
        from app.serverctl import schedule_reload

        schedule_reload()
    except HTTPException:
        raise
    except Exception:
        logger.exception("schedule reload failed")
        raise HTTPException(status_code=500, detail="无法启动重新加载，请稍后重试")
    return {"ok": True, "message": "正在重新加载写作服务…", "started_at": APP_STARTED_AT}


@app.get("/")
def index():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("?v=extractbook2", f"?v={APP_STARTED_AT}")
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
