"""``markbot cron``：命令行管理定时任务。

调度器本身跑在 ``markbot serve`` / ``markbot gateway`` 进程里；这里的命令
只读写同一份任务文件，``run`` 会临时构建 Kernel 立即执行一次。
"""

from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

from markbot.api.ui import NullUI
from markbot.cron.runner import build_job_runner
from markbot.cron.service import CronError, CronService, resolve_timezone
from markbot.cron.tool import _parse_at, _format_job
from markbot.cron.types import CronSchedule, ms_to_iso

USAGE = """用法：
  markbot cron list [--all]
  markbot cron add --message "任务内容" (--every 3600 | --cron "0 9 * * *" | --at "2026-12-01 09:00")
                          [--name 名称] [--tz Asia/Shanghai]
  markbot cron remove <job_id>
  markbot cron enable|disable <job_id>
  markbot cron run <job_id>       立即执行一次（需要可用 Provider）
  markbot cron history <job_id> [--limit 5]

说明：调度器在 `markbot serve` / `markbot gateway start` 里运行；
      命令行创建的任务只归档，不发聊天消息。"""


def _flag(argv: list[str], name: str, default: str | None = None) -> str | None:
    if name not in argv:
        return default
    index = argv.index(name)
    return argv[index + 1] if index + 1 < len(argv) else default


def _service() -> CronService:
    from markbot.kernel.config import load_config

    cfg = load_config()
    return CronService(
        Path(cfg.home) / "cron" / "jobs.json",
        default_delivery=cfg.cron.default_delivery,
        default_timezone=cfg.cron.timezone or None,
    )


def cron_command(argv: list[str]) -> int:
    if not argv or argv[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0
    sub, rest = argv[0], argv[1:]
    service = _service()
    try:
        if sub == "list":
            return _list(service, "--all" in rest)
        if sub == "add":
            return _add(service, rest)
        if sub == "remove":
            return _remove(service, rest)
        if sub in ("enable", "disable"):
            return _toggle(service, rest, sub == "enable")
        if sub == "history":
            return _history(service, rest)
        if sub == "run":
            return _run(service, rest)
    except CronError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"未知子命令: {sub}\n{USAGE}", file=sys.stderr)
    return 2


def _list(service: CronService, include_disabled: bool) -> int:
    jobs = service.list_jobs(include_disabled=include_disabled)
    if not jobs:
        print("（没有定时任务）")
        return 0
    for job in jobs:
        print(_format_job(job))
    return 0


def _add(service: CronService, rest: list[str]) -> int:
    message = _flag(rest, "--message") or ""
    if not message.strip():
        raise CronError("add 需要 --message")
    every = _flag(rest, "--every") or _flag(rest, "--every-seconds")
    expr = _flag(rest, "--cron") or ""
    at = _flag(rest, "--at") or ""
    chosen = [name for name, value in (("--every", every), ("--cron", expr), ("--at", at)) if value]
    if len(chosen) != 1:
        raise CronError("--every / --cron / --at 必须且只能选一个")
    tz_name = _flag(rest, "--tz") or service.default_timezone
    if every:
        try:
            seconds = int(str(every))
        except ValueError as exc:
            raise CronError("--every 需要整数秒") from exc
        schedule = CronSchedule(kind="every", every_ms=seconds * 1000)
    elif expr:
        if tz_name:
            resolve_timezone(tz_name)
        schedule = CronSchedule(kind="cron", expr=str(expr).strip(), tz=tz_name)
    else:
        schedule = CronSchedule(kind="at", at_ms=_parse_at(str(at), tz_name))
    job = service.add_job(
        name=_flag(rest, "--name") or message,
        message=message,
        schedule=schedule,
        delivery="none",
    )
    print(f"已创建定时任务 {job.id}（{job.name}）")
    print(f"调度: {schedule.describe()} | 下次: {ms_to_iso(job.state.next_run_at_ms)}")
    print("注意：命令行创建的任务只归档，不发聊天消息；调度器需在 serve/gateway 中运行。")
    return 0


def _remove(service: CronService, rest: list[str]) -> int:
    job_id = _required_id(rest)
    if not service.remove_job(job_id):
        raise CronError(f"找不到定时任务: {job_id}")
    print(f"已删除 {job_id}")
    return 0


def _toggle(service: CronService, rest: list[str], enabled: bool) -> int:
    job = service.set_enabled(_required_id(rest), enabled)
    print(f"已{'启用' if enabled else '停用'} {job.id} {job.name}")
    if job.state.next_run_at_ms:
        print(f"下次执行: {ms_to_iso(job.state.next_run_at_ms)}")
    return 0


def _history(service: CronService, rest: list[str]) -> int:
    job_id = _required_id(rest)
    job = service.get_job(job_id)
    if job is None:
        raise CronError(f"找不到定时任务: {job_id}")
    limit = 5
    raw = _flag(rest, "--limit")
    if raw:
        try:
            limit = max(1, int(raw))
        except ValueError:
            raise CronError("--limit 需要整数") from None
    if not job.state.history:
        print("（还没有运行记录）")
        return 0
    for run in job.state.history[:limit]:
        print(
            f"- {ms_to_iso(run.started_at_ms)} {run.status} "
            f"耗时 {run.finished_at_ms - run.started_at_ms}ms"
        )
        if run.error:
            print(f"  错误: {run.error}")
        preview = run.response.strip().replace("\n", " ")
        if preview:
            print(f"  结果: {preview[:300]}")
    return 0


def _run(service: CronService, rest: list[str]) -> int:
    job_id = _required_id(rest)
    from markbot.kernel.app import Kernel
    from markbot.kernel.config import load_config
    from markbot.kernel.providers import ProviderError, build_provider

    cfg = load_config()
    try:
        provider = build_provider(cfg, cfg.secrets)
    except ProviderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print('提示：在 config.toml 设置 provider = "echo" 可离线验证。', file=sys.stderr)
        return 2
    kernel = Kernel.build(config=cfg, provider=provider, ui=NullUI())
    service.on_job = build_job_runner(kernel, run_lock=threading.Lock())
    try:
        record = asyncio.run(service.run_job_once(job_id))
    finally:
        asyncio.run(kernel.shutdown())
    print(f"{record.status} 耗时 {record.finished_at_ms - record.started_at_ms}ms")
    if record.error:
        print(f"error: {record.error}", file=sys.stderr)
    if record.response:
        print(record.response)
    return 0 if record.status == "ok" else 1


def _required_id(rest: list[str]) -> str:
    args = [item for item in rest if not item.startswith("--")]
    if not args:
        raise CronError("需要 job_id")
    return args[0]


__all__ = ["cron_command"]
