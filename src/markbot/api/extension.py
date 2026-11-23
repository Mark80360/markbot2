"""扩展 API：车道 A 的唯一注入点（协议，不提供实现）。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from markbot.api.tool import Tool

CommandHandler = Callable[[str], str | Awaitable[str]]


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    description: str
    handler: CommandHandler


class ExtensionAPI(Protocol):
    version: str
    id: str
    # 只读的公开配置（不含 [secrets]）；内置扩展据此读取自身小节
    config: Mapping[str, Any]

    def on(self, event: str, handler: Callable[..., Any]) -> Callable[[], None]: ...

    def register_tool(self, tool: Tool) -> None: ...

    def register_command(self, name: str, description: str, handler: CommandHandler) -> None: ...

    def register_provider(self, provider: Any) -> None: ...

    def register_skill_root(self, path: Path) -> None: ...

    def append_entry(self, key: str, value: Any) -> None: ...

    def ui(self): ...


__all__ = ["Command", "CommandHandler", "ExtensionAPI"]
