"""子代理：``spawn_agent``（ADR 0013）。

主代理把独立的子任务（探索代码、评审改动、写/跑测试、调研）派给子代理：子代理在独立子会话里运行，
有自己的步数上限与工具子集，只把结果摘要返回给主会话——主会话的上下文只增加这份摘要。

角色：
- 内置 ``explorer``（只读探索）、``reviewer``（评审，只读 + 静态检查）、``tester``（写测试并运行）、
  ``researcher``（网页调研）、``general``（除子代理外的全部工具）；
- 能力包或学习层的 ``sub_agents/<name>.md`` 可以新增/覆盖角色：正文是角色说明，可选 frontmatter
  ``tools: [read_file, grep_search]`` 指定工具。

一次回复里的多个 ``spawn_agent`` 会并行执行（上限 ``[subagents] max_parallel``）。子代理的权限、
确认 UI 与工作区和主代理相同，不能再派生子代理。
"""

from __future__ import annotations

import threading
from typing import Any

import yaml
from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult

READ_ONLY = [
    "read_file", "list_dir", "glob_files", "grep_search", "file_outline",
    "git_status", "git_diff", "git_log", "memory_search", "list_skills", "read_skill", "read_skill_file",
]
WRITE = ["edit_file", "apply_patch", "write_file"]

BUILTIN_ROLES: dict[str, dict[str, Any]] = {
    "explorer": {
        "description": "只读探索：定位相关代码、理清调用关系与现状",
        "tools": READ_ONLY,
        "prompt": (
            "你是代码探索子代理，只读不改。用 grep_search / file_outline / read_file 找到与任务相关的代码，"
            "理清调用关系、数据流与现有约定。输出：结论（直接回答问题）、关键文件与行号、依据（引用代码片段），"
            "以及你不确定的地方。不要给出没有读过代码就做出的推测。"
        ),
    },
    "reviewer": {
        "description": "代码评审：找出改动里的缺陷、风险与遗漏",
        "tools": [*READ_ONLY, "run_checks"],
        "prompt": (
            "你是代码评审子代理，只读不改。重点看正确性（边界条件、错误处理、并发、资源释放）、"
            "安全（注入、越权、密钥）、与项目约定的一致性、测试是否覆盖。可以运行 run_checks。"
            "输出按严重程度分组（必须修复 / 建议修复 / 可选），每条写明 文件:行号、问题、原因、修改建议；"
            "没有问题就明确说没有发现问题。"
        ),
    },
    "tester": {
        "description": "测试：补充测试用例并运行，报告结果",
        "tools": [*READ_ONLY, *WRITE, "run_tests", "run_checks"],
        "prompt": (
            "你是测试子代理。先读被测代码与已有测试，按项目已有的测试风格补充用例（覆盖正常路径、边界与错误），"
            "只修改/新增测试文件，不修改被测代码；然后用 run_tests 运行。输出：新增/修改了哪些测试、运行结果、"
            "发现的被测代码缺陷（附失败用例与原因）。"
        ),
    },
    "researcher": {
        "description": "调研：查找资料并整理成带来源的结论",
        "tools": ["fetch_url", "memory_search", "read_file", "grep_search"],
        "prompt": (
            "你是调研子代理。围绕任务查找可靠来源，提取关键事实与数据，整理成结论。"
            "每条关键事实附来源链接；区分事实与推测；信息不足时说明缺口。"
        ),
    },
    "general": {
        "description": "通用：可以使用除子代理以外的全部工具",
        "tools": ["*"],
        "prompt": "你是子代理，独立完成分配给你的子任务，完成后给出简洁、完整的结果。",
    },
}


def _parse_role(text: str) -> dict[str, Any]:
    body = text.lstrip("\ufeff")
    meta: dict[str, Any] = {}
    if body.startswith("---"):
        parts = body.split("\n---", 1)
        if len(parts) == 2:
            try:
                loaded = yaml.safe_load(parts[0][3:]) or {}
                meta = loaded if isinstance(loaded, dict) else {}
            except yaml.YAMLError:
                meta = {}
            body = parts[1].lstrip("-\n")
    tools = meta.get("tools")
    if isinstance(tools, str):
        tools = [item.strip() for item in tools.split(",") if item.strip()]
    first_line = body.strip().splitlines()[0][:80] if body.strip() else ""
    return {
        "description": str(meta.get("description") or first_line),
        "tools": [str(item) for item in tools] if isinstance(tools, list) else READ_ONLY,
        "prompt": body.strip(),
    }


def roles(session: Any) -> dict[str, dict[str, Any]]:
    out = {name: dict(spec) for name, spec in BUILTIN_ROLES.items()}
    custom = getattr(getattr(session, "runtime", None), "sub_agents", None) or {}
    for name, text in custom.items():
        out[str(name)] = _parse_role(str(text))
    return out


def _available(session: Any) -> list[str]:
    tools = getattr(getattr(session, "runtime", None), "tools", None)
    names = getattr(tools, "names", None)
    return list(names()) if callable(names) else []


class SpawnIn(BaseModel):
    task: str = Field(description="交给子代理的完整任务说明：目标、范围、需要返回什么（子代理看不到主会话历史）")
    role: str = Field(default="explorer", description="角色：explorer / reviewer / tester / researcher / general 或自定义角色")
    context: str = Field(default="", description="子代理需要知道的背景（相关文件、已知结论、约束）")
    tools: list[str] = Field(default_factory=list, description="覆盖角色默认的工具列表（只能是现有工具）")
    max_steps: int = Field(default=0, ge=0, le=60, description="步数上限；0 用默认值")


class EmptyIn(BaseModel):
    pass


_ACTIVE: dict[str, int] = {}
_LOCK = threading.Lock()


def _settings(config: Any) -> dict[str, Any]:
    section = (config or {}).get("subagents") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


def setup(api: ExtensionAPI) -> None:
    settings = _settings(getattr(api, "config", None))
    default_steps = int(settings.get("max_steps") or 20)
    max_parallel = max(1, int(settings.get("max_parallel") or 3))
    result_chars = max(1000, int(settings.get("result_max_chars") or 8000))

    async def spawn_agent(ctx, args: SpawnIn) -> ToolResult:
        runner = getattr(ctx, "run_subagent", None)
        if not callable(runner):
            return ToolResult.error("当前环境不支持子代理")
        session = ctx.session
        if (getattr(session, "metadata", None) or {}).get("subagent"):
            return ToolResult.error("子代理不能再派生子代理")
        catalog = roles(session)
        spec = catalog.get(args.role)
        if spec is None:
            return ToolResult.error(f"未知角色: {args.role}。可用角色：{'、'.join(catalog)}")
        available = _available(session)
        wanted = args.tools or list(spec["tools"])
        if "*" in wanted:
            wanted = [name for name in available if name not in {"spawn_agent", "list_agents"}]
        tools = [name for name in wanted if name in available and name != "spawn_agent"]
        if not tools:
            return ToolResult.error("子代理没有可用工具：检查 tools 参数或角色定义")
        key = str(getattr(session, "id", ""))
        with _LOCK:
            if _ACTIVE.get(key, 0) >= max_parallel:
                return ToolResult.error(f"同时运行的子代理已达上限 {max_parallel}，请等前面的完成")
            _ACTIVE[key] = _ACTIVE.get(key, 0) + 1
        prompt = (
            f"{spec['prompt']}\n\n## 任务\n{args.task.strip()}"
            + (f"\n\n## 背景\n{args.context.strip()}" if args.context.strip() else "")
            + "\n\n完成后直接给出结果（不要寒暄）；你的回复会原样交给主代理。"
        )
        try:
            outcome = await runner(
                prompt, tools=tools, max_steps=args.max_steps or default_steps, label=args.role
            )
        finally:
            with _LOCK:
                _ACTIVE[key] = max(0, _ACTIVE.get(key, 1) - 1)
        text = str(outcome.get("text") or "").strip()
        if len(text) > result_chars:
            text = text[:result_chars] + f"\n…（子代理结果过长，已截断到 {result_chars} 字符）"
        reason = outcome.get("stopped_reason")
        head = (
            f"[子代理 {args.role} · {outcome.get('steps', 0)} 步 · 工具调用 {outcome.get('tool_calls', 0)} 次"
            + ("" if reason == "completed" else f" · 结束原因 {reason}") + "]"
        )
        changed = outcome.get("changed_files") or []
        if changed:
            head += "\n子代理修改了文件：" + "、".join(changed[:10])
        if outcome.get("error"):
            head += f"\n错误：{outcome['error']}"
        return ToolResult(
            content=f"{head}\n{text or '（子代理没有返回内容）'}",
            is_error=reason not in ("completed", "max_steps") and not text,
            details={k: outcome.get(k) for k in ("session_id", "steps", "tool_calls", "stopped_reason", "usage")},
        )

    async def list_agents(ctx, args: EmptyIn) -> ToolResult:
        catalog = roles(ctx.session)
        lines = [
            f"- {name}: {spec.get('description', '')}（工具：{'、'.join(spec['tools'][:8])}{'…' if len(spec['tools']) > 8 else ''}）"
            for name, spec in catalog.items()
        ]
        return ToolResult(content="可用子代理角色：\n" + "\n".join(lines))

    api.register_tool(
        Tool(
            name="spawn_agent",
            description=(
                "把独立的子任务交给子代理在独立会话里完成，只返回结果摘要（节省主会话上下文）。"
                "适合：并行探索多处代码、评审改动、补测试、调研。一次回复里的多个调用会并行执行。"
                "子代理看不到主会话历史，task 要写完整"
            ),
            params=SpawnIn,
            handler=spawn_agent,
            timeout_s=1800.0,
            concurrency_safe=True,
        )
    )
    api.register_tool(
        Tool(name="list_agents", description="列出可用的子代理角色", params=EmptyIn, handler=list_agents)
    )


__all__ = ["BUILTIN_ROLES", "roles", "setup"]
