"""长期记忆工具：显式记住、检索、遗忘与状态查询。

目标目录在 workspace 之外，权限策略无法用相对路径表达；实际约束由
``markbot.memory.store`` 的白名单（``optimizer.gate.allowed_path``）、容量检查
与写后校验承担，因此这里不声明 fs 能力。
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.memory.journal import Journal
from markbot.memory.store import MemoryStore

_logger = logging.getLogger("markbot.memory")
MAX_SEARCH_CHARS = 4_000


class RememberIn(BaseModel):
    text: str = Field(description="要长期记住的一句话（事实、偏好或流程描述）")
    target: Literal["memory", "wiki", "skill"] = Field(
        default="memory",
        description="memory=常驻记忆；wiki=按需知识页；skill=可复用流程（需 steps）",
    )
    topic: str = Field(default="", description="wiki 页名或技能名（target!=memory 时必填）")
    replaces: str = Field(default="", description="要替换掉的旧表述；留空表示新增")
    key: str = Field(
        default="",
        description="冲突键（如 pref.reply_language）；同键旧条目会被自动替换",
    )
    steps: list[str] = Field(
        default_factory=list, description="target=skill 时的可执行步骤，按顺序给出"
    )
    lifespan: Literal["permanent", "durable", "ephemeral"] = Field(
        default="durable", description="时效：permanent 长期有效；durable 90 天；ephemeral 14 天"
    )


class SearchIn(BaseModel):
    query: str = Field(description="检索关键词，空格分隔")
    limit: int = Field(default=5, ge=1, le=20)
    scope: Literal["all", "memory", "wiki", "sessions"] = Field(
        default="all", description="检索范围：sessions=已归档会话历史"
    )


class ForgetIn(BaseModel):
    text: str = Field(default="", description="从常驻记忆删除包含该片段的条目")
    topic: str = Field(default="", description="删除整个 wiki 页（与 text 二选一）")


class EmptyIn(BaseModel):
    pass


def _store(ctx) -> MemoryStore | None:
    runtime = getattr(ctx.session, "runtime", None)
    learned = getattr(runtime, "learned_dir", None)
    return MemoryStore(learned) if learned is not None else None


def _journal(ctx) -> Journal | None:
    """``sessions/`` 与 ``learned/`` 同级（都在 markbot home 下）。"""
    store = _store(ctx)
    return Journal(store.root.parent) if store is not None else None


def _memory_off(ctx) -> bool:
    metadata = getattr(ctx.session, "metadata", None) or {}
    return str(metadata.get("memory", "")).lower() == "off"


def _session_id(ctx) -> str:
    return str(getattr(ctx.session, "id", ""))


def _project_rev(ctx) -> str:
    metadata = getattr(ctx.session, "metadata", None) or {}
    return str(metadata.get("project_rev", "") or "")


def _audit(ctx, level: str, message: str, **fields) -> None:
    """关键动作写进运行日志；只记路径、字符数与校验结果，不记正文。"""
    logs = getattr(ctx.session, "logs", None)
    if logs is not None:
        logs.runtime_log(level, message, session=_session_id(ctx), **fields)


def setup(api: ExtensionAPI) -> None:
    async def remember(ctx, args: RememberIn) -> ToolResult:
        if _memory_off(ctx):
            _logger.info("记忆写入被拒绝 reason=session_off session=%s", _session_id(ctx))
            return ToolResult.error("本会话已关闭记忆读写（metadata.memory = off）")
        store = _store(ctx)
        if store is None:
            return ToolResult.error("当前会话没有长期记忆目录（learned/），无法写入")
        if args.replaces or (args.target == "skill" and args.topic
                             and store.skill_path(args.topic).exists()):
            approved = await ctx.confirm(
                "更新长期记忆",
                f"将替换包含「{args.replaces or args.topic}」的旧内容为：\n{args.text[:200]}",
            )
            if not approved:
                _audit(ctx, "INFO", "记忆替换被取消", path=args.target)
                return ToolResult(
                    content="用户未确认，未替换长期记忆",
                    details={"written": [], "removed": [], "verified": False},
                )
        try:
            if args.target == "skill":
                if not args.topic:
                    return ToolResult.error("target=skill 需要 topic（技能名）")
                if not args.steps:
                    return ToolResult.error("target=skill 需要 steps（可执行步骤）")
                receipt = store.remember_skill(
                    args.topic,
                    args.text,
                    args.steps,
                    session_id=_session_id(ctx),
                )
            else:
                receipt = store.remember(
                    args.text,
                    target=args.target,
                    topic=args.topic,
                    replaces=args.replaces,
                    lifespan=args.lifespan,
                    key=args.key,
                    verified_by="user",
                    sources=(_session_id(ctx),),
                    project_rev=_project_rev(ctx),
                    session_id=_session_id(ctx),
                )
        except OSError as exc:
            _logger.warning(
                "记忆写入失败 session=%s error=%s", _session_id(ctx), exc
            )
            return ToolResult.error(f"写入失败: {exc}")

        payload = receipt.as_dict()
        if not receipt.ok:
            _audit(
                ctx, "WARN", "记忆写入被拒绝", path=receipt.path, reason=receipt.message
            )
            return ToolResult(content=receipt.message or "未写入", is_error=True, details=payload)
        if not receipt.changed:
            _audit(ctx, "INFO", "记忆无变化", path=receipt.path)
            return ToolResult(content=f"无变化：{receipt.path} 已包含该条目", details=payload)
        _audit(
            ctx,
            "INFO",
            "记忆写入",
            path=receipt.path,
            chars=receipt.chars,
            removed=len(receipt.removed),
            verified=receipt.verified,
        )
        removed = f"，替换 {len(receipt.removed)} 条旧内容" if receipt.removed else ""
        return ToolResult(
            content=f"已写入 {receipt.path}（{receipt.chars} 字符，校验通过{removed}）",
            details=payload,
        )

    async def search(ctx, args: SearchIn) -> ToolResult:
        if _memory_off(ctx):
            return ToolResult.error("本会话已关闭记忆读写（metadata.memory = off）")
        store = _store(ctx)
        if store is None:
            return ToolResult.error("当前会话没有长期记忆目录（learned/）")
        hits = store.search(args.query, limit=args.limit, scope=args.scope)
        journal = _journal(ctx)
        if args.scope in {"all", "sessions"} and journal is not None:
            for hit in journal.search(args.query, limit=args.limit):
                hits.append(
                    {
                        "source": "sessions",
                        "path": f"sessions/{hit.session_id}/{hit.source}.jsonl",
                        "line": hit.line,
                        "text": hit.text,
                    }
                )
        if not hits:
            return ToolResult(content="未找到匹配的长期记忆", details={"hits": []})

        labels = {"memory": "常驻", "wiki": "wiki", "sessions": "历史"}
        lines: list[str] = []
        used = 0
        for hit in hits:
            label = labels.get(hit["source"], hit["source"])
            location = hit["path"] + (f":{hit['line']}" if hit["line"] else "")
            line = f"- [{label}] {location} — {hit['text']}"
            if used + len(line) > MAX_SEARCH_CHARS:
                lines.append("…（结果已截断，可用 read_file 精读）")
                break
            lines.append(line)
            used += len(line)
        return ToolResult(content="\n".join(lines), details={"hits": hits})

    async def forget(ctx, args: ForgetIn) -> ToolResult:
        if _memory_off(ctx):
            return ToolResult.error("本会话已关闭记忆读写（metadata.memory = off）")
        store = _store(ctx)
        if store is None:
            return ToolResult.error("当前会话没有长期记忆目录（learned/），无法删除")
        if args.topic:
            approved = await ctx.confirm(
                "删除长期记忆页", f"将删除 wiki 页 {args.topic} 及其索引条目。"
            )
            if not approved:
                _logger.info("记忆删除未确认 session=%s", _session_id(ctx))
                return ToolResult.error("用户未确认，未删除")
        try:
            receipt = store.forget(
                text=args.text, topic=args.topic, session_id=_session_id(ctx)
            )
        except OSError as exc:
            _logger.warning(
                "记忆删除失败 session=%s error=%s", _session_id(ctx), exc
            )
            return ToolResult.error(f"删除失败: {exc}")

        payload = receipt.as_dict()
        if not receipt.ok:
            _audit(
                ctx, "WARN", "记忆删除被拒绝", path=receipt.path, reason=receipt.message
            )
            return ToolResult(content=receipt.message or "未删除", is_error=True, details=payload)
        if not receipt.changed:
            return ToolResult(content=f"{receipt.path}：{receipt.message}", details=payload)
        _audit(
            ctx,
            "INFO",
            "记忆删除",
            path=receipt.path,
            removed=len(receipt.removed),
            verified=receipt.verified,
        )
        return ToolResult(
            content=f"已从 {receipt.path} 删除 {len(receipt.removed)} 条：校验通过",
            details=payload,
        )

    async def status(ctx, args: EmptyIn) -> ToolResult:
        store = _store(ctx)
        if store is None:
            return ToolResult.error("当前会话没有长期记忆目录（learned/）")
        info = store.status()
        lines = [
            f"常驻记忆: {info['resident']['path']}"
            f"（{info['resident']['chars']}/{info['resident']['limit']} 字符）",
            f"wiki: {info['wiki']['path']}（{info['wiki']['pages']} 页："
            f"{'、'.join(info['wiki']['topics']) or '无'}）",
            f"索引: {info['index']['path']}（{info['index']['entries']} 条）",
        ]
        return ToolResult(content="\n".join(lines), details=info)

    api.register_tool(
        Tool(
            name="memory_remember",
            description=(
                "把用户明确要求记住、或已确认的稳定事实/流程写入长期记忆"
                "（memory 常驻、wiki 知识页、skill 可复用流程）。"
                "同一主题用 key 去重；覆盖旧条目会先征得用户确认。"
                "写入后必须告知具体文件路径；未确认的推断不要写入"
            ),
            params=RememberIn,
            handler=remember,
        )
    )
    api.register_tool(
        Tool(
            name="memory_search",
            description="检索已沉淀的长期记忆与 wiki 页，返回命中路径与片段",
            params=SearchIn,
            handler=search,
        )
    )
    api.register_tool(
        Tool(
            name="memory_forget",
            description="删除常驻记忆条目，或删除整个 wiki 页（删除页面需要用户确认）",
            params=ForgetIn,
            handler=forget,
        )
    )
    api.register_tool(
        Tool(
            name="memory_status",
            description="查看长期记忆规模与路径，判断是否需要整理",
            params=EmptyIn,
            handler=status,
        )
    )


__all__ = ["setup"]
