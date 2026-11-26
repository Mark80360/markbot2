"""工具契约：扩展唯一需要实现的执行单元。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from markbot.api.execution import ExecutionEnvironment


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
    env: ExecutionEnvironment

    def secret(self, name: str) -> str:
        """在边界处取密钥；密钥永不进入模型上下文。"""
        ...

    def confirm(self, title: str, message: str) -> Awaitable[bool]:
        """请求用户确认；非交互环境返回 False。"""
        ...


ToolHandler = Callable[[ToolContext, BaseModel], Awaitable[ToolResult] | ToolResult]

# 参数角色：权限策略据此知道哪个参数是路径/URL/命令，而不是按参数名猜。
# 声明方式（任选其一）：
#   Tool(..., arg_roles={"source": "path", "endpoint": "url"})
#   source: str = Field(json_schema_extra={ROLE_KEY: "path"})
ROLE_KEY = "x-markbot-role"
ARG_ROLES = frozenset({"path", "url", "command", "program"})


def _strip_roles(schema: Any) -> Any:
    """发给模型的 schema 不带内部角色标注（部分 provider 的严格模式会拒绝未知关键字）。"""
    if isinstance(schema, dict):
        return {k: _strip_roles(v) for k, v in schema.items() if k != ROLE_KEY}
    if isinstance(schema, list):
        return [_strip_roles(item) for item in schema]
    return schema


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
    # 动态工具（如 MCP）使用服务端下发的 JSON Schema；非 None 时优先于 params
    input_schema: dict[str, Any] | None = None
    # 参数名 -> 角色（path / url / command / program）；见 ROLE_KEY
    arg_roles: Mapping[str, str] | None = None
    # 并行工具调用时能否与其它调用同时执行（ADR 0012）。None = 按能力推断：
    # 声明了 fs.write / exec 的工具不并发（避免并发写同一文件、命令互相干扰）
    concurrency_safe: bool | None = None
    # 声明 exec 能力的工具是否走通用「执行命令需确认」。自身已做危险操作守卫
    # （如 kill_process 拒绝结束自身/父进程/系统关键进程）时可置 False；
    # 能力范围、受保护路径与 strict 档位仍然生效。
    confirm_exec: bool = True

    def is_concurrency_safe(self) -> bool:
        if self.concurrency_safe is not None:
            return bool(self.concurrency_safe)
        return not any(
            str(cap).startswith(("fs.write", "exec")) for cap in (self.capabilities or ())
        )

    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": _strip_roles(
                self.input_schema if self.input_schema is not None
                else self.params.model_json_schema()
            ),
        }

    def roles(self) -> dict[str, str]:
        """显式声明的参数角色：``arg_roles`` 优先，其次 schema 属性上的 ``x-markbot-role``。"""
        found: dict[str, str] = {}
        try:
            raw = (
                self.input_schema if self.input_schema is not None
                else self.params.model_json_schema()
            )
        except Exception:  # noqa: BLE001 - schema 生成失败时只看 arg_roles
            raw = {}
        for name, prop in ((raw or {}).get("properties") or {}).items():
            if isinstance(prop, dict) and prop.get(ROLE_KEY) in ARG_ROLES:
                found[str(name)] = str(prop[ROLE_KEY])
        for name, role in (self.arg_roles or {}).items():
            if role in ARG_ROLES:
                found[str(name)] = str(role)
        return found

    def parse_args(self, raw: dict[str, Any]) -> Any:
        """把模型给的参数转成 handler 入参。

        动态工具的参数契约由服务端持有，客户端不再二次校验，避免把 provider
        适配过的 schema 误当成服务端校验规则。
        """
        if self.input_schema is not None:
            return dict(raw or {})
        return self.params.model_validate(raw)


__all__ = ["ARG_ROLES", "ROLE_KEY", "Tool", "ToolContext", "ToolHandler", "ToolResult"]
