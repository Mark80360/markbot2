"""技能工具：索引之外按需读取正文，脚本执行走双重关卡。"""

from __future__ import annotations

import sys

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.api.sandbox import run_sandboxed


class ReadSkillIn(BaseModel):
    name: str = Field(description="技能名称")


class ReadSkillFileIn(BaseModel):
    skill: str = Field(description="技能名称")
    path: str = Field(description="技能目录内的相对路径，例如 references/api.md")


class RunScriptIn(BaseModel):
    skill: str = Field(description="技能名称")
    script: str = Field(description="技能目录内的脚本相对路径")
    args: list[str] = Field(default_factory=list, description="脚本参数")
    timeout_s: float = Field(default=30.0, ge=1, le=300)


class EmptyIn(BaseModel):
    pass


def _index(ctx):
    skills = getattr(ctx.session, "skills", None)
    if skills is None:
        raise RuntimeError("当前会话没有技能索引")
    return skills


def _unknown_skill(skills, name: str) -> ToolResult:
    """未知技能时给出可操作的原因：被跳过（frontmatter 不合规）还是根本不存在。"""
    marker = f"/{name}/SKILL.md"
    skipped = [
        warning
        for warning in getattr(skills, "warnings", []) or []
        if marker in str(warning).replace("\\", "/")
    ]
    if skipped:
        return ToolResult.error(f"技能 {name} 存在但未进入索引：{skipped[0]}")
    names = list(skills.names())[:30]
    return ToolResult.error(
        f"未知技能: {name}。当前可用技能：{'、'.join(names) or '（无）'}"
    )


def setup(api: ExtensionAPI) -> None:
    async def list_skills(ctx, args: EmptyIn) -> ToolResult:
        skills = _index(ctx)
        lines = []
        for meta in skills.all():
            missing = skills.missing_requirements(meta)
            suffix = f"（不可用：{'、'.join(missing)}）" if missing else ""
            lines.append(f"- {meta.name}: {meta.description}{suffix}（{meta.source}）")
        return ToolResult(content="\n".join(lines) or "（无技能）")

    async def read_skill(ctx, args: ReadSkillIn) -> ToolResult:
        skills = _index(ctx)
        try:
            meta, body = skills.read_body(args.name)
        except KeyError:
            return _unknown_skill(skills, args.name)
        return ToolResult(
            content=f"# {meta.name}\n\n{body}",
            details={"path": str(meta.path), "source": meta.source},
        )

    async def read_skill_file(ctx, args: ReadSkillFileIn) -> ToolResult:
        skills = _index(ctx)
        try:
            meta, target, text = skills.read_asset(args.skill, args.path)
        except KeyError:
            return _unknown_skill(skills, args.skill)
        except PermissionError as exc:
            return ToolResult.error(str(exc))
        except FileNotFoundError:
            return ToolResult.error(f"技能内文件不存在: {args.path}")
        return ToolResult(
            content=text or "（空文件）",
            details={"skill": meta.name, "path": str(target)},
        )

    async def run_skill_script(ctx, args: RunScriptIn) -> ToolResult:
        skills = _index(ctx)
        meta = skills.get(args.skill)
        if meta is None:
            return ToolResult.error(f"未知技能: {args.skill}")
        if "run_script" not in meta.allowed_tools and "*" not in meta.allowed_tools:
            return ToolResult.error(f"技能 {args.skill} 未声明 run_script 权限")
        skill_dir = meta.path.parent.resolve()
        script = (skill_dir / args.script).resolve()
        try:
            script.relative_to(skill_dir)
        except ValueError:
            return ToolResult.error(f"脚本越界: {args.script}")
        if not script.is_file():
            return ToolResult.error(f"脚本不存在: {args.script}")
        if script.suffix == ".py":
            cmd = [sys.executable, str(script), *args.args]
        elif script.suffix in {".sh", ".bash"}:
            cmd = ["bash", str(script), *args.args]
        else:
            return ToolResult.error(f"不支持的脚本类型: {script.suffix}")
        timeout_s = min(args.timeout_s, 300.0)
        # 优先走执行环境（OS 级隔离、统一超时策略）；旧执行环境没有 exec_argv 时退回
        exec_argv = getattr(getattr(ctx, "env", None), "exec_argv", None)
        if callable(exec_argv):
            # 学习层技能目录位于受保护的 MarkBot home 内：只读开放本技能目录，其余仍受保护
            try:
                result = await exec_argv(
                    cmd, cwd=skill_dir, timeout_s=timeout_s, readable=(skill_dir,)
                )
            except TypeError:  # 旧执行环境的 exec_argv 不接受 readable
                result = await exec_argv(cmd, cwd=skill_dir, timeout_s=timeout_s)
        else:
            result = await run_sandboxed(cmd, cwd=skill_dir, timeout_s=timeout_s)
        content = result.combined()
        if not result.ok:
            return ToolResult(
                content=content or f"脚本失败，退出码 {result.returncode}",
                is_error=True,
                details={"returncode": result.returncode},
            )
        return ToolResult(content=content or "（无输出）")

    api.register_tool(
        Tool(
            name="list_skills",
            description="列出当前可用技能",
            params=EmptyIn,
            handler=list_skills,
        )
    )
    api.register_tool(
        Tool(
            name="read_skill",
            description="读取技能正文；技能索引里只有名称与描述",
            params=ReadSkillIn,
            handler=read_skill,
        )
    )
    api.register_tool(
        Tool(
            name="read_skill_file",
            description="读取技能目录内的附带文件（references/assets 等）",
            params=ReadSkillFileIn,
            handler=read_skill_file,
        )
    )
    api.register_tool(
        Tool(
            name="run_skill_script",
            description="执行技能目录内声明的脚本",
            params=RunScriptIn,
            handler=run_skill_script,
            capabilities=frozenset({"exec:python"}),
            timeout_s=120.0,
        )
    )


__all__ = ["setup"]
