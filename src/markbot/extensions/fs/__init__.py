"""文件系统工具：读、写、精确编辑、补丁、列目录、glob（ADR 0012）。

所有 I/O 都走 ``ctx.env``（``ExecutionEnvironment``），扩展不直接拼路径或读盘，
这样换成容器/远程执行时无需改工具代码。

编辑类工具的约定：
- ``edit_file``：用 ``old_string`` 精确定位（必须唯一，除非 ``replace_all``），保留文件原有换行风格；
- ``apply_patch``：unified diff，多文件**全部成功才落盘**，任何一个 hunk 对不上都不修改任何文件；
- 写入一律原子（临时文件 + 替换）；文件在本会话读取之后被外部改过时拒绝写入，提示先重新读取；
- 每次改动记入会话的变更清单（交付报告与「改了代码要验证」提醒使用）。
"""

from __future__ import annotations

import difflib
from pathlib import Path

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.extensions import _tracking as tracking
from markbot.extensions.fs.patch import PatchError, apply_hunks, new_file_text, parse_patch

MAX_READ_CHARS = 40_000
MAX_READ_BYTES = 10 * 1024 * 1024
MAX_DIFF_LINES = 60
_BINARY_SNIFF = 8192


def _resolve(ctx, raw: str, *, write: bool = False) -> Path:
    try:
        return ctx.env.resolve(raw, write=write)
    except TypeError:  # 旧的执行环境实现没有 write 参数
        return ctx.env.resolve(raw)


def _read_bytes(ctx, raw: str) -> bytes:
    reader = getattr(ctx.env, "read_bytes", None)
    if callable(reader):
        return reader(raw)
    return ctx.env.read_text(raw).encode("utf-8")


def _write_bytes(ctx, raw: str, data: bytes) -> Path:
    writer = getattr(ctx.env, "write_bytes", None)
    if callable(writer):
        return writer(raw, data)
    return ctx.env.write_text(raw, data.decode("utf-8"))


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF]


def _decode(data: bytes) -> tuple[str, str]:
    """返回 ``(统一为 \\n 的文本, 原换行符)``。非 UTF-8 时按替换字符解码。"""
    text = data.decode("utf-8", errors="replace")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def _encode(text: str, newline: str) -> bytes:
    return (text.replace("\n", "\r\n") if newline == "\r\n" else text).encode("utf-8")


def _diff(path: str, old: str, new: str) -> tuple[str, int, int]:
    lines = list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile=f"a/{path}", tofile=f"b/{path}",
            lineterm="", n=2,
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    shown = lines[:MAX_DIFF_LINES]
    if len(lines) > MAX_DIFF_LINES:
        shown.append(f"…（diff 共 {len(lines)} 行，已截断）")
    return "\n".join(shown), added, removed


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _near_matches(text: str, needle: str, limit: int = 3) -> list[int]:
    """old_string 找不到时，按首个非空行（忽略首尾空白）找可能的位置，帮助模型修正缩进/空白。"""
    first = next((line.strip() for line in needle.split("\n") if line.strip()), "")
    if not first:
        return []
    hits = [number for number, line in enumerate(text.split("\n"), 1) if first in line.strip()]
    return hits[:limit]


class ReadIn(BaseModel):
    path: str = Field(description="文件路径（相对工作目录或绝对路径）", json_schema_extra={"x-markbot-role": "path"})
    start_line: int = Field(default=1, ge=1, description="起始行号")
    max_lines: int = Field(default=400, ge=1, le=4000, description="最多读取行数")


class WriteIn(BaseModel):
    path: str = Field(description="目标文件路径", json_schema_extra={"x-markbot-role": "path"})
    content: str = Field(description="完整文件内容")
    create_dirs: bool = Field(default=True, description="自动创建父目录")


class EditIn(BaseModel):
    path: str = Field(description="要修改的文件", json_schema_extra={"x-markbot-role": "path"})
    old_string: str = Field(
        description="要替换的原文（逐字符一致，含缩进；需包含足够上下文以唯一定位）。"
        "为空表示新建文件（文件不存在或为空时）"
    )
    new_string: str = Field(description="替换后的内容")
    replace_all: bool = Field(default=False, description="替换所有出现处；默认要求 old_string 唯一")


class PatchIn(BaseModel):
    patch: str = Field(description="unified diff 文本（--- a/x、+++ b/x、@@ 行号 @@），可包含多个文件")
    paths: list[str] = Field(
        description="补丁会修改/新建/删除的全部文件路径（用于权限检查与确认展示，必须与补丁一致）",
        min_length=1,
        json_schema_extra={"x-markbot-role": "path"},
    )


class ListIn(BaseModel):
    path: str = Field(default=".", description="目录路径", json_schema_extra={"x-markbot-role": "path"})
    limit: int = Field(default=200, ge=1, le=1000)


class GlobIn(BaseModel):
    # glob 模式也按路径检查：``../*``、绝对路径越界会被拒绝
    pattern: str = Field(
        description="glob 模式，例如 **/*.py；也可以是绝对路径模式，如 ~/.markbot/workspace/**/*.py",
        json_schema_extra={"x-markbot-role": "path"},
    )
    limit: int = Field(default=200, ge=1, le=1000)


def setup(api: ExtensionAPI) -> None:
    async def read_file(ctx, args: ReadIn) -> ToolResult:
        path = _resolve(ctx, args.path)
        if not path.is_file():
            return ToolResult.error(f"不是文件: {args.path}")
        size = path.stat().st_size
        if size > MAX_READ_BYTES:
            return ToolResult.error(
                f"文件过大（{size} 字节）：请用 grep_search 定位后按行号分段读取，或用命令行工具处理"
            )
        data = _read_bytes(ctx, args.path)
        if _is_binary(data):
            return ToolResult.error(
                f"{args.path} 是二进制文件（{size} 字节），不能按文本读取", binary=True
            )
        tracking.record_read(ctx.session, path, data)
        text, _ = _decode(data)
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
        path = _resolve(ctx, args.path, write=True)
        old = ""
        if path.exists():
            stale = tracking.stale_reason(ctx.session, path)
            if stale:
                return ToolResult.error(stale, stale=True)
            try:
                old, _ = _decode(path.read_bytes()) if path.is_file() else ("", "\n")
            except OSError:
                old = ""
        written = ctx.env.write_text(args.path, args.content, create_dirs=args.create_dirs)
        _, added, removed = _diff(args.path, old, args.content)
        tracking.record_change(
            ctx.session, written, added=added, removed=removed,
            action="create" if not old else "write",
        )
        return ToolResult(
            content=f"已写入 {written}（{len(args.content)} 字符，+{added} -{removed} 行）",
            details={"path": str(written), "added": added, "removed": removed},
        )

    async def edit_file(ctx, args: EditIn) -> ToolResult:
        path = _resolve(ctx, args.path, write=True)
        exists = path.is_file()
        if not args.old_string:
            if exists and path.stat().st_size > 0:
                return ToolResult.error("old_string 为空只能用于新建文件；该文件已存在且不为空")
            data = args.new_string.encode("utf-8")
            written = _write_bytes(ctx, args.path, data)
            tracking.record_change(
                ctx.session, written, added=args.new_string.count("\n") + 1, action="create", data=data
            )
            return ToolResult(content=f"已新建 {written}", details={"path": str(written)})
        if not exists:
            return ToolResult.error(f"文件不存在: {args.path}（新建文件请让 old_string 为空）")
        stale = tracking.stale_reason(ctx.session, path)
        if stale:
            return ToolResult.error(stale, stale=True)
        raw = _read_bytes(ctx, args.path)
        if _is_binary(raw):
            return ToolResult.error(f"{args.path} 是二进制文件，不能按文本编辑")
        text, newline = _decode(raw)
        old = args.old_string.replace("\r\n", "\n")
        new = args.new_string.replace("\r\n", "\n")
        if old == new:
            return ToolResult.error("old_string 与 new_string 相同，没有改动")
        count = text.count(old)
        if count == 0:
            hint = _near_matches(text, old)
            where = f"；第 {'、'.join(map(str, hint))} 行附近有相似内容，可能是缩进或空白不一致" if hint else ""
            return ToolResult.error(
                f"在 {args.path} 中找不到 old_string{where}。请重新读取文件，逐字符复制要替换的原文"
            )
        if count > 1 and not args.replace_all:
            lines: list[int] = []
            start = 0
            while len(lines) < 10:
                index = text.find(old, start)
                if index < 0:
                    break
                lines.append(_line_of(text, index))
                start = index + 1
            return ToolResult.error(
                f"old_string 在 {args.path} 中出现了 {count} 次（第 {'、'.join(map(str, lines))} 行）。"
                "请加入更多上下文让它唯一，或设置 replace_all=true"
            )
        updated = text.replace(old, new) if args.replace_all else text.replace(old, new, 1)
        data = _encode(updated, newline)
        written = _write_bytes(ctx, args.path, data)
        diff, added, removed = _diff(args.path, text, updated)
        tracking.record_change(ctx.session, written, added=added, removed=removed, data=data)
        replaced = f"替换 {count} 处" if args.replace_all else "替换 1 处"
        return ToolResult(
            content=f"已修改 {written}（{replaced}，+{added} -{removed} 行）\n{diff}",
            details={"path": str(written), "added": added, "removed": removed, "replacements": count if args.replace_all else 1},
        )

    async def apply_patch(ctx, args: PatchIn) -> ToolResult:
        try:
            patches = parse_patch(args.patch)
        except PatchError as exc:
            return ToolResult.error(f"补丁格式错误: {exc}")
        declared = {}
        for raw in args.paths:
            declared[str(_resolve(ctx, raw, write=True))] = raw
        plan: list[tuple[str, Path, bytes | None, bytes | None, int, int]] = []
        for item in patches:
            target = _resolve(ctx, item.target, write=True)
            if str(target) not in declared:
                return ToolResult.error(
                    f"补丁修改了 {item.target}，但它不在 paths 里；paths 必须列出补丁涉及的全部文件"
                )
            original: bytes | None = target.read_bytes() if target.is_file() else None
            if item.is_new:
                if original:
                    return ToolResult.error(f"{item.target} 已存在，不能按新文件创建")
                new_bytes: bytes | None = new_file_text(item).encode("utf-8")
            else:
                if original is None:
                    return ToolResult.error(f"文件不存在: {item.target}")
                stale = tracking.stale_reason(ctx.session, target)
                if stale:
                    return ToolResult.error(stale, stale=True)
                if _is_binary(original):
                    return ToolResult.error(f"{item.target} 是二进制文件，不能打补丁")
                if item.is_delete:
                    new_bytes = None
                else:
                    text, newline = _decode(original)
                    try:
                        new_bytes = _encode(apply_hunks(text, item), newline)
                    except PatchError as exc:
                        return ToolResult.error(f"补丁未应用（没有修改任何文件）: {exc}")
            added, removed = item.counts()
            plan.append((item.target, target, original, new_bytes, added, removed))
        # 全部计算成功后才落盘；中途失败则把已写的文件恢复原样
        done: list[tuple[str, Path, bytes | None]] = []
        try:
            for raw, target, original, new_bytes, _, _ in plan:
                if new_bytes is None:
                    ctx.env.delete(raw)
                else:
                    _write_bytes(ctx, raw, new_bytes)
                done.append((raw, target, original))
        except Exception as exc:  # noqa: BLE001 - 回滚后如实报告
            for raw, target, original in reversed(done):
                try:
                    if original is None:
                        ctx.env.delete(raw)
                    else:
                        _write_bytes(ctx, raw, original)
                except Exception:  # noqa: BLE001
                    pass
            return ToolResult.error(f"写入失败，已回滚全部文件: {type(exc).__name__}: {exc}")
        lines = []
        for raw, target, original, new_bytes, added, removed in plan:
            action = "delete" if new_bytes is None else ("create" if original is None else "patch")
            tracking.record_change(
                ctx.session, target, added=added, removed=removed, action=action, data=new_bytes
            )
            label = {"delete": "删除", "create": "新建", "patch": "修改"}[action]
            lines.append(f"- {label} {raw}（+{added} -{removed}）")
        return ToolResult(
            content=f"补丁已应用到 {len(plan)} 个文件：\n" + "\n".join(lines),
            details={"files": [item[0] for item in plan]},
        )

    async def list_dir(ctx, args: ListIn) -> ToolResult:
        path = _resolve(ctx, args.path)
        if not path.is_dir():
            return ToolResult.error(f"不是目录: {args.path}")
        entries = []
        for child in ctx.env.list_dir(args.path, limit=args.limit):
            kind = "dir" if child.is_dir() else "file"
            size = child.stat().st_size if child.is_file() else 0
            entries.append(f"{kind}\t{child.name}\t{size}")
        return ToolResult(content="\n".join(entries) or "（空目录）")

    async def glob_files(ctx, args: GlobIn) -> ToolResult:
        matches = ctx.env.glob(args.pattern, limit=args.limit)
        return ToolResult(content="\n".join(matches) or "（无匹配）")

    api.register_tool(
        Tool(
            name="read_file",
            description="读取文本文件内容，可按行截取；二进制文件会被拒绝",
            params=ReadIn,
            handler=read_file,
            capabilities=frozenset({"fs.read:./**"}),
        )
    )
    api.register_tool(
        Tool(
            name="write_file",
            description="写入文本文件（整体覆盖）。修改已有文件请优先用 edit_file / apply_patch",
            params=WriteIn,
            handler=write_file,
            capabilities=frozenset({"fs.write:./**"}),
        )
    )
    api.register_tool(
        Tool(
            name="edit_file",
            description=(
                "精确修改文件：把唯一出现的 old_string 替换为 new_string（保留原换行风格，返回 diff）。"
                "改代码时优先使用；修改前先 read_file 确认原文"
            ),
            params=EditIn,
            handler=edit_file,
            capabilities=frozenset({"fs.write:./**"}),
        )
    )
    api.register_tool(
        Tool(
            name="apply_patch",
            description=(
                "应用 unified diff 补丁，可一次修改/新建/删除多个文件；全部 hunk 都能对上才落盘，"
                "否则不修改任何文件。paths 必须列出涉及的全部文件"
            ),
            params=PatchIn,
            handler=apply_patch,
            capabilities=frozenset({"fs.write:./**"}),
            timeout_s=60.0,
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
