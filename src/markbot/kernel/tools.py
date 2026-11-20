"""工具注册表与工具执行上下文。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from markbot.api.extension import Command
from markbot.api.tool import Tool
from markbot.api.ui import UI, NullUI
from markbot.kernel.permissions import PermissionPolicy


@dataclass
class Conflict:
    tool: str
    winner: str
    loser: str


class ToolRegistry:
    """同名工具按优先级取高者；冲突记录但不静默合并。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._origins: dict[str, str] = {}
        self._priority: dict[str, int] = {}
        self.conflicts: list[Conflict] = []

    def register(self, tool: Tool, *, origin: str = "builtin", priority: int = 0) -> bool:
        current = self._tools.get(tool.name)
        if current is not None:
            winner_priority = self._priority[tool.name]
            if priority <= winner_priority:
                self.conflicts.append(Conflict(tool.name, self._origins[tool.name], origin))
                return False
            self.conflicts.append(Conflict(tool.name, origin, self._origins[tool.name]))
        self._tools[tool.name] = tool
        self._origins[tool.name] = origin
        self._priority[tool.name] = priority
        return True

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def origin_of(self, name: str) -> str:
        return self._origins.get(name, "unknown")

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[name] for name in self.names()]

    def schemas(self, expose: tuple[str, ...] = ("direct",)) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self.all() if tool.expose in expose]

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools


@dataclass
class CommandRegistry:
    _commands: dict[str, tuple[Command, str]] = field(default_factory=dict)
    conflicts: list[Conflict] = field(default_factory=list)

    def register(self, command: Command, *, origin: str = "builtin", priority: int = 0) -> bool:
        existing = self._commands.get(command.name)
        if existing and priority <= existing[1]:
            self.conflicts.append(Conflict(command.name, existing[0].name, origin))
            return False
        self._commands[command.name] = (command, priority)
        return True

    def get(self, name: str) -> Command | None:
        entry = self._commands.get(name)
        return entry[0] if entry else None

    def names(self) -> list[str]:
        return sorted(self._commands)

    def all(self) -> list[Command]:
        return [self._commands[n][0] for n in self.names()]


class KernelToolContext:
    """实现 ``markbot.api.ToolContext``。"""

    def __init__(
        self,
        *,
        session: Any,
        policy: PermissionPolicy,
        secrets: Mapping[str, str] | None = None,
        ui: UI | None = None,
    ) -> None:
        self.session = session
        self.policy = policy
        self._secrets = secrets or {}
        self._ui = ui or NullUI()

    def secret(self, name: str) -> str:
        value = self._secrets.get(name)
        if not value:
            raise KeyError(f"未配置密钥: {name}")
        return value

    async def confirm(self, title: str, message: str) -> bool:
        return await self._ui.confirm(title, message)


__all__ = ["CommandRegistry", "Conflict", "KernelToolContext", "ToolRegistry"]
