"""回显工具：M0 验收与连通性检查用。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult


class EchoIn(BaseModel):
    text: str = Field(description="要回显的文本")


def setup(api: ExtensionAPI) -> None:
    async def echo(ctx, args: EchoIn) -> ToolResult:
        return ToolResult(content=args.text)

    api.register_tool(
        Tool(
            name="echo",
            description="原样返回输入文本，用于连通性检查",
            params=EchoIn,
            handler=echo,
        )
    )


__all__ = ["setup"]
