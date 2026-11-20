"""技能工具：索引之外按需读取正文，脚本执行走双重关卡。"""

from __future__ import annotations

import sys

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.api.sandbox import run_sandboxed


class ReadSkillIn(BaseModel):
    name: str = Field(description="技能名称")


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


def setup(api: ExtensionAPI) -> None:
    async def list_skills(ctx, args: EmptyIn) -> ToolResult:
        skills = _index(ctx)
        lines = [f"- {m.name}: {m.description}（{m.source}）" for m in skills.all()]
        return ToolResult(content="\n".join(lines) or "（无技能）")

    async def read_skill(ctx, args: ReadSkillIn) -> ToolResult:
        skills = _index(ctx)
        try:
            meta, body = skills.read_body(args.name)
        except KeyError:
            return ToolResult.error(f"未知技能: {args.name}")
        return ToolResult(
            content=f"# {meta.name}\n\n{body}",
            details={"path": str(meta.path), "source": meta.source},
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
        result = await run_sandboxed(
            cmd,
            cwd=skill_dir,
            timeout_s=min(args.timeout_s, 300.0),
        )
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
            name="run_skill_script",
            description="执行技能目录内声明的脚本",
            params=RunScriptIn,
            handler=run_skill_script,
            capabilities=frozenset({"exec:python"}),
            timeout_s=120.0,
        )
    )


__all__ = ["setup"]
