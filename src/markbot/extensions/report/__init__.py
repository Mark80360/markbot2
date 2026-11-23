"""交付报告：``write_report``（ADR 0013）。

把本会话的变更清单（改了哪些文件、增删行数）与验证记录（跑过哪些测试/检查、结果）自动汇总，
加上模型写的背景、改动说明、风险与后续，生成 Markdown 报告，写到会话临时目录
``<home>/sessions/<id>/scratch/reports/``。回复里用 ``[报告](路径)`` 链接，飞书渠道会把文件一并发出。

报告只写入会话临时目录（经执行环境的路径守卫），因此不声明 fs 能力、不需要逐次确认。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult
from markbot.extensions import _tracking as tracking

TEMPLATES = {
    "dev_report": ("开发报告", ["背景", "改动说明", "改动文件", "验证", "风险与限制", "后续建议"]),
    "research": ("调研报告", ["背景", "结论", "详细发现", "来源", "风险与限制", "后续建议"]),
    "incident": ("故障报告", ["背景", "影响", "原因", "处理过程", "改动文件", "验证", "后续预防"]),
}


class ReportIn(BaseModel):
    title: str = Field(description="报告标题", min_length=2, max_length=120)
    template: Literal["dev_report", "research", "incident"] = Field(default="dev_report")
    summary: str = Field(description="一段话总结：做了什么、结果如何")
    background: str = Field(default="", description="背景与目标")
    details: str = Field(
        default="",
        description="正文：开发报告写改动说明（为什么这样改）；调研写详细发现；故障写原因与处理过程",
    )
    sources: list[str] = Field(default_factory=list, description="来源链接（调研）")
    risks: str = Field(default="", description="风险、限制、未解决的问题")
    next_steps: str = Field(default="", description="后续建议")


def _slug(text: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text.strip()).strip("-")
    return slug[:40] or "report"


def _verifications(session: Any) -> str:
    records = getattr(session, "verifications", None) or []
    if not records:
        return "（本会话没有运行测试或检查）"
    lines = []
    for item in records[-10:]:
        stamp = datetime.fromtimestamp(item.get("at", 0)).astimezone().strftime("%H:%M")
        state = "✅ 通过" if item.get("ok") else "❌ 未通过"
        lines.append(f"- {stamp} {item.get('kind')} {state}：{item.get('summary', '')}")
    return "\n".join(lines)


def _report_dir(ctx) -> Path | None:
    guard = getattr(ctx.env, "guard", None)
    home = getattr(guard, "home", None)
    session_id = getattr(ctx.session, "id", None)
    if home is None or not session_id:
        return None
    return Path(home) / "sessions" / str(session_id) / "scratch" / "reports"


def render(args: ReportIn, session: Any) -> str:
    kind, sections = TEMPLATES[args.template]
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    body = {
        "背景": args.background,
        "改动说明": args.details,
        "结论": args.summary,
        "详细发现": args.details,
        "原因": args.details,
        "处理过程": "",
        "影响": "",
        "改动文件": tracking.changes_summary(session) or "（本会话没有修改文件）",
        "验证": _verifications(session),
        "来源": "\n".join(f"- {item}" for item in args.sources) or "（未提供）",
        "风险与限制": args.risks,
        "后续建议": args.next_steps,
        "后续预防": args.next_steps,
    }
    lines = [f"# {args.title}", "", f"> {kind} · {now} · 会话 {getattr(session, 'id', '-')}", "", "## 摘要", "",
             args.summary.strip(), ""]
    for name in sections:
        if name == "结论":
            continue
        text = str(body.get(name) or "").strip()
        if not text:
            continue
        lines += [f"## {name}", "", text, ""]
    return "\n".join(lines).rstrip() + "\n"


def setup(api: ExtensionAPI) -> None:
    async def write_report(ctx, args: ReportIn) -> ToolResult:
        directory = _report_dir(ctx)
        if directory is None:
            return ToolResult.error("当前环境没有会话临时目录，无法生成报告")
        name = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_slug(args.title)}.md"
        target = directory / name
        text = render(args, ctx.session)
        try:
            written = ctx.env.write_text(str(target), text)
        except PermissionError as exc:
            return ToolResult.error(f"报告写入被拒绝: {exc}", denied=True)
        uri = Path(written).as_posix()
        return ToolResult(
            content=(
                f"报告已生成：{written}（{len(text)} 字符）\n"
                f"在最终回复里附上链接 [{args.title}]({uri}) ，聊天渠道会把文件一起发送。\n\n"
                + text[:1500]
            ),
            details={"path": str(written), "chars": len(text)},
        )

    api.register_tool(
        Tool(
            name="write_report",
            description=(
                "生成交付报告（Markdown）：自动汇总本会话改过的文件与测试/检查结果，加上你写的摘要、背景、"
                "说明、风险与后续。开发任务完成时调用，并在回复里附上报告链接"
            ),
            params=ReportIn,
            handler=write_report,
        )
    )


__all__ = ["TEMPLATES", "render", "setup"]
