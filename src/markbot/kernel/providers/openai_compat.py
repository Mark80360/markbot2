"""OpenAI 兼容 Chat Completions Provider。"""

from __future__ import annotations

import json
from typing import Any

from markbot.kernel.providers.base import (
    DeltaCallback,
    ModelReply,
    ProviderError,
    ToolCall,
    make_tool_call,
)
from markbot.kernel.providers.http import int_items, post_json, sse_event, stream_error, stream_sse


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

    async def complete(
        self, payload: dict[str, Any], on_delta: DeltaCallback | None = None
    ) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": _messages(payload),
            # 探测请求会显式传小 max_tokens，避免为定位被拒内容而生成大段回复
            "max_tokens": payload.get("max_tokens") or self.max_tokens,
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
        url = self.base_url + "/chat/completions"
        if on_delta is None:
            return await post_json(
                url,
                body,
                headers,
                parse=_parse,
                timeout_s=self.timeout_s,
                max_retries=self.max_retries,
                backoff_s=self.retry_backoff_s,
            )
        body["stream"] = True
        return await stream_sse(
            url,
            body,
            headers,
            new_accumulator=_OpenAIStream,
            on_delta=on_delta,
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
        calls.append(
            make_tool_call(
                str(raw.get("id") or ""), str(fn.get("name") or ""), fn.get("arguments")
            )
        )
    usage = data.get("usage") or {}
    return ModelReply(
        text=message.get("content") or "",
        tool_calls=calls,
        usage={k: v for k, v in usage.items() if isinstance(v, int)},
        raw=data,
    )


class _OpenAIStream:
    """累积 Chat Completions 的流式增量：文本、按 index 拼接的工具调用、usage。"""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.tools: dict[int, dict[str, str]] = {}
        self.usage: dict[str, int] = {}

    def feed(self, event: dict[str, Any]) -> str | None:
        if event.get("error"):
            raise stream_error(event["error"])
        if event.get("usage"):
            self.usage.update(int_items(event["usage"]))
        choices = event.get("choices") or []
        if not choices:
            return None
        delta = choices[0].get("delta") or {}
        for raw_call in delta.get("tool_calls") or []:
            try:
                index = int(raw_call.get("index") or 0)
            except (TypeError, ValueError):
                index = 0
            slot = self.tools.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if raw_call.get("id"):
                slot["id"] = str(raw_call["id"])
            function = raw_call.get("function") or {}
            if function.get("name"):
                slot["name"] += str(function["name"])
            if function.get("arguments"):
                slot["arguments"] += str(function["arguments"])
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.text.append(content)
            return content
        return None

    def result(self) -> ModelReply | None:
        if not self.text and not self.tools:
            return None
        calls = [
            make_tool_call(slot["id"], slot["name"], slot["arguments"])
            for _, slot in sorted(self.tools.items())
        ]
        return ModelReply(text="".join(self.text), tool_calls=calls, usage=self.usage)


# 兼容旧引用
_stream_event = sse_event








__all__ = ["OpenAICompatProvider"]
