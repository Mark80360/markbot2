"""工具注册表与工具执行上下文。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from markbot.api.execution import ExecutionEnvironment
from markbot.api.extension import Command
from markbot.api.tool import Tool
from markbot.api.ui import UI, NullUI
from markbot.kernel.execution import LocalExecution
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
        env: ExecutionEnvironment | None = None,
        subagent: Any = None,
        vision: Any = None,
    ) -> None:
        self.session = session
        self._subagent = subagent
        self._vision = vision
        # 主模型能否直接看图（AgentLoop 按 ContextBuilder.vision 设置；ADR 0015）
        self.main_vision = False
        self.policy = policy
        self.env = env or LocalExecution(
            policy.root,
            guard=getattr(policy, "guard", None),
            os_sandbox_mode=getattr(policy, "os_sandbox", "off"),
        )
        self._secrets = secrets or {}
        self._ui = ui or NullUI()

    async def run_subagent(
        self, prompt: str, *, tools: list[str], max_steps: int = 20, label: str = "sub"
    ) -> dict[str, Any]:
        """在独立子会话里运行一个子代理，只返回结果摘要（ADR 0013）。"""
        if self._subagent is None:
            raise RuntimeError("当前环境不支持子代理")
        return await self._subagent(
            self.session, prompt, tools=tools, max_steps=max_steps, label=label
        )

    async def describe_image(self, path: str, question: str = "") -> str:
        """交给视觉模型描述图片（ADR 0015）；没有可用的视觉模型时抛 RuntimeError。"""
        if self._vision is None:
            raise RuntimeError("当前环境没有可用的视觉模型")
        return await self._vision(path, question)

    @property
    def vision_available(self) -> bool:
        return self._vision is not None

    def secret_names(self) -> list[str]:
        """已配置的密钥名（只有名字，没有值），供定时任务预检等使用。"""
        return sorted(name for name, value in self._secrets.items() if value)

    def secret(self, name: str) -> str:
        value = self._secrets.get(name)
        if not value:
            raise KeyError(f"未配置密钥: {name}")
        return value

    async def confirm(self, title: str, message: str) -> bool:
        return await self._ui.confirm(title, message)


__all__ = ["CommandRegistry", "Conflict", "KernelToolContext", "ToolRegistry"]
