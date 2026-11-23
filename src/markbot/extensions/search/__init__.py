"""代码搜索与导航：``grep_search``、``file_outline``（ADR 0012）。

- ``grep_search``：优先调用 ripgrep（遵守 .gitignore、跳过二进制），没有 rg 时用纯 Python 实现
  兜底（跳过常见的依赖/构建目录、隐藏目录与 .gitignore 里的简单模式）。结果带行号，可带上下文。
  搜索根目录经执行环境的路径守卫校验，受保护目录（MarkBot home、学习层）里的内容不会出现。
- ``file_outline``：列出文件里的类、函数、方法（Python 用 ast，其它语言用正则近似），带行号，
  大文件先看结构再按行号读取。
"""

from __future__ import annotations

import ast
import fnmatch
import os
import re
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_LINE_CHARS = 300
SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".tox", "dist",
     "build", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", "target", ".idea",
     ".vscode", ".markbot"}
)


class GrepIn(BaseModel):
    pattern: str = Field(description="正则表达式（literal=true 时按普通文本匹配）")
    path: str = Field(default=".", description="搜索的目录或文件", json_schema_extra={"x-markbot-role": "path"})
    glob: str = Field(default="", description="只搜索匹配的文件，如 *.py 或 src/**/*.ts")
    ignore_case: bool = Field(default=False)
    literal: bool = Field(default=False, description="按普通文本而不是正则匹配")
    context: int = Field(default=0, ge=0, le=5, description="每个匹配前后显示的行数")
    max_results: int = Field(default=100, ge=1, le=1000, description="最多返回的匹配行数")
    files_only: bool = Field(default=False, description="只列出包含匹配的文件")


class OutlineIn(BaseModel):
    path: str = Field(description="要查看结构的文件", json_schema_extra={"x-markbot-role": "path"})


def _resolve(ctx, raw: str) -> Path:
    return ctx.env.resolve(raw)


def _display(ctx, path: Path) -> str:
    root = getattr(ctx.env, "root", None)
    try:
        return path.relative_to(root).as_posix() if root else path.as_posix()
    except ValueError:
        return path.as_posix()


def _protected(ctx, path: Path) -> bool:
    guard = getattr(ctx.env, "guard", None)
    check = getattr(guard, "denial", None)
    if not callable(check):
        return False
    try:
        return check(path, write=False) is not None
    except Exception:  # noqa: BLE001
        return True


# ------------------------------------------------------------------ ripgrep
async def _rg(ctx, args: GrepIn, base: Path) -> ToolResult | None:
    rg = shutil.which("rg")
    exec_argv = getattr(ctx.env, "exec_argv", None)
    if rg is None or not callable(exec_argv):
        return None
    argv = [rg, "--line-number", "--no-heading", "--color", "never", "--max-columns", str(MAX_LINE_CHARS),
            "--max-columns-preview", "--max-filesize", "2M", "--glob", "!.markbot"]
    if args.ignore_case:
        argv.append("--ignore-case")
    if args.literal:
        argv.append("--fixed-strings")
    if args.files_only:
        argv.append("--files-with-matches")
    elif args.context:
        argv += ["--context", str(args.context)]
    if args.glob:
        argv += ["--glob", args.glob]
    argv += ["--", args.pattern, str(base)]
    result = await exec_argv(argv, cwd=getattr(ctx.env, "root", None), timeout_s=60)
    if result.returncode not in (0, 1):  # 1 = 没有匹配
        if "regex parse error" in result.stderr:
            return ToolResult.error(f"正则表达式无效: {result.stderr.strip()[:300]}（可设置 literal=true）")
        return None  # 其它错误退回 Python 实现
    root = getattr(ctx.env, "root", None)
    lines: list[str] = []
    for line in result.stdout.splitlines():
        if root:
            prefix = str(root) + os.sep
            if line.startswith(prefix):
                line = line[len(prefix):]
        lines.append(line.replace("\\", "/") if os.name == "nt" else line)
    return _format(lines, args, engine="rg")


# ------------------------------------------------------------------ Python 兜底
def _gitignore(base: Path) -> list[str]:
    patterns: list[str] = []
    for directory in (base, *base.parents):
        path = directory / ".gitignore"
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if line and not line.startswith(("#", "!")):
                        patterns.append(line.rstrip("/"))
            except OSError:
                pass
        if (directory / ".git").exists():
            break
    return patterns


def _ignored(name: str, rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern) for pattern in patterns)


def _python_grep(ctx, args: GrepIn, base: Path) -> ToolResult:
    flags = re.IGNORECASE if args.ignore_case else 0
    try:
        regex = re.compile(re.escape(args.pattern) if args.literal else args.pattern, flags)
    except re.error as exc:
        return ToolResult.error(f"正则表达式无效: {exc}（可设置 literal=true）")
    patterns = _gitignore(base if base.is_dir() else base.parent)
    files: list[Path] = []
    if base.is_file():
        files = [base]
    else:
        for dirpath, dirnames, filenames in os.walk(base):
            current = Path(dirpath)
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
                and not _ignored(d, (current / d).relative_to(base).as_posix(), patterns)
                and not _protected(ctx, current / d)
            )
            for name in sorted(filenames):
                path = current / name
                rel = path.relative_to(base).as_posix()
                if name.startswith(".") or _ignored(name, rel, patterns):
                    continue
                if args.glob and not (fnmatch.fnmatch(rel, args.glob) or fnmatch.fnmatch(name, args.glob)):
                    continue
                files.append(path)
    lines: list[str] = []
    limit = args.max_results
    for path in files:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace").splitlines()
        shown = _display(ctx, path)
        hits = [index for index, line in enumerate(text) if regex.search(line)]
        if not hits:
            continue
        if args.files_only:
            lines.append(shown)
        else:
            emitted: set[int] = set()
            for index in hits:
                for number in range(max(0, index - args.context), min(len(text), index + args.context + 1)):
                    if number in emitted:
                        continue
                    emitted.add(number)
                    sep = ":" if number == index or number in hits else "-"
                    lines.append(f"{shown}{sep}{number + 1}{sep}{text[number][:MAX_LINE_CHARS]}")
        if len(lines) > limit * 2:
            break
    return _format(lines, args, engine="python")


def _format(lines: list[str], args: GrepIn, *, engine: str) -> ToolResult:
    if not lines:
        return ToolResult(content="（无匹配）", details={"matches": 0, "engine": engine})
    shown = lines[: args.max_results]
    more = len(lines) - len(shown)
    content = "\n".join(shown)
    if more > 0:
        content += f"\n…（还有 {more} 行未显示：缩小 path/glob 或提高 max_results）"
    return ToolResult(
        content=content, details={"matches": len(lines), "shown": len(shown), "engine": engine}
    )


# ------------------------------------------------------------------ outline
def _python_outline(text: str) -> list[str]:
    tree = ast.parse(text)
    out: list[str] = []

    def walk(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                out.append(f"{child.lineno:>5}  {'  ' * depth}class {child.name}")
                walk(child, depth + 1)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                params = ", ".join(arg.arg for arg in child.args.args)
                out.append(f"{child.lineno:>5}  {'  ' * depth}{prefix} {child.name}({params})")
                walk(child, depth + 1)

    walk(tree, 0)
    return out


_GENERIC = [
    (re.compile(r"^\s*(export\s+)?(default\s+)?(async\s+)?function\*?\s+([\w$]+)"), "function"),
    (re.compile(r"^\s*(export\s+)?(default\s+)?(abstract\s+)?class\s+([\w$]+)"), "class"),
    (re.compile(r"^\s*(export\s+)?(const|let)\s+([\w$]+)\s*=\s*(async\s*)?(\([^)]*\)|[\w$]+)\s*=>"), "function"),
    (re.compile(r"^\s*(export\s+)?(interface|type|enum)\s+([\w$]+)"), "type"),
    (re.compile(r"^func\s+(\([^)]*\)\s*)?([\w]+)"), "func"),
    (re.compile(r"^\s*(pub(\([^)]*\))?\s+)?(async\s+)?fn\s+(\w+)"), "fn"),
    (re.compile(r"^\s*(pub\s+)?(struct|enum|trait|impl)\b\s*(<[^>]*>)?\s*([\w:]+)"), "type"),
    (re.compile(r"^\s*(public|private|protected|internal|static|\s)*\s*(class|interface|record)\s+(\w+)"), "class"),
    (re.compile(r"^\s*(public|private|protected|internal)\s+[\w<>\[\],\s]+\s+(\w+)\s*\([^;]*$"), "method"),
    (re.compile(r"^(#{1,6})\s+(.+)"), "heading"),
]


def _generic_outline(text: str) -> list[str]:
    out: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        for regex, kind in _GENERIC:
            match = regex.match(line)
            if match:
                out.append(f"{number:>5}  {line.strip()[:120]}")
                break
    return out


def setup(api: ExtensionAPI) -> None:
    async def grep_search(ctx, args: GrepIn) -> ToolResult:
        base = _resolve(ctx, args.path)
        if not base.exists():
            return ToolResult.error(f"路径不存在: {args.path}")
        try:
            result = await _rg(ctx, args, base)
        except Exception:  # noqa: BLE001 - rg 不可用时退回 Python 实现
            result = None
        return result if result is not None else _python_grep(ctx, args, base)

    async def file_outline(ctx, args: OutlineIn) -> ToolResult:
        path = _resolve(ctx, args.path)
        if not path.is_file():
            return ToolResult.error(f"不是文件: {args.path}")
        if path.stat().st_size > MAX_FILE_BYTES:
            return ToolResult.error("文件过大，请用 grep_search 定位")
        text = path.read_bytes().decode("utf-8", errors="replace")
        total = text.count("\n") + 1
        if path.suffix == ".py":
            try:
                entries = _python_outline(text)
            except SyntaxError as exc:
                entries = _generic_outline(text)
                entries.insert(0, f"（Python 语法错误，第 {exc.lineno} 行：{exc.msg}；以下为近似结构）")
        else:
            entries = _generic_outline(text)
        if not entries:
            return ToolResult(content=f"（没有识别到结构，共 {total} 行）", details={"lines": total})
        return ToolResult(
            content=f"{_display(ctx, path)}（{total} 行）\n" + "\n".join(entries[:400]),
            details={"lines": total, "entries": len(entries)},
        )

    api.register_tool(
        Tool(
            name="grep_search",
            description=(
                "在文件内容里搜索正则/文本，返回 路径:行号:内容（遵守 .gitignore）。"
                "找函数定义、调用点、配置项时优先用它，而不是 run_command grep"
            ),
            params=GrepIn,
            handler=grep_search,
            capabilities=frozenset({"fs.read:./**"}),
            timeout_s=90.0,
        )
    )
    api.register_tool(
        Tool(
            name="file_outline",
            description="列出文件中的类、函数、方法与标题及其行号；读大文件前先看结构",
            params=OutlineIn,
            handler=file_outline,
            capabilities=frozenset({"fs.read:./**"}),
        )
    )


__all__ = ["setup"]
