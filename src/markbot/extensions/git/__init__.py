"""Git 工具：状态、差异、历史、提交、分支、worktree（ADR 0013）。

- 只封装常用的安全操作；``push --force``、``reset --hard``、``clean -f``、改写历史等不提供，
  通过 ``run_command`` 执行时由权限策略强制确认；
- 能力声明为 ``exec:git``：standard 档位每次需要确认，trusted 档位免确认（能力钉死了程序）；
- 在当前回合的工作区（绑定项目时是项目目录）里执行；``git_commit`` 默认只提交明确列出的文件，
  避免把无关改动、密钥文件一起提交。
"""

from __future__ import annotations

import re
import shutil
from typing import Literal

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult

MAX_OUTPUT = 20_000
_REF = re.compile(r"^[A-Za-z0-9._/@{}~^:-]{1,200}$")
_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]{1,100}$")
_SENSITIVE = re.compile(r"(^|/)(\.env(\..*)?|secrets\.toml|auth\.token|id_rsa|.*\.pem|.*\.key)$")


async def _git(ctx, *args: str, timeout_s: float = 60) -> tuple[int, str]:
    git = shutil.which("git")
    exec_argv = getattr(ctx.env, "exec_argv", None)
    if git is None:
        return 127, "未找到 git"
    if not callable(exec_argv):
        return 1, "当前执行环境不支持直接执行程序"
    result = await exec_argv(
        [git, "-c", "core.quotepath=off", "-c", "color.ui=never", *args],
        cwd=getattr(ctx.env, "root", None),
        timeout_s=timeout_s,
    )
    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    if result.timed_out:
        return 124, output + "\n[timeout]"
    return result.returncode, output.strip()


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"


def _bad_paths(paths: list[str]) -> str | None:
    for path in paths:
        if path.startswith("-") or ".." in path.replace("\\", "/").split("/"):
            return f"非法路径: {path}"
    return None


class StatusIn(BaseModel):
    pass


class DiffIn(BaseModel):
    staged: bool = Field(default=False, description="查看已暂存（--cached）的改动")
    ref: str = Field(default="", description="与某个提交/分支比较，如 HEAD~1、main")
    paths: list[str] = Field(default_factory=list, description="只看这些路径", json_schema_extra={"x-markbot-role": "path"})
    stat: bool = Field(default=False, description="只看文件级统计")


class LogIn(BaseModel):
    max_count: int = Field(default=15, ge=1, le=200)
    path: str = Field(default="", description="只看这个路径的历史", json_schema_extra={"x-markbot-role": "path"})
    ref: str = Field(default="", description="分支或提交，默认当前分支")


class CommitIn(BaseModel):
    message: str = Field(description="提交说明：第一行是简短摘要（祈使句），空一行后可写详细说明", min_length=3)
    paths: list[str] = Field(
        default_factory=list,
        description="要提交的文件；留空则只提交已暂存的改动。all=true 时提交全部已跟踪文件的改动",
        json_schema_extra={"x-markbot-role": "path"},
    )
    all: bool = Field(default=False, description="提交全部已跟踪文件的改动（不含未跟踪的新文件）")


class BranchIn(BaseModel):
    action: Literal["list", "create", "switch"] = Field(default="list")
    name: str = Field(default="", description="分支名（create / switch）")
    start_point: str = Field(default="", description="create 时的起点，默认当前 HEAD")


class WorktreeIn(BaseModel):
    action: Literal["list", "add", "remove"] = Field(default="list")
    path: str = Field(default="", description="worktree 目录（add / remove）", json_schema_extra={"x-markbot-role": "path"})
    branch: str = Field(default="", description="add 时新建的分支名")


def setup(api: ExtensionAPI) -> None:
    async def git_status(ctx, args: StatusIn) -> ToolResult:
        code, out = await _git(ctx, "status", "--porcelain=v1", "--branch", "--untracked-files=normal")
        if code != 0:
            return ToolResult.error(f"git status 失败: {out}")
        lines = out.splitlines()
        head = lines[0][3:] if lines and lines[0].startswith("## ") else ""
        entries = [line for line in lines if not line.startswith("## ")]
        if not entries:
            return ToolResult(content=f"分支 {head}：工作区干净", details={"clean": True, "branch": head})
        return ToolResult(
            content=f"分支 {head}：{len(entries)} 个文件有改动\n" + _clip("\n".join(entries)),
            details={"clean": False, "branch": head, "count": len(entries)},
        )

    async def git_diff(ctx, args: DiffIn) -> ToolResult:
        bad = _bad_paths(args.paths)
        if bad:
            return ToolResult.error(bad)
        if args.ref and not _REF.match(args.ref):
            return ToolResult.error(f"非法引用: {args.ref}")
        argv = ["diff", "--no-ext-diff"]
        if args.staged:
            argv.append("--cached")
        if args.stat:
            argv.append("--stat")
        if args.ref:
            argv.append(args.ref)
        argv += ["--", *args.paths]
        code, out = await _git(ctx, *argv)
        if code != 0:
            return ToolResult.error(f"git diff 失败: {out}")
        return ToolResult(content=_clip(out) or "（没有差异）")

    async def git_log(ctx, args: LogIn) -> ToolResult:
        if args.ref and not _REF.match(args.ref):
            return ToolResult.error(f"非法引用: {args.ref}")
        argv = ["log", f"--max-count={args.max_count}", "--date=short",
                "--pretty=format:%h %ad %an %s"]
        if args.ref:
            argv.append(args.ref)
        if args.path:
            bad = _bad_paths([args.path])
            if bad:
                return ToolResult.error(bad)
            argv += ["--", args.path]
        code, out = await _git(ctx, *argv)
        if code != 0:
            return ToolResult.error(f"git log 失败: {out}")
        return ToolResult(content=out or "（没有提交记录）")

    async def git_commit(ctx, args: CommitIn) -> ToolResult:
        bad = _bad_paths(args.paths)
        if bad:
            return ToolResult.error(bad)
        sensitive = [path for path in args.paths if _SENSITIVE.search(path.replace("\\", "/"))]
        if sensitive:
            return ToolResult.error(f"拒绝提交疑似密钥文件: {', '.join(sensitive)}")
        if args.paths:
            code, out = await _git(ctx, "add", "--", *args.paths)
            if code != 0:
                return ToolResult.error(f"git add 失败: {out}")
        argv = ["commit", "-m", args.message]
        if args.all:
            argv.insert(1, "--all")
        code, out = await _git(ctx, *argv, timeout_s=120)
        if code != 0:
            hint = ""
            if "user.email" in out or "Please tell me who you are" in out:
                hint = "（需要先配置 git user.name 与 user.email）"
            elif "nothing to commit" in out or "no changes added" in out:
                hint = "（没有可提交的改动：用 paths 指定文件，或先 git add）"
            return ToolResult.error(f"git commit 失败{hint}: {_clip(out, 3000)}")
        _, head = await _git(ctx, "log", "-1", "--pretty=format:%h %s")
        return ToolResult(content=f"已提交 {head}\n{_clip(out, 3000)}", details={"commit": head.split(" ", 1)[0]})

    async def git_branch(ctx, args: BranchIn) -> ToolResult:
        if args.action == "list":
            code, out = await _git(ctx, "branch", "--list", "--format=%(HEAD) %(refname:short) %(upstream:short)")
            return ToolResult(content=out or "（没有分支）") if code == 0 else ToolResult.error(out)
        if not _BRANCH.match(args.name):
            return ToolResult.error(f"非法分支名: {args.name!r}")
        if args.action == "create":
            if args.start_point and not _REF.match(args.start_point):
                return ToolResult.error(f"非法起点: {args.start_point}")
            argv = ["switch", "-c", args.name] + ([args.start_point] if args.start_point else [])
        else:
            argv = ["switch", args.name]
        code, out = await _git(ctx, *argv)
        if code != 0:
            hint = "（工作区有未提交改动会阻止切换：先提交，或用 git_worktree 在独立目录工作）" if "overwritten" in out else ""
            return ToolResult.error(f"git switch 失败{hint}: {out}")
        return ToolResult(content=out or f"已切换到 {args.name}")

    async def git_worktree(ctx, args: WorktreeIn) -> ToolResult:
        if args.action == "list":
            code, out = await _git(ctx, "worktree", "list")
            return ToolResult(content=out) if code == 0 else ToolResult.error(out)
        if not args.path or args.path.startswith("-"):
            return ToolResult.error("需要 path")
        try:
            target = ctx.env.resolve(args.path, write=True)
        except TypeError:
            target = ctx.env.resolve(args.path)
        if args.action == "add":
            if args.branch and not _BRANCH.match(args.branch):
                return ToolResult.error(f"非法分支名: {args.branch!r}")
            argv = ["worktree", "add"] + (["-b", args.branch] if args.branch else []) + [str(target)]
        else:
            argv = ["worktree", "remove", str(target)]
        code, out = await _git(ctx, *argv, timeout_s=120)
        if code != 0:
            return ToolResult.error(f"git worktree {args.action} 失败: {out}")
        return ToolResult(content=out or f"worktree {args.action} 完成: {target}")

    def register(name: str, description: str, params, handler, *, read_only: bool) -> None:
        api.register_tool(
            Tool(
                name=name,
                description=description,
                params=params,
                handler=handler,
                capabilities=frozenset({"exec:git"}),
                timeout_s=150.0,
                concurrency_safe=read_only,
            )
        )

    register("git_status", "查看当前分支与工作区改动（git status）", StatusIn, git_status, read_only=True)
    register("git_diff", "查看改动差异（工作区 / 已暂存 / 与某个提交比较），可限定路径或只看统计",
             DiffIn, git_diff, read_only=True)
    register("git_log", "查看提交历史（简短格式）", LogIn, git_log, read_only=True)
    register(
        "git_commit",
        "提交改动：默认只提交 paths 列出的文件（推荐），或 all=true 提交全部已跟踪改动；拒绝提交密钥文件",
        CommitIn, git_commit, read_only=False,
    )
    register("git_branch", "列出、新建（并切换到）或切换分支", BranchIn, git_branch, read_only=False)
    register(
        "git_worktree",
        "在独立目录里检出新分支（git worktree add），不影响用户当前工作区的未提交改动",
        WorktreeIn, git_worktree, read_only=False,
    )


__all__ = ["setup"]
