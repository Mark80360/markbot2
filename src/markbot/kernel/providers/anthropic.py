"""Anthropic Messages Provider。"""

from __future__ import annotations

from typing import Any

from markbot.api.media import image_block
from markbot.kernel.providers.base import (
    DeltaCallback,
    ModelReply,
    ToolCall,
    make_tool_call,
)
from markbot.kernel.providers.http import int_items, post_json, sse_event, stream_error, stream_sse


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
        prompt_cache: bool = False,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self.version = version
        # 提示缓存（ADR 0013）：工具定义与系统提示加 cache_control 断点。第三方兼容网关可能
        # 不认这个字段，所以默认关闭，由工厂在官方接口时打开
        self.prompt_cache = prompt_cache

    async def complete(
        self, payload: dict[str, Any], on_delta: DeltaCallback | None = None
    ) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            # 探测请求会显式传小 max_tokens，避免为定位被拒内容而生成大段回复
            "max_tokens": payload.get("max_tokens") or self.max_tokens,
            "messages": _messages(payload.get("messages", [])),
        }
        if payload.get("system"):
            body["system"] = (
                [{"type": "text", "text": payload["system"], "cache_control": {"type": "ephemeral"}}]
                if self.prompt_cache
                else payload["system"]
            )
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
            if self.prompt_cache:
                # 工具定义在请求最前面且很少变化：缓存断点放在最后一个工具上
                body["tools"][-1] = {**body["tools"][-1], "cache_control": {"type": "ephemeral"}}
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": self.version,
            "Content-Type": "application/json",
        }
        url = self.base_url + "/v1/messages"
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
            new_accumulator=_AnthropicStream,
            on_delta=on_delta,
            timeout_s=self.timeout_s,
            max_retries=self.max_retries,
            backoff_s=self.retry_backoff_s,
        )


def _blocks(text: str, paths: list[str]) -> list[dict[str, Any]]:
    """文本 + 图片 -> Messages API 内容块；读取失败的图片改为文字说明。"""
    blocks: list[dict[str, Any]] = []
    notes: list[str] = []
    for path in paths:
        block = image_block(path)
        if block is None:
            notes.append(f"[图片 {path} 无法读取]")
            continue
        kind, data = block
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": kind, "data": data}})
    text = "\n".join(part for part in (text, *notes) if part)
    return [{"type": "text", "text": text or "（图片）"}, *blocks]


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
                "content": (
                    _blocks(str(msg.get("content") or ""), list(msg["images"]))
                    if msg.get("images")
                    else msg.get("content", "")
                ),
                "is_error": bool(msg.get("is_error")),
            }
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "user" and msg.get("images"):
            out.append({"role": "user", "content": _blocks(str(msg.get("content") or ""), list(msg["images"]))})
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
                make_tool_call(
                    str(block.get("id") or ""),
                    str(block.get("name") or ""),
                    block.get("input"),
                )
            )
    usage = data.get("usage") or {}
    return ModelReply(
        text="".join(text_parts),
        tool_calls=calls,
        usage={k: v for k, v in usage.items() if isinstance(v, int)},
        raw=data,
    )


class _AnthropicStream:
    """累积 Messages API 的流式事件：text_delta、tool_use 的 input_json_delta、usage。"""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.tools: dict[int, dict[str, str]] = {}
        self.usage: dict[str, int] = {}

    @staticmethod
    def _index(event: dict[str, Any]) -> int:
        try:
            return int(event.get("index") or 0)
        except (TypeError, ValueError):
            return 0

    def feed(self, event: dict[str, Any]) -> str | None:
        kind = event.get("type")
        if kind == "error":
            raise stream_error(event.get("error"))
        if kind == "message_start":
            self.usage.update(int_items((event.get("message") or {}).get("usage")))
        elif kind == "message_delta":
            self.usage.update(int_items(event.get("usage")))
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                self.tools[self._index(event)] = {
                    "id": str(block.get("id") or ""),
                    "name": str(block.get("name") or ""),
                    "json": "",
                }
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    self.text.append(text)
                    return text
            elif delta.get("type") == "input_json_delta":
                slot = self.tools.get(self._index(event))
                if slot is not None:
                    slot["json"] += str(delta.get("partial_json") or "")
        return None

    def result(self) -> ModelReply | None:
        if not self.text and not self.tools:
            return None
        calls = [
            make_tool_call(slot["id"], slot["name"], slot["json"])
            for _, slot in sorted(self.tools.items())
        ]
        return ModelReply(text="".join(self.text), tool_calls=calls, usage=self.usage)


# 兼容旧引用
_sse_payload = sse_event












__all__ = ["AnthropicProvider"]
