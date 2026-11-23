"""Provider 共用的 HTTP 传输：重试、退避、错误分类、SSE 读取。

各家 provider 只负责「请求体怎么拼、响应怎么解析、流式事件怎么累积」，
重试策略与错误语义在这里只有一份：

- 网络错误、超时、429、5xx、响应无法解析：按 ``backoff_s * 2^(n-1)`` 退避重试；
- 其它 4xx：不重试；命中内容安全特征时抛 :class:`ContentRejectedError`；
- 流式已经向用户输出过文本后中断：不重放（会重复输出），直接报错；
- 流式事件里的错误（``StreamAccumulator.feed`` 抛 ``ProviderError``）：不重试。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

import httpx

from markbot.kernel.providers.base import (
    ContentRejectedError,
    DeltaCallback,
    ModelReply,
    ProviderError,
    describe_error,
    is_moderation_rejection,
)

RETRYABLE_EXC = (httpx.TransportError, httpx.TimeoutException)
_logger = logging.getLogger("markbot.provider")


class StreamAccumulator(Protocol):
    def feed(self, event: dict[str, Any]) -> str | None:
        """处理一条 SSE 事件，返回本次新增的可见文本（没有则 ``None``）。"""
        ...

    def result(self) -> ModelReply | None:
        """流结束后的完整回复；什么都没收到时返回 ``None``（按「模型返回为空」重试）。"""
        ...


def sse_event(line: str) -> dict[str, Any] | None:
    """解析一行 SSE：返回事件 JSON；``[DONE]``、空行、非 data 行返回 ``None``。"""
    if not line or not line.startswith("data:"):
        return None
    chunk = line[5:].strip()
    if not chunk or chunk == "[DONE]":
        return None
    try:
        event = json.loads(chunk)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def int_items(value: Any) -> dict[str, int]:
    """usage 字典只保留整数项（各家都会混入嵌套结构）。"""
    if not isinstance(value, dict):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool)}


def _client_error(url: str, status: int, text: str, started: float | None = None) -> ProviderError:
    if started is None:
        _logger.error(
            "provider 请求被拒绝 url=%s status=%s body=%s", url, status, text[:300]
        )
    else:
        _logger.error(
            "provider 请求被拒绝 url=%s status=%s 耗时=%.1fs body=%s",
            url,
            status,
            time.monotonic() - started,
            text[:300],
        )
    detail = f"provider HTTP {status} url={url}: {text[:300]}"
    if is_moderation_rejection(text):
        return ContentRejectedError(detail)
    return ProviderError(detail)


def _retryable_status(status: int) -> bool:
    return status == 429 or status >= 500


async def _backoff(
    *, label: str, url: str, attempt: int, attempts: int, started: float, backoff_s: float,
    last_error: str,
) -> bool:
    """记录失败；还有重试机会时等待并返回 True。"""
    elapsed = time.monotonic() - started
    if attempt >= attempts:
        _logger.error(
            "provider %s失败 url=%s 第 %d/%d 次 耗时=%.1fs 已放弃 错误=%s",
            label, url, attempt, attempts, elapsed, last_error,
        )
        return False
    delay = backoff_s * (2 ** (attempt - 1))
    _logger.warning(
        "provider %s失败 url=%s 第 %d/%d 次 耗时=%.1fs 将在 %.1fs 后重试 错误=%s",
        label, url, attempt, attempts, elapsed, delay, last_error,
    )
    if delay > 0:
        await asyncio.sleep(delay)
    return True


def _give_up(last_error: str, url: str, timeout_s: float, attempts: int) -> ProviderError:
    return ProviderError(
        f"{last_error} url={url} 超时设置={timeout_s:.0f}s 已尝试 {attempts} 次"
    )


async def post_json(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    parse: Callable[[Any], ModelReply],
    timeout_s: float,
    max_retries: int,
    backoff_s: float,
) -> ModelReply:
    """非流式请求；``parse`` 抛 ``ValueError`` / ``ProviderError`` 视为响应不可解析并重试。"""
    attempts = max(0, max_retries) + 1
    last_error = "provider 请求失败"
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=body, headers=headers)
        except RETRYABLE_EXC as exc:
            last_error = describe_error(exc)
        else:
            if _retryable_status(resp.status_code):
                last_error = f"provider HTTP {resp.status_code}: {resp.text[:300]}"
            elif resp.status_code >= 400:
                raise _client_error(url, resp.status_code, resp.text, started)
            else:
                try:
                    return parse(resp.json())
                except (ValueError, ProviderError) as exc:
                    last_error = describe_error(exc)
        if not await _backoff(
            label="调用", url=url, attempt=attempt, attempts=attempts, started=started,
            backoff_s=backoff_s, last_error=last_error,
        ):
            break
    raise _give_up(last_error, url, timeout_s, attempts)


async def stream_sse(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    new_accumulator: Callable[[], StreamAccumulator],
    on_delta: DeltaCallback,
    timeout_s: float,
    max_retries: int,
    backoff_s: float,
) -> ModelReply:
    """SSE 流式请求：增量文本实时交给 ``on_delta``，结束后返回完整回复。"""
    attempts = max(0, max_retries) + 1
    last_error = "provider 请求失败"
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        acc = new_accumulator()
        emitted = False
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                async with client.stream("POST", url, json=body, headers=headers) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode("utf-8", "replace")
                        if not _retryable_status(resp.status_code):
                            raise _client_error(url, resp.status_code, raw)
                        last_error = f"provider HTTP {resp.status_code} url={url}: {raw[:300]}"
                    else:
                        async for line in resp.aiter_lines():
                            event = sse_event(line)
                            if event is None:
                                continue
                            text = acc.feed(event)
                            if text:
                                emitted = True
                                on_delta(text)
                        reply = acc.result()
                        if reply is not None:
                            return reply
                        last_error = "模型返回为空"
        except RETRYABLE_EXC as exc:
            if emitted:
                raise ProviderError(
                    f"流式输出中断：{describe_error(exc)}（已输出内容无法重放）"
                ) from exc
            last_error = describe_error(exc)
        if not await _backoff(
            label="流式调用", url=url, attempt=attempt, attempts=attempts, started=started,
            backoff_s=backoff_s, last_error=last_error,
        ):
            break
    raise _give_up(last_error, url, timeout_s, attempts)


def stream_error(error: Any) -> ProviderError:
    message = error.get("message") if isinstance(error, dict) else str(error)
    return ProviderError(f"provider 流式错误: {message or error}")


__all__ = [
    "RETRYABLE_EXC",
    "StreamAccumulator",
    "int_items",
    "post_json",
    "sse_event",
    "stream_error",
    "stream_sse",
]
