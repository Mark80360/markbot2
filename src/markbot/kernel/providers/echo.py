"""离线 Provider：用于 selftest、测试与无密钥演示。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from markbot.kernel.providers.base import ModelReply, ToolCall

ReplyLike = ModelReply | str | Callable[[dict[str, Any]], ModelReply]


class EchoProvider:
    """确定性回显；``selftest:echo`` 会触发一次工具调用。"""

    name = "echo"

    def __init__(self, prefix: str = "[echo] ") -> None:
        self.prefix = prefix

    async def complete(self, payload: dict[str, Any]) -> ModelReply:
        messages = payload.get("messages") or []
        last_user = ""
        for msg in reversed(messages):
            if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                last_user = msg["content"]
                break
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        if tool_messages:
            return ModelReply(text=f"{self.prefix}tool result: {tool_messages[-1].get('content', '')}")
        tools = {tool.get("name") for tool in payload.get("tools") or []}
        if "echo" in tools and last_user.startswith("selftest:echo"):
            return ModelReply(tool_calls=[ToolCall(id="call_echo_1", name="echo", args={"text": last_user})])
        return ModelReply(text=f"{self.prefix}{last_user}")


class ScriptedProvider:
    """按顺序返回预设回复，用于测试与优化器回归。"""

    name = "scripted"

    def __init__(self, replies: list[ReplyLike]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self._last: ModelReply = ModelReply(text="")

    async def complete(self, payload: dict[str, Any]) -> ModelReply:
        self.calls.append(payload)
        if self.replies:
            item = self.replies.pop(0)
            if isinstance(item, ModelReply):
                self._last = item
            elif isinstance(item, str):
                self._last = ModelReply(text=item)
            else:
                self._last = item(payload)
        return self._last


__all__ = ["EchoProvider", "ReplyLike", "ScriptedProvider"]
