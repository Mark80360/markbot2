"""查看图片：``view_image``（ADR 0015）。

- 主模型能看图（``[agent] vision``）：把图片附到工具结果里，模型下一步直接看到；
- 主模型不能看图，或 ``describe=true``：交给视觉模型（``[agent.roles.vision]``）描述，返回文字。

截图类工具（``browser_screenshot`` / ``desktop_screenshot``）会自动附图，不需要再调用它。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.api.media import MAX_IMAGE_BYTES, media_type


class ViewImageIn(BaseModel):
    path: str = Field(description="图片路径（png/jpg/gif/webp）", json_schema_extra={"x-markbot-role": "path"})
    question: str = Field(default="", description="想从图片里了解什么（如：转写表格、找出报错信息）")
    describe: bool = Field(default=False, description="强制交给视觉模型描述成文字（例如需要 OCR 全文时）")


def _resolve(ctx, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(ctx.env.root) / path
    return path


def setup(api: ExtensionAPI) -> None:
    async def view_image(ctx, args: ViewImageIn) -> ToolResult:
        path = _resolve(ctx, args.path)
        if media_type(path) is None:
            return ToolResult.error(f"不支持的图片格式: {path.suffix or '(无扩展名)'}（支持 png/jpg/gif/webp）")
        if not path.is_file():
            return ToolResult.error(f"图片不存在: {path}")
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            return ToolResult.error(f"图片过大（{size // 1024} KB，上限 {MAX_IMAGE_BYTES // 1024 // 1024} MB）")
        main_vision = bool(getattr(ctx, "main_vision", False))
        if main_vision and not args.describe:
            note = f"\n关注：{args.question}" if args.question else ""
            return ToolResult(content=f"图片已附上：{path}{note}", details={"images": [str(path)], "path": str(path)})
        describe = getattr(ctx, "describe_image", None)
        if describe is None:
            return ToolResult.error("当前环境没有视觉模型；请配置 [agent.roles.vision]")
        try:
            text = await describe(str(path), args.question)
        except Exception as exc:  # noqa: BLE001 - 视觉模型不可用时给出可读原因
            return ToolResult.error(f"看图失败: {exc}")
        return ToolResult(content=f"[视觉模型对 {path.name} 的描述]\n{text}", details={"path": str(path), "described": True})

    api.register_tool(
        Tool(
            name="view_image",
            description=(
                "查看本地图片（用户发来的图片、截图、图表）。主模型能看图时直接附图；"
                "否则由视觉模型描述成文字"
            ),
            params=ViewImageIn,
            handler=view_image,
            capabilities=frozenset({"fs.read:*"}),
            timeout_s=120.0,
        )
    )


__all__ = ["setup"]
