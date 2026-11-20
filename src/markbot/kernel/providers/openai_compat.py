"""OpenAI 兼容 Chat Completions Provider。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from markbot.kernel.providers.base import (
    ModelReply,
    ProviderError,
    ToolCall,
    describe_error,
)

_RETRYABLE = (httpx.TransportError, httpx.TimeoutException)
_logger = logging.getLogger("markbot.provider")


class OpenAICompatProvider:
    name = "openai_compat"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        max_tokens: int = 4096,
        timeout_s: float = 120.0,
        max_retries: int = 2,
        retry_backoff_s: float = 2.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s

    async def complete(self, payload: dict[str, Any]) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(payload),
            "max_tokens": self.max_tokens,
        }
        tools = payload.get("tools") or []
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object"}),
                    },
                }
                for tool in tools
            ]
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        return await _request(
            self.base_url + "/chat/completions",
            body,
            headers,
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
            backoff_s=self.retry_backoff_s,
        )


def _messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    system = payload.get("system")
    if system:
        out.append({"role": "system", "content": system})
    for msg in payload.get("messages", []):
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {
                                "name": call["name"],
                                "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                            },
                        }
                        for call in msg["tool_calls"]
                    ],
                }
            )
        elif role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.get("tool_call_id"),
                    "content": msg.get("content", ""),
                }
            )
        else:
            out.append({"role": role, "content": msg.get("content", "")})
    return out


def _parse(data: dict[str, Any]) -> ModelReply:
    choices = data.get("choices") or []
    if not choices:
        raise ProviderError("模型返回为空")
    message = choices[0].get("message") or {}
    calls: list[ToolCall] = []
    for raw in message.get("tool_calls") or []:
        fn = raw.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append(ToolCall(id=str(raw.get("id") or ""), name=str(fn.get("name") or ""), args=args))
    usage = data.get("usage") or {}
    return ModelReply(
        text=message.get("content") or "",
        tool_calls=calls,
        usage={k: v for k, v in usage.items() if isinstance(v, int)},
        raw=data,
    )


async def _request(
    url: str,
    body: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout_s: float,
    max_retries: int,
    backoff_s: float,
) -> ModelReply:
    """带重试的模型请求：网络错误、超时、429/5xx、响应无法解析都会重试。"""
    attempts = max(0, max_retries) + 1
    last_error = "provider 请求失败"
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=body, headers=headers)
        except _RETRYABLE as exc:
            last_error = describe_error(exc)
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = f"provider HTTP {resp.status_code}: {resp.text[:300]}"
            elif resp.status_code >= 400:
                _logger.error(
                    "provider 请求被拒绝 url=%s status=%s 耗时=%.1fs body=%s",
                    url,
                    resp.status_code,
                    time.monotonic() - started,
                    resp.text[:300],
                )
                raise ProviderError(
                    f"provider HTTP {resp.status_code} url={url}: {resp.text[:300]}"
                )
            else:
                try:
                    return _parse(resp.json())
                except (ValueError, ProviderError) as exc:
                    last_error = describe_error(exc)

        elapsed = time.monotonic() - started
        if attempt >= attempts:
            _logger.error(
                "provider 调用失败 url=%s 第 %d/%d 次 耗时=%.1fs 已放弃 错误=%s",
                url,
                attempt,
                attempts,
                elapsed,
                last_error,
            )
            break
        delay = backoff_s * (2 ** (attempt - 1))
        _logger.warning(
            "provider 调用失败 url=%s 第 %d/%d 次 耗时=%.1fs 将在 %.1fs 后重试 错误=%s",
            url,
            attempt,
            attempts,
            elapsed,
            delay,
            last_error,
        )
        if delay > 0:
            await asyncio.sleep(delay)
    raise ProviderError(
        f"{last_error} url={url} 超时设置={timeout_s:.0f}s 已尝试 {attempts} 次"
    )


__all__ = ["OpenAICompatProvider"]
