"""Anthropic Messages Provider。"""

from __future__ import annotations

import asyncio
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

_logger = logging.getLogger("markbot.provider")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 4096,
        timeout_s: float = 120.0,
        max_retries: int = 2,
        retry_backoff_s: float = 2.0,
        version: str = "2023-06-01",
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.version = version

    async def complete(self, payload: dict[str, Any]) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": _messages(payload.get("messages", [])),
        }
        if payload.get("system"):
            body["system"] = payload["system"]
        tools = payload.get("tools") or []
        if tools:
            body["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {"type": "object"}),
                }
                for tool in tools
            ]
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
            "Content-Type": "application/json",
        }
        data = await _request(
            self.base_url + "/v1/messages",
            body,
            headers,
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
            backoff_s=self.retry_backoff_s,
        )
        return _parse(data)


def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        if role == "system":
            continue
        if role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for call in msg["tool_calls"]:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call.get("arguments") or {},
                    }
                )
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id"),
                "content": msg.get("content", ""),
                "is_error": bool(msg.get("is_error")),
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            out.append({"role": role, "content": msg.get("content", "")})
    return out


def _parse(data: dict[str, Any]) -> ModelReply:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            text_parts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            calls.append(
                ToolCall(
                    id=str(block.get("id") or ""),
                    name=str(block.get("name") or ""),
                    args=block.get("input") or {},
                )
            )
    usage = data.get("usage") or {}
    return ModelReply(
        text="".join(text_parts),
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
) -> dict[str, Any]:
    """带重试的模型请求：网络错误、超时、429/5xx、响应无法解析都会重试。"""
    attempts = max(0, max_retries) + 1
    last_error = "provider 请求失败"
    for attempt in range(1, attempts + 1):
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=body, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
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
                    return resp.json()
                except ValueError as exc:
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


__all__ = ["AnthropicProvider"]
