"""定时任务服务：持久化、到点触发与运行历史。

只负责“什么时候跑”，不关心“怎么跑”：到点后调用 ``on_job`` 回调，由
``markbot.cron.runner`` 复用 ``Kernel.run`` 执行。调度器线程与 HTTP 请求线程
共享同一个 job 文件，所有读写都在锁内完成。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from markbot.cron.types import (
    CronJob,
    CronJobState,
    CronPayload,
    CronRun,
    CronSchedule,
    new_job_id,
    new_run_id,
    now_ms,
)

logger = logging.getLogger("markbot.cron")

CronCallback = Callable[[CronJob], Awaitable[str]]

_SERVICE: "CronService | None" = None


def set_service(service: "CronService | None") -> None:
    """登记进程内唯一的调度服务，供 cron 工具和关闭流程使用。"""
    global _SERVICE
    _SERVICE = service


def get_service() -> "CronService | None":
    return _SERVICE


class CronError(ValueError):
    """调度参数或任务状态不合法。"""


class CronStoreError(CronError):
    """任务文件不可读。"""


def resolve_timezone(name: str | None):
    if not name:
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(f"未知时区: {name}") from exc


def cron_next_ms(expr: str, tz_name: str | None, base_ms: int) -> int:
    try:
        from croniter import croniter
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise CronError(
            "cron 表达式需要 croniter：pip install 'markbot[cron]'"
        ) from exc
    tz = resolve_timezone(tz_name)
    base = datetime.fromtimestamp(base_ms / 1000, tz=tz)
    try:
        nxt = croniter(expr, base).get_next(datetime)
    except Exception as exc:  # noqa: BLE001 - croniter 的异常类型不统一
        raise CronError(f"cron 表达式不合法: {expr} -> {exc}") from exc
    return int(nxt.timestamp() * 1000)


class CronService:
    def __init__(
        self,
        store_path: Path | str,
        *,
        on_job: CronCallback | None = None,
        default_delivery: str = "auto",
        default_timezone: str | None = None,
        max_history: int = 3,
        max_delay_s: float = 60.0,
        max_concurrent: int = 2,
        job_timeout_s: float = 1800.0,
        max_retries: int = 1,
        retry_backoff_s: float = 300.0,
        misfire_policy: str = "run_once",
        misfire_grace_s: float = 6 * 3600.0,
        alert_after_failures: int = 2,
        on_failure: Callable[[CronJob, int, str], None] | None = None,
    ) -> None:
        self.store_path = Path(store_path)
        self.on_job = on_job
        self.default_delivery = (
            default_delivery if default_delivery in ("auto", "none", "channel") else "auto"
        )
        self.default_timezone = default_timezone or None
        self.max_history = max(1, int(max_history))
        self.max_delay_s = max(0.05, float(max_delay_s))
        # 可靠性（ADR 0010）：并发执行、单次超时、失败重试、错过触发的补跑策略、连续失败告警
        self.max_concurrent = max(1, int(max_concurrent))
        self.job_timeout_s = max(0.0, float(job_timeout_s or 0))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_s = max(1.0, float(retry_backoff_s))
        self.misfire_policy = misfire_policy if misfire_policy in ("skip", "run_once") else "run_once"
        self.misfire_grace_s = max(0.0, float(misfire_grace_s))
        self.alert_after_failures = max(0, int(alert_after_failures))
        self.on_failure = on_failure
        self._inflight: dict[str, asyncio.Task[CronRun]] = {}
        self._retries: dict[str, int] = {}

        self._lock = threading.RLock()
        self._running = False
        self._stop_requested = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake: asyncio.Event | None = None

    # ------------------------------------------------------------- persistence
    @property
    def store_dir(self):
        return self.store_path.parent

    def _load(self) -> list[CronJob]:
        if not self.store_path.exists():
            return []
        try:
            raw = json.loads(self.store_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CronStoreError(f"读取定时任务失败: {self.store_path} -> {exc}") from exc
        if isinstance(raw, dict):
            items = raw.get("jobs") or []
        elif isinstance(raw, list):
            items = raw
        else:
            items = []
        jobs: list[CronJob] = []
        for item in items:
            if isinstance(item, dict):
                jobs.append(CronJob.from_dict(item))
        return jobs

    def _save(self, jobs: list[CronJob]) -> None:
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "jobs": [job.to_dict() for job in jobs]}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        tmp = self.store_path.with_name(
            f".{self.store_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        tmp.write_text(text + "\n", encoding="utf-8")
        os.replace(tmp, self.store_path)

    def _write_run(self, job: CronJob, run: CronRun) -> None:
        target = self.store_dir / "runs" / job.id
        try:
            target.mkdir(parents=True, exist_ok=True)
            data = {"job_id": job.id, "job_name": job.name, "run": run.to_dict()}
            (target / f"{run.run_id}.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            logger.warning("写入运行记录失败 job=%s run=%s", job.id, run.run_id)

    # ----------------------------------------------------------------- queries
    def list_jobs(self, *, include_disabled: bool = True) -> list[CronJob]:
        with self._lock:
            jobs = self._load()
        return sorted(
            (job for job in jobs if include_disabled or job.enabled),
            key=lambda job: (job.state.next_run_at_ms or 0, job.created_at_ms),
        )

    def get_job(self, job_id: str) -> CronJob | None:
        with self._lock:
            for job in self._load():
                if job.id == job_id:
                    return job
        return None

    # -------------------------------------------------------------------- CRUD
    def add_job(
        self,
        *,
        name: str,
        message: str,
        schedule: CronSchedule,
        session_id: str | None = None,
        delivery: str = "none",
        notify: dict[str, str] | None = None,
        delete_after_run: bool | None = None,
    ) -> CronJob:
        if not message.strip():
            raise CronError("任务内容不能为空")
        now = now_ms()
        next_run = self._initial_next(schedule, now)
        if schedule.kind == "at" and (next_run is None or next_run <= now):
            raise CronError("at 时间已经过去")
        if schedule.kind == "every" and not schedule.every_ms:
            raise CronError("every_seconds 必须大于 0")
        if delivery not in ("none", "channel"):
            raise CronError("delivery 只能是 none 或 channel")
        job = CronJob(
            id=new_job_id(),
            name=(name or message).strip()[:60],
            enabled=True,
            schedule=schedule,
            payload=CronPayload(
                message=message.strip(),
                session_id=session_id,
                delivery=delivery,  # type: ignore[arg-type]
                notify=dict(notify or {}),
            ),
            state=CronJobState(next_run_at_ms=next_run),
            created_at_ms=now,
            updated_at_ms=now,
            delete_after_run=(
                schedule.kind == "at" if delete_after_run is None else delete_after_run
            ),
        )
        with self._lock:
            jobs = self._load()
            jobs.append(job)
            self._save(jobs)
        self.wake()
        logger.info(
            "新增定时任务 id=%s name=%s schedule=%s delivery=%s",
            job.id,
            job.name,
            schedule.describe(),
            delivery,
        )
        return job

    def update_job(
        self,
        job_id: str,
        *,
        name: str | None = None,
        message: str | None = None,
        schedule: CronSchedule | None = None,
        delivery: str | None = None,
        notify: dict[str, str] | None = None,
    ) -> CronJob:
        now = now_ms()
        with self._lock:
            jobs = self._load()
            job = next((item for item in jobs if item.id == job_id), None)
            if job is None:
                raise CronError(f"找不到定时任务: {job_id}")
            if name is not None:
                job.name = name.strip()[:60] or job.name
            if message is not None:
                if not message.strip():
                    raise CronError("任务内容不能为空")
                job.payload.message = message.strip()
            if delivery is not None:
                if delivery not in ("none", "channel"):
                    raise CronError("delivery 只能是 none 或 channel")
                job.payload.delivery = delivery  # type: ignore[assignment]
                if notify is not None:
                    job.payload.notify = dict(notify)
            if schedule is not None:
                next_run = self._initial_next(schedule, now)
                if schedule.kind == "at" and (next_run is None or next_run <= now):
                    raise CronError("at 时间已经过去")
                job.schedule = schedule
                job.state.next_run_at_ms = next_run
                job.enabled = next_run is not None
            job.updated_at_ms = now
            self._save(jobs)
        self.wake()
        logger.info("更新定时任务 id=%s", job_id)
        return job

    def remove_job(self, job_id: str) -> bool:
        with self._lock:
            jobs = self._load()
            kept = [job for job in jobs if job.id != job_id]
            if len(kept) == len(jobs):
                return False
            self._save(kept)
        self.wake()
        logger.info("删除定时任务 id=%s", job_id)
        return True

    def set_enabled(self, job_id: str, enabled: bool) -> CronJob:
        now = now_ms()
        with self._lock:
            jobs = self._load()
            job = next((item for item in jobs if item.id == job_id), None)
            if job is None:
                raise CronError(f"找不到定时任务: {job_id}")
            job.enabled = bool(enabled)
            job.updated_at_ms = now
            if job.enabled and job.state.next_run_at_ms is None:
                job.state.next_run_at_ms = self._initial_next(job.schedule, now)
            self._save(jobs)
        self.wake()
        logger.info("%s定时任务 id=%s", "启用" if enabled else "停用", job_id)
        return job

    def trigger_now(self, job_id: str) -> CronJob:
        """把下次执行时间提前到现在，由调度线程串行执行（不阻塞当前回合）。"""
        now = now_ms()
        with self._lock:
            jobs = self._load()
            job = next((item for item in jobs if item.id == job_id), None)
            if job is None:
                raise CronError(f"找不到定时任务: {job_id}")
            job.enabled = True
            job.state.next_run_at_ms = now
            job.updated_at_ms = now
            self._save(jobs)
        self.wake()
        return job

    async def run_job_once(self, job_id: str) -> CronRun:
        """立即执行一次（CLI 手动触发），不走调度线程。"""
        job = self.get_job(job_id)
        if job is None:
            raise CronError(f"找不到定时任务: {job_id}")
        return await self._execute(job)

    # -------------------------------------------------------------- scheduling
    def start_in_thread(self, *, name: str = "cron-scheduler") -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop_requested = False
        self._thread = threading.Thread(
            target=self._thread_main, daemon=True, name=name
        )
        self._thread.start()
        return self._thread

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._stop_requested

    def running_jobs(self) -> list[str]:
        return sorted(self._inflight)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._run_forever())
        except Exception:  # noqa: BLE001 - 调度线程不能静默死掉
            logger.exception("定时任务调度线程异常退出")

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_requested = True
        self._running = False
        self._notify_loop()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def wake(self) -> None:
        self._notify_loop()

    def _notify_loop(self) -> None:
        loop = self._loop
        event = self._wake
        if loop is None or event is None:
            return
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # 事件循环已关闭
            pass

    async def _run_forever(self) -> None:
        if self._stop_requested:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._running = True
        try:
            self._resume_jobs()
        except CronStoreError:
            logger.exception("定时任务文件读取失败，调度器未启动")
            self._running = False
            return
        logger.info("定时任务调度器已启动")
        try:
            while self._running and not self._stop_requested:
                try:
                    delay = self._next_delay_s()
                except CronStoreError:
                    logger.exception("定时任务文件读取失败，稍后重试")
                    delay = self.max_delay_s
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
                else:
                    self._wake.clear()
                if not self._running or self._stop_requested:
                    break
                try:
                    await self._tick()
                except Exception:  # noqa: BLE001 - 单个任务失败不能拖垮调度器
                    logger.exception("定时任务执行异常")
        finally:
            logger.info("定时任务调度器已停止")
            self._loop = None
            self._wake = None

    def _resume_jobs(self) -> None:
        now = now_ms()
        with self._lock:
            jobs = self._load()
            changed = False
            for job in jobs:
                if job.enabled and job.state.next_run_at_ms is not None:
                    changed |= self._apply_misfire(job, now)
                    continue
                if not job.enabled or job.state.next_run_at_ms is not None:
                    continue
                try:
                    job.state.next_run_at_ms = self._initial_next(job.schedule, now)
                except CronError as exc:
                    job.state.last_error = str(exc)
                    job.enabled = False
                if job.schedule.kind == "at" and job.state.next_run_at_ms is None:
                    job.enabled = False
                changed = True
            if changed:
                self._save(jobs)

    def _apply_misfire(self, job: CronJob, now: int) -> bool:
        """启动时处理停机期间错过的触发：在宽限期内补跑一次，否则跳到下一个周期。"""
        due = job.state.next_run_at_ms
        if due is None or due >= now:
            return False
        overdue_s = (now - due) / 1000
        if self.misfire_policy == "run_once" and overdue_s <= self.misfire_grace_s:
            logger.info("定时任务错过触发，将补跑一次 id=%s 逾期=%ds", job.id, int(overdue_s))
            return False
        try:
            job.state.next_run_at_ms = (
                None if job.schedule.kind == "at" else self._initial_next(job.schedule, now)
            )
        except CronError as exc:
            job.state.next_run_at_ms = None
            job.state.last_error = str(exc)
        if job.state.next_run_at_ms is None:
            job.enabled = False
        job.state.last_status = "skipped"
        job.state.last_error = f"停机期间错过触发（逾期 {int(overdue_s)}s），已按 {self.misfire_policy} 策略跳过"
        logger.warning("定时任务错过触发已跳过 id=%s 逾期=%ds", job.id, int(overdue_s))
        return True

    def _next_delay_s(self) -> float:
        now = now_ms()
        with self._lock:
            jobs = self._load()
        upcoming = [
            job.state.next_run_at_ms
            for job in jobs
            if job.enabled
            and job.state.next_run_at_ms is not None
            and job.id not in self._inflight
        ]
        if not upcoming:
            return self.max_delay_s
        remaining = min(upcoming) - now
        return max(0.05, min(self.max_delay_s, remaining / 1000))

    async def _tick(self) -> None:
        """把到点的任务各自放进后台任务并发执行（不再串行 await，长任务不阻塞其它任务）。

        执行期间不清空 ``next_run_at_ms``：进程在执行中崩溃时，重启后按补跑策略处理
        （宽限期内补跑一次），而不是像旧实现那样静默丢失这次执行。
        """
        now = now_ms()
        with self._lock:
            due = [
                job
                for job in self._load()
                if job.enabled
                and job.state.next_run_at_ms is not None
                and job.state.next_run_at_ms <= now
                and job.id not in self._inflight
            ]
        for job in sorted(due, key=lambda item: item.state.next_run_at_ms or 0):
            if len(self._inflight) >= self.max_concurrent:
                logger.info("定时任务并发已满，稍后执行 id=%s", job.id)
                break
            task = asyncio.create_task(self._execute(job), name=f"cron-{job.id}")
            self._inflight[job.id] = task
            task.add_done_callback(lambda _t, job_id=job.id: self._job_done(job_id))

    def _job_done(self, job_id: str) -> None:
        self._inflight.pop(job_id, None)
        wake = self._wake
        if wake is not None:
            wake.set()

    async def _execute(self, job: CronJob) -> CronRun:
        if self.on_job is None:
            raise CronError("没有配置任务执行器")
        started = now_ms()
        status = "ok"
        error = ""
        response = ""
        try:
            if self.job_timeout_s > 0:
                response = await asyncio.wait_for(self.on_job(job), timeout=self.job_timeout_s)
            else:
                response = await self.on_job(job)
        except asyncio.TimeoutError:
            status = "error"
            error = f"执行超时（超过 {int(self.job_timeout_s)}s），已取消"
            logger.warning("定时任务超时 id=%s name=%s", job.id, job.name)
        except Exception as exc:  # noqa: BLE001 - 失败写进历史，调度继续
            status = "error"
            error = f"{type(exc).__name__}: {exc}"
            logger.warning("定时任务失败 id=%s name=%s", job.id, job.name, exc_info=True)
        finished = now_ms()
        record = CronRun(
            run_id=new_run_id(),
            started_at_ms=started,
            finished_at_ms=finished,
            status=status,
            error=error,
            response=response or "",
        )
        with self._lock:
            jobs = self._load()
            fresh = next((item for item in jobs if item.id == job.id), None)
            if fresh is None:
                return record
            fresh.state.last_run_at_ms = started
            fresh.state.last_status = status
            fresh.state.last_error = error
            fresh.state.history.insert(0, record)
            del fresh.state.history[max(self.max_history, self.alert_after_failures) :]
            retry = status == "error" and self._retries.get(job.id, 0) < self.max_retries
            if status == "ok":
                self._retries.pop(job.id, None)
            if retry:
                # 失败重试：退避后再跑一次，不等下一个周期
                self._retries[job.id] = self._retries.get(job.id, 0) + 1
                fresh.state.next_run_at_ms = finished + int(self.retry_backoff_s * 1000)
                fresh.updated_at_ms = finished
                logger.info(
                    "定时任务将重试 id=%s 第 %d/%d 次 %ds 后",
                    job.id, self._retries[job.id], self.max_retries, int(self.retry_backoff_s),
                )
            elif fresh.delete_after_run:
                jobs.remove(fresh)
            else:
                self._retries.pop(job.id, None)
                try:
                    next_run = self._next_after_run(fresh.schedule, finished)
                except CronError as exc:
                    # 例如 croniter 被卸载：保留任务但停用，避免每轮空转
                    next_run = None
                    fresh.state.last_error = str(exc)
                fresh.state.next_run_at_ms = next_run
                if next_run is None:
                    fresh.enabled = False
                fresh.updated_at_ms = finished
            failures = 0
            for item in fresh.state.history:
                if item.status != "error":
                    break
                failures += 1
            self._save(jobs)
        self._write_run(job, record)
        n = self.alert_after_failures
        if status == "error" and n > 0 and failures >= n and failures % n == 0 and self.on_failure:
            try:
                await asyncio.to_thread(self.on_failure, job, failures, error)
            except Exception:  # noqa: BLE001 - 告警失败不影响调度
                logger.exception("定时任务失败告警发送失败 id=%s", job.id)
        logger.info(
            "定时任务完成 id=%s name=%s status=%s 耗时=%dms",
            job.id,
            job.name,
            status,
            finished - started,
        )
        return record

    # ------------------------------------------------------------- time helpers
    def _initial_next(self, schedule: CronSchedule, now: int) -> int | None:
        if schedule.kind == "at":
            return schedule.at_ms
        if schedule.kind == "every":
            if not schedule.every_ms or schedule.every_ms <= 0:
                raise CronError("every_seconds 必须大于 0")
            return now + schedule.every_ms
        if schedule.kind == "cron":
            if not schedule.expr:
                raise CronError("cron 表达式不能为空")
            return cron_next_ms(schedule.expr, schedule.tz, now)
        raise CronError(f"未知的调度类型: {schedule.kind}")

    def _next_after_run(self, schedule: CronSchedule, now: int) -> int | None:
        if schedule.kind == "at":
            return None
        return self._initial_next(schedule, now)


__all__ = [
    "CronCallback",
    "CronError",
    "CronService",
    "CronStoreError",
    "cron_next_ms",
    "get_service",
    "resolve_timezone",
    "set_service",
]
