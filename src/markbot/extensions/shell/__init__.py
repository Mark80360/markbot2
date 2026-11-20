"""受限命令执行工具（默认需要用户确认）。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.api.sandbox import run_shell_sandboxed


class RunIn(BaseModel):
    command: str = Field(description="要执行的 shell 命令")
    timeout_s: float = Field(default=30.0, ge=1, le=600, description="超时秒数")


def setup(api: ExtensionAPI) -> None:
    async def run_command(ctx, args: RunIn) -> ToolResult:
        cwd = Path(ctx.policy.root)
        result = await run_shell_sandboxed(
            args.command, cwd=cwd, timeout_s=min(args.timeout_s, 600.0)
        )
        content = result.combined()
        if not result.ok:
            return ToolResult(
                content=content or f"命令失败，退出码 {result.returncode}",
                is_error=True,
                details={"returncode": result.returncode, "timed_out": result.timed_out},
            )
        return ToolResult(
            content=content or "（无输出）",
            details={"returncode": result.returncode},
        )

    api.register_tool(
        Tool(
            name="run_command",
            description="在受限环境中执行 shell 命令并返回输出",
            params=RunIn,
            handler=run_command,
            capabilities=frozenset({"exec:*"}),
            timeout_s=120.0,
        )
    )


__all__ = ["setup"]
