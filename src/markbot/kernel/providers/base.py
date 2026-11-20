"""Provider 抽象：只负责一次模型调用。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class ProviderError(RuntimeError):
    pass


def describe_error(exc: BaseException | None) -> str:
    """异常的可读描述。

    ``httpx.ReadTimeout`` 这类异常的 ``str()`` 是空串，直接记录会得到
    空白的错误信息（日志里就成了 ``"error": ""``），因此这里始终带上异常类型。
    """
    if exc is None:
        return "未知错误"
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelReply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None


class Provider(Protocol):
    name: str

    async def complete(self, payload: dict[str, Any]) -> ModelReply: ...


__all__ = ["ModelReply", "Provider", "ProviderError", "ToolCall", "describe_error"]
