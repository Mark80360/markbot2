"""文件系统工具：读写、列目录、glob。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult

MAX_READ_CHARS = 40_000


def _resolve(ctx, raw: str) -> Path:
    path = Path(raw).expanduser()
    root = Path(ctx.policy.root).resolve()
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError(f"路径越界: {raw}") from exc
    return resolved


class ReadIn(BaseModel):
    path: str = Field(description="文件路径（相对工作目录或绝对路径）")
    start_line: int = Field(default=1, ge=1, description="起始行号")
    max_lines: int = Field(default=400, ge=1, le=4000, description="最多读取行数")


class WriteIn(BaseModel):
    path: str = Field(description="目标文件路径")
    content: str = Field(description="完整文件内容")
    create_dirs: bool = Field(default=True, description="自动创建父目录")


class ListIn(BaseModel):
    path: str = Field(default=".", description="目录路径")
    limit: int = Field(default=200, ge=1, le=1000)


class GlobIn(BaseModel):
    pattern: str = Field(description="glob 模式，例如 **/*.py")
    limit: int = Field(default=200, ge=1, le=1000)


def setup(api: ExtensionAPI) -> None:
    async def read_file(ctx, args: ReadIn) -> ToolResult:
        path = _resolve(ctx, args.path)
        if not path.is_file():
            return ToolResult.error(f"不是文件: {args.path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        start = args.start_line - 1
        chunk = lines[start : start + args.max_lines]
        content = "\n".join(chunk)
        if len(content) > MAX_READ_CHARS:
            content = content[:MAX_READ_CHARS] + "\n…（已截断）"
        return ToolResult(
            content=content,
            details={"path": str(path), "lines": len(chunk), "total_lines": len(lines)},
        )

    async def write_file(ctx, args: WriteIn) -> ToolResult:
        path = _resolve(ctx, args.path)
        if args.create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args.content, encoding="utf-8")
        return ToolResult(
            content=f"已写入 {path}（{len(args.content)} 字符）",
            details={"path": str(path)},
        )

    async def list_dir(ctx, args: ListIn) -> ToolResult:
        path = _resolve(ctx, args.path)
        if not path.is_dir():
            return ToolResult.error(f"不是目录: {args.path}")
        entries = []
        for child in sorted(path.iterdir())[: args.limit]:
            kind = "dir" if child.is_dir() else "file"
            size = child.stat().st_size if child.is_file() else 0
            entries.append(f"{kind}\t{child.name}\t{size}")
        return ToolResult(content="\n".join(entries) or "（空目录）")

    async def glob_files(ctx, args: GlobIn) -> ToolResult:
        root = Path(ctx.policy.root).resolve()
        matches = []
        for path in root.glob(args.pattern):
            try:
                matches.append(path.relative_to(root).as_posix())
            except ValueError:
                continue
            if len(matches) >= args.limit:
                break
        return ToolResult(content="\n".join(sorted(matches)) or "（无匹配）")

    api.register_tool(
        Tool(
            name="read_file",
            description="读取文本文件内容，可按行截取",
            params=ReadIn,
            handler=read_file,
            capabilities=frozenset({"fs.read:./**"}),
        )
    )
    api.register_tool(
        Tool(
            name="write_file",
            description="写入文本文件（覆盖）",
            params=WriteIn,
            handler=write_file,
            capabilities=frozenset({"fs.write:./**"}),
        )
    )
    api.register_tool(
        Tool(
            name="list_dir",
            description="列出目录内容",
            params=ListIn,
            handler=list_dir,
            capabilities=frozenset({"fs.read:./**"}),
        )
    )
    api.register_tool(
        Tool(
            name="glob_files",
            description="按 glob 模式查找文件",
            params=GlobIn,
            handler=glob_files,
            capabilities=frozenset({"fs.read:./**"}),
        )
    )


__all__ = ["setup"]
