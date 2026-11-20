"""``cron`` 工具：让 Agent 管理定时任务。

只做参数解析和展示，真正的调度与执行在 ``CronService`` / ``runner`` 里。
任务默认绑定调用它的会话，并沿用该会话的渠道投递信息。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from markbot.api.tool import Tool, ToolContext, ToolResult
from markbot.cron.service import CronError, CronService, resolve_timezone
from markbot.cron.types import CronJob, CronSchedule, ms_to_iso, now_ms

ServiceProvider = Callable[[], Optional[CronService]]


class CronIn(BaseModel):
    action: Literal[
        "add", "list", "update", "remove", "enable", "disable", "history", "run"
    ] = Field(
        description="add 新建；list 查看；update 修改；remove 删除；enable/disable 启停；"
        "history 查看运行历史；run 立即触发一次"
    )
    name: str = Field(default="", description="任务名，留空取任务内容前 60 字")
    message: str = Field(
        default="",
        description="到点后交给 Agent 执行的任务内容（add 必填，update 用于修改）",
    )
    every_seconds: int | None = Field(
        default=None, description="间隔执行：每多少秒一次（与 cron_expr/at 三选一）"
    )
    cron_expr: str = Field(
        default="", description="crontab 表达式，如 '0 9 * * 1-5'（与 every_seconds/at 三选一）"
    )
    at: str = Field(
        default="", description="一次性执行时间，ISO8601 或 'YYYY-MM-DD HH:MM'（与另外两个三选一）"
    )
    tz: str = Field(default="", description="cron 表达式/at 的时区，如 Asia/Shanghai；留空用默认时区")
    job_id: str = Field(default="", description="目标任务 id（remove/update/enable/disable/history/run 必填）")
    delivery: Literal["", "auto", "none", "channel"] = Field(
        default="",
        description="结果投递：auto 有渠道就发回聊天，否则只归档；none 只归档+日志；"
        "channel 必须发回创建任务的聊天",
    )
    include_disabled: bool = Field(default=False, description="list 时是否包含已停用任务")
    limit: int = Field(default=5, ge=1, le=20, description="history 返回的条数")


DESCRIPTION = (
    "管理定时任务（cron）。任务到点后会以一条提醒消息驱动当前 Agent 执行任务内容，"
    "复用同一套工具、权限与会话归档。支持的调度：every_seconds（固定间隔）、"
    "cron_expr（crontab 表达式）、at（一次性时间），三者只能选一个。"
    "delivery=channel 时任务会把最终回复发回创建任务的聊天，否则只归档并写日志。"
)


def build_cron_tool(service_provider: ServiceProvider) -> Tool:
    async def handler(ctx: ToolContext, args: CronIn) -> ToolResult:
        try:
            service = service_provider()
            if service is None:
                return ToolResult.error(
                    "定时任务服务未运行：请通过 `markbot gateway start` 或 `markbot serve` 启动。"
                )
            return _handle(service, ctx, args)
        except CronError as exc:
            return ToolResult.error(str(exc))
        except Exception as exc:  # noqa: BLE001 - 工具边界统一转成可读错误
            return ToolResult.error(f"定时任务操作失败: {type(exc).__name__}: {exc}")

    return Tool(
        name="cron",
        description=DESCRIPTION,
        params=CronIn,
        handler=handler,
        source="builtin",
    )


def _handle(service: CronService, ctx: ToolContext, args: CronIn) -> ToolResult:
    action = args.action
    if action == "add":
        return _add(service, ctx, args)
    if action == "list":
        return _list(service, args.include_disabled)
    if action == "update":
        return _update(service, ctx, args)
    if action == "remove":
        if not service.remove_job(_require_id(args)):
            return ToolResult.error(f"找不到定时任务: {args.job_id}")
        return ToolResult(f"已删除定时任务 {args.job_id}")
    if action in ("enable", "disable"):
        job = service.set_enabled(_require_id(args), action == "enable")
        return ToolResult(
            f"已{'启用' if action == 'enable' else '停用'}任务 {job.id} {job.name}\n"
            f"下次执行: {ms_to_iso(job.state.next_run_at_ms) if job.enabled else '-'}"
        )
    if action == "history":
        return _history(service, args)
    if action == "run":
        job = service.trigger_now(_require_id(args))
        return ToolResult(f"已触发任务 {job.id} {job.name}，将在后台串行执行。")
    raise CronError(f"未知操作: {action}")


def _add(service: CronService, ctx: ToolContext, args: CronIn) -> ToolResult:
    if not args.message.strip():
        raise CronError("add 需要 message")
    metadata = _metadata(ctx)
    if metadata.get("cron"):
        raise CronError("定时任务内不能再创建定时任务")
    notify = _notify(metadata)
    delivery = _resolve_delivery(args.delivery, service, notify)
    schedule = _schedule(args, service)
    session_id = str(getattr(ctx.session, "id", "") or "") or None
    job = service.add_job(
        name=args.name or args.message,
        message=args.message,
        schedule=schedule,
        session_id=session_id,
        delivery=delivery,
        notify=notify if delivery == "channel" else {},
    )
    return ToolResult(
        f"已创建定时任务 {job.id}（{job.name}）\n"
        f"调度: {schedule.describe()}\n"
        f"下次执行: {ms_to_iso(job.state.next_run_at_ms)}\n"
        f"投递: {_delivery_label(job)}"
    )


def _list(service: CronService, include_disabled: bool) -> ToolResult:
    jobs = service.list_jobs(include_disabled=include_disabled)
    if not jobs:
        return ToolResult("（没有定时任务）")
    lines = [f"定时任务 {len(jobs)} 个:"]
    for job in jobs:
        lines.append(_format_job(job))
    return ToolResult("\n\n".join(lines))


def _update(service: CronService, ctx: ToolContext, args: CronIn) -> ToolResult:
    job_id = _require_id(args)
    schedule = None
    if args.every_seconds is not None or args.cron_expr.strip() or args.at.strip():
        schedule = _schedule(args, service)
    delivery = None
    notify = None
    if args.delivery:
        metadata = _metadata(ctx)
        notify = _notify(metadata)
        delivery = _resolve_delivery(args.delivery, service, notify)
        notify = notify if delivery == "channel" else {}
    job = service.update_job(
        job_id,
        name=args.name or None,
        message=args.message or None,
        schedule=schedule,
        delivery=delivery,
        notify=notify,
    )
    return ToolResult(
        f"已更新定时任务 {job.id}（{job.name}）\n{_format_job(job)}"
    )


def _history(service: CronService, args: CronIn) -> ToolResult:
    job = service.get_job(_require_id(args))
    if job is None:
        raise CronError(f"找不到定时任务: {args.job_id}")
    if not job.state.history:
        return ToolResult(f"任务 {job.id} 还没有运行记录。")
    lines = [f"任务 {job.id}（{job.name}）最近 {min(args.limit, len(job.state.history))} 次运行:"]
    for run in job.state.history[: args.limit]:
        head = (
            f"- {ms_to_iso(run.started_at_ms)} {run.status} "
            f"耗时 {run.finished_at_ms - run.started_at_ms}ms"
        )
        lines.append(head)
        if run.error:
            lines.append(f"  错误: {run.error}")
        preview = run.response.strip().replace("\n", " ")
        if preview:
            lines.append(f"  结果: {preview[:200]}")
    return ToolResult("\n".join(lines))


def _require_id(args: CronIn) -> str:
    job_id = args.job_id.strip()
    if not job_id:
        raise CronError(f"{args.action} 需要 job_id")
    return job_id


def _schedule(args: CronIn, service: CronService) -> CronSchedule:
    given = [
        ("every_seconds", args.every_seconds is not None),
        ("cron_expr", bool(args.cron_expr.strip())),
        ("at", bool(args.at.strip())),
    ]
    chosen = [name for name, present in given if present]
    if len(chosen) != 1:
        raise CronError("请从 every_seconds、cron_expr、at 中选择且只选一个")
    kind = chosen[0]
    tz_name = args.tz.strip() or service.default_timezone
    if kind == "every_seconds":
        seconds = int(args.every_seconds or 0)
        if seconds <= 0:
            raise CronError("every_seconds 必须大于 0")
        return CronSchedule(kind="every", every_ms=seconds * 1000)
    if kind == "cron_expr":
        expr = args.cron_expr.strip()
        if tz_name:
            resolve_timezone(tz_name)
        return CronSchedule(kind="cron", expr=expr, tz=tz_name)
    return CronSchedule(kind="at", at_ms=_parse_at(args.at, tz_name))


def _parse_at(raw: str, tz_name: str | None) -> int:
    text = raw.strip()
    value: datetime | None = None
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                value = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if value is None:
        raise CronError(f"无法解析时间: {raw}（用 ISO8601 或 YYYY-MM-DD HH:MM）")
    if value.tzinfo is None:
        value = value.replace(tzinfo=resolve_timezone(tz_name))
    at_ms = int(value.timestamp() * 1000)
    if at_ms <= now_ms():
        raise CronError("at 时间已经过去")
    return at_ms


def _metadata(ctx: ToolContext) -> dict[str, Any]:
    metadata = getattr(ctx.session, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _notify(metadata: dict[str, Any]) -> dict[str, str]:
    raw = metadata.get("notify")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value}


def _resolve_delivery(
    requested: str, service: CronService, notify: dict[str, str]
) -> str:
    choice = (requested or service.default_delivery or "auto").strip()
    has_channel = bool(notify.get("url") and notify.get("chat_id"))
    if choice == "auto":
        return "channel" if has_channel else "none"
    if choice == "channel":
        if not has_channel:
            raise CronError(
                "当前会话没有渠道回调，结果无法发回聊天；"
                "请把 delivery 改成 none，或在聊天窗口里创建任务。"
            )
        return "channel"
    if choice == "none":
        return "none"
    raise CronError("delivery 只能是 auto、none 或 channel")


def _delivery_label(job: CronJob) -> str:
    if job.payload.delivery == "channel":
        chat = job.payload.notify.get("chat_id", "")
        return f"发回聊天 {chat}" if chat else "发回聊天（未绑定）"
    return "仅归档 + 日志"


def _format_job(job: CronJob) -> str:
    state = "启用" if job.enabled else "停用"
    last = job.state.last_status or "从未运行"
    if job.state.last_error:
        last += f"（{job.state.last_error[:100]}）"
    return (
        f"- {job.id} {job.name} [{state}]\n"
        f"  调度: {job.schedule.describe()} | 下次: {ms_to_iso(job.state.next_run_at_ms)}\n"
        f"  上次: {last} | 投递: {_delivery_label(job)}"
    )


__all__ = ["CronIn", "DESCRIPTION", "build_cron_tool"]
