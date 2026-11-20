"""工具契约：扩展唯一需要实现的执行单元。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel


@dataclass(frozen=True, slots=True)
class ToolResult:
    content: str
    is_error: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str, **details: Any) -> "ToolResult":
        return cls(content=message, is_error=True, details=details)


class ToolContext(Protocol):
    session: Any
    policy: Any

    def secret(self, name: str) -> str:
        """在边界处取密钥；密钥永不进入模型上下文。"""
        ...

    def confirm(self, title: str, message: str) -> Awaitable[bool]:
        """请求用户确认；非交互环境返回 False。"""
        ...


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[ToolResult] | ToolResult]


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    params: type[BaseModel]
    handler: ToolHandler
    capabilities: frozenset[str] = frozenset()
    source: str = "builtin"  # builtin | package | learned
    timeout_s: float = 60.0
    expose: Literal["direct", "deferred", "hidden"] = "direct"

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.params.model_json_schema(),
        }


__all__ = ["Tool", "ToolContext", "ToolHandler", "ToolResult"]
