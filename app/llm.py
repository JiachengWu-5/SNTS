"""llama-server client with thinking-budget safeguards and streaming."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

from app.config import (
    CONTEXT_TOKENS,
    DEFAULT_LLM_URL,
    PROMPT_SAFETY_MARGIN,
    THINKING_RESERVE_TOKENS,
)
from app.textutil import count_hanzi, estimate_tokens

logger = logging.getLogger("novel_writer.llm")


class LLMError(RuntimeError):
    pass


@dataclass
class ChatResult:
    content: str = ""
    reasoning: str = ""
    retried_without_thinking: bool = False
    hanzi: int = 0

    def __post_init__(self) -> None:
        self.hanzi = count_hanzi(self.content)


@dataclass
class StreamEvent:
    kind: str
    text: str = ""
    result: ChatResult | None = None
    message: str = ""
    extra: dict = field(default_factory=dict)


async def check_health(llm_url: str | None = None) -> dict:
    base = (llm_url or DEFAULT_LLM_URL).rstrip("/")
    url = f"{base}/health"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
        if response.status_code == 200:
            return {"ok": True, "url": base, "detail": "ok"}
        return {"ok": False, "url": base, "detail": f"HTTP {response.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "url": base, "detail": "无法连接写作模型"}


def available_output_tokens(prompt_text: str, context_tokens: int = CONTEXT_TOKENS) -> int:
    return max(0, context_tokens - estimate_tokens(prompt_text) - PROMPT_SAFETY_MARGIN)


def plan_max_tokens(prompt_text: str, target_chars: int, enable_thinking: bool, context_tokens: int = CONTEXT_TOKENS) -> int:
    remaining = available_output_tokens(prompt_text, context_tokens)
    # Qwen often uses ~1.5-2 tokens per Chinese character; keep a generous body budget.
    body_tokens = max(int(target_chars * 2.2) + 256, 512)
    if enable_thinking:
        needed = body_tokens + THINKING_RESERVE_TOKENS
    else:
        needed = body_tokens
    if remaining <= 0:
        return 0
    planned = min(needed, remaining)
    # If thinking would leave almost no room for the body, spend remaining tokens on the body.
    if enable_thinking and planned - THINKING_RESERVE_TOKENS < max(body_tokens // 2, 512):
        planned = min(max(body_tokens, 256), remaining)
    return planned


async def chat(
    messages: list[dict],
    *,
    llm_url: str | None = None,
    temperature: float = 0.8,
    max_tokens: int = 4096,
    enable_thinking: bool = False,
    cancel_event=None,
) -> ChatResult:
    collected = ChatResult()
    async for event in stream_chat(
        messages,
        llm_url=llm_url,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "error":
            raise LLMError(event.message)
        if event.kind == "cancelled":
            raise LLMError("已停止生成")
        if event.kind == "done" and event.result:
            collected = event.result
    return collected


async def stream_chat(
    messages: list[dict],
    *,
    llm_url: str | None = None,
    temperature: float = 0.8,
    max_tokens: int = 4096,
    enable_thinking: bool = False,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    result = await _stream_once(
        messages,
        llm_url=llm_url,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    )
    if result is None:
        yield StreamEvent(kind="cancelled", message="已停止生成")
        return
    if result.content.strip():
        yield StreamEvent(kind="done", result=result, text=result.content)
        return
    if enable_thinking:
        logger.warning("thinking-on call returned empty content; retrying with thinking off")
        yield StreamEvent(kind="status", message="思考已结束但正文为空，正在改为直接输出正文")
        retry = await _stream_once(
            _retry_messages(messages),
            llm_url=llm_url,
            temperature=temperature,
            max_tokens=max_tokens,
            enable_thinking=False,
            cancel_event=cancel_event,
        )
        if retry is None:
            yield StreamEvent(kind="cancelled", message="已停止生成")
            return
        retry.retried_without_thinking = True
        if not retry.content.strip():
            yield StreamEvent(kind="error", message="模型没有返回正文，请重试或关闭思考后再试")
            return
        yield StreamEvent(kind="done", result=retry, text=retry.content)
        return
    yield StreamEvent(kind="error", message="模型没有返回正文，请重试")


async def _stream_once(
    messages: list[dict],
    *,
    llm_url: str | None,
    temperature: float,
    max_tokens: int,
    enable_thinking: bool,
    cancel_event,
) -> ChatResult | None:
    """Yield happens at the public stream_chat layer; this collects one upstream call.

    Intermediate tokens are published through a callback list on the result object
    so the public generator can later be used as a live stream. For live UI updates
    we actually need to yield as tokens arrive. See stream_chat_live.
    """
    # Kept for non-live callers; live path uses stream_chat_live.
    collected = ChatResult()
    async for event in stream_chat_live(
        messages,
        llm_url=llm_url,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "text":
            collected.content += event.text
        elif event.kind == "thinking":
            collected.reasoning += event.text
        elif event.kind == "cancelled":
            return None
        elif event.kind == "error":
            raise LLMError(event.message)
        elif event.kind == "done" and event.result:
            return event.result
    collected.hanzi = count_hanzi(collected.content)
    return collected


async def stream_chat_live(
    messages: list[dict],
    *,
    llm_url: str | None = None,
    temperature: float = 0.8,
    max_tokens: int = 4096,
    enable_thinking: bool = False,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    base = (llm_url or DEFAULT_LLM_URL).rstrip("/")
    url = f"{base}/v1/chat/completions"
    payload = {
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": bool(enable_thinking)},
    }
    content = []
    reasoning = []
    cancelled = False
    timeout = httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="ignore")[:300]
                    raise LLMError(f"写作模型返回错误 HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        await response.aclose()
                        break
                    piece = _parse_sse_line(line)
                    if piece is None:
                        continue
                    if piece.get("reasoning"):
                        reasoning.append(piece["reasoning"])
                        yield StreamEvent(kind="thinking", text=piece["reasoning"])
                    if piece.get("content"):
                        content.append(piece["content"])
                        yield StreamEvent(kind="text", text=piece["content"])
    except LLMError:
        raise
    except httpx.HTTPError as exc:
        logger.warning("llama-server request failed: %s", exc.__class__.__name__)
        yield StreamEvent(kind="error", message="无法连接写作模型，请确认 llama-server 已在运行")
        return

    if cancelled:
        yield StreamEvent(kind="cancelled", message="已停止生成")
        return

    result = ChatResult(content=_strip_think_tags("".join(content)), reasoning="".join(reasoning))
    if not result.content and result.reasoning:
        # Some servers put everything into reasoning_content when thinking overflows.
        logger.warning("stream finished with empty content and non-empty reasoning")
    yield StreamEvent(kind="done", result=result, text=result.content)


async def stream_chat_with_retry(
    messages: list[dict],
    *,
    llm_url: str | None = None,
    temperature: float = 0.8,
    max_tokens: int = 4096,
    enable_thinking: bool = False,
    cancel_event=None,
) -> AsyncIterator[StreamEvent]:
    if int(max_tokens) <= 0:
        yield StreamEvent(kind="error", message="输出额度不足，无法生成。请减少文风样本或缩短提示后再试。")
        return
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    async for event in stream_chat_live(
        messages,
        llm_url=llm_url,
        temperature=temperature,
        max_tokens=max_tokens,
        enable_thinking=enable_thinking,
        cancel_event=cancel_event,
    ):
        if event.kind == "text":
            content_parts.append(event.text)
            yield event
        elif event.kind == "thinking":
            reasoning_parts.append(event.text)
            yield StreamEvent(kind="status", message="正在思考…")
        elif event.kind in {"error", "cancelled"}:
            yield event
            return
        elif event.kind == "done":
            result = event.result or ChatResult(
                content=_strip_think_tags("".join(content_parts)),
                reasoning="".join(reasoning_parts),
            )
            if result.content.strip():
                yield StreamEvent(kind="done", result=result, text=result.content)
                return
            if enable_thinking:
                logger.warning("thinking-on call returned empty content; retrying with thinking off")
                yield StreamEvent(kind="status", message="思考已结束但正文为空，正在改为直接输出正文")
                async for retry_event in stream_chat_live(
                    _retry_messages(messages),
                    llm_url=llm_url,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    enable_thinking=False,
                    cancel_event=cancel_event,
                ):
                    if retry_event.kind == "text":
                        yield retry_event
                    elif retry_event.kind == "thinking":
                        yield StreamEvent(kind="status", message="正在思考…")
                    elif retry_event.kind in {"error", "cancelled"}:
                        yield retry_event
                        return
                    elif retry_event.kind == "done":
                        retry_result = retry_event.result or ChatResult()
                        retry_result.retried_without_thinking = True
                        if not retry_result.content.strip():
                            yield StreamEvent(kind="error", message="模型没有返回正文，请重试或关闭思考后再试")
                            return
                        yield StreamEvent(kind="done", result=retry_result, text=retry_result.content)
                        return
                return
            yield StreamEvent(kind="error", message="模型没有返回正文，请重试")
            return


def _retry_messages(messages: list[dict]) -> list[dict]:
    follow = {
        "role": "user",
        "content": "请只输出最终文稿正文，不要输出思考过程，不要解释。直接从标题或正文第一句开始。",
    }
    return list(messages) + [follow]


def _parse_sse_line(line: str) -> dict | None:
    if not line:
        return None
    if line.startswith("data:"):
        data = line[5:].strip()
    else:
        return None
    if not data or data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = payload.get("choices") or []
    if not choices:
        return None
    choice = choices[0]
    delta = choice.get("delta") or choice.get("message") or {}
    content = delta.get("content") or ""
    reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
    if not content and not reasoning:
        return None
    return {"content": content, "reasoning": reasoning}


def _strip_think_tags(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    # Drop leaked thinking blocks if the server mixed them into content.
    lower = cleaned.lower()
    for start, end in (("<think>", "</think>"), ("<thinking>", "</thinking>")):
        while True:
            lo = lower.find(start)
            if lo < 0:
                break
            hi = lower.find(end, lo + len(start))
            if hi < 0:
                cleaned = cleaned[:lo]
                lower = cleaned.lower()
                break
            cleaned = cleaned[:lo] + cleaned[hi + len(end) :]
            lower = cleaned.lower()
    return cleaned.strip()
