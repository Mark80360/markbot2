"""定时任务数据模型。

参考 nanobot 的 ``CronJob`` / ``CronSchedule``，去掉渠道绑定、WebUI 元数据与
多进程合并逻辑：markbot 的调度只在本机一个进程里跑，到点后复用 ``Kernel``
执行一个普通回合。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

ScheduleKind = Literal["at", "every", "cron"]
Delivery = Literal["none", "channel"]


def now_ms() -> int:
    return int(time.time() * 1000)


def new_job_id() -> str:
    return uuid.uuid4().hex[:8]


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def ms_to_iso(value: int | None) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value / 1000).astimezone().isoformat(timespec="seconds")


def describe_schedule(schedule: "CronSchedule") -> str:
    if schedule.kind == "at":
        return f"一次性 @ {ms_to_iso(schedule.at_ms)}"
    if schedule.kind == "every":
        seconds = int((schedule.every_ms or 0) / 1000)
        return f"每 {seconds} 秒"
    tz = f" ({schedule.tz})" if schedule.tz else ""
    return f"cron {schedule.expr}{tz}"


@dataclass
class CronSchedule:
    kind: ScheduleKind
    at_ms: int | None = None
    every_ms: int | None = None
    expr: str | None = None
    tz: str | None = None

    def describe(self) -> str:
        return describe_schedule(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at_ms": self.at_ms,
            "every_ms": self.every_ms,
            "expr": self.expr,
            "tz": self.tz,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CronSchedule":
        kind = str(data.get("kind") or "every")
        if kind not in ("at", "every", "cron"):
            kind = "every"
        return cls(
            kind=kind,  # type: ignore[arg-type]
            at_ms=_opt_int(data.get("at_ms")),
            every_ms=_opt_int(data.get("every_ms")),
            expr=_opt_str(data.get("expr")),
            tz=_opt_str(data.get("tz")),
        )


@dataclass
class CronPayload:
    """任务到点后要做什么：以一条消息驱动一个 Agent 回合。"""

    kind: str = "agent_turn"
    message: str = ""
    # 任务绑定的会话；为空时使用 ``cron_<job_id>`` 独立会话
    session_id: str | None = None
    # none: 只归档 + 日志；channel: 通过渠道回调把最终回复发回聊天
    delivery: Delivery = "none"
    # 渠道投递需要的信息，由创建任务时的 ``session.metadata["notify"]`` 带过来
    notify: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "session_id": self.session_id,
            "delivery": self.delivery,
            "notify": dict(self.notify),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CronPayload":
        delivery = str(data.get("delivery") or "none")
        if delivery not in ("none", "channel"):
            delivery = "none"
        notify = data.get("notify")
        return cls(
            kind=str(data.get("kind") or "agent_turn"),
            message=str(data.get("message") or ""),
            session_id=_opt_str(data.get("session_id")),
            delivery=delivery,  # type: ignore[arg-type]
            notify={str(k): str(v) for k, v in (notify or {}).items()},
        )


@dataclass
class CronRun:
    run_id: str
    started_at_ms: int
    finished_at_ms: int
    status: str = "ok"  # ok | error | skipped
    error: str = ""
    response: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at_ms": self.started_at_ms,
            "finished_at_ms": self.finished_at_ms,
            "status": self.status,
            "error": self.error,
            "response": self.response,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CronRun":
        return cls(
            run_id=str(data.get("run_id") or new_run_id()),
            started_at_ms=int(data.get("started_at_ms") or 0),
            finished_at_ms=int(data.get("finished_at_ms") or 0),
            status=str(data.get("status") or "ok"),
            error=str(data.get("error") or ""),
            response=str(data.get("response") or ""),
        )


@dataclass
class CronJobState:
    next_run_at_ms: int | None = None
    last_run_at_ms: int | None = None
    last_status: str = ""
    last_error: str = ""
    history: list[CronRun] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "next_run_at_ms": self.next_run_at_ms,
            "last_run_at_ms": self.last_run_at_ms,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "history": [run.to_dict() for run in self.history],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CronJobState":
        history = [
            CronRun.from_dict(item)
            for item in (data.get("history") or [])
            if isinstance(item, dict)
        ]
        return cls(
            next_run_at_ms=_opt_int(data.get("next_run_at_ms")),
            last_run_at_ms=_opt_int(data.get("last_run_at_ms")),
            last_status=str(data.get("last_status") or ""),
            last_error=str(data.get("last_error") or ""),
            history=history,
        )


@dataclass
class CronJob:
    id: str
    name: str
    enabled: bool
    schedule: CronSchedule
    payload: CronPayload
    state: CronJobState = field(default_factory=CronJobState)
    created_at_ms: int = field(default_factory=now_ms)
    updated_at_ms: int = field(default_factory=now_ms)
    delete_after_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": self.enabled,
            "schedule": self.schedule.to_dict(),
            "payload": self.payload.to_dict(),
            "state": self.state.to_dict(),
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "delete_after_run": self.delete_after_run,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CronJob":
        return cls(
            id=str(data.get("id") or new_job_id()),
            name=str(data.get("name") or data.get("id") or "未命名任务"),
            enabled=bool(data.get("enabled", True)),
            schedule=CronSchedule.from_dict(data.get("schedule") or {}),
            payload=CronPayload.from_dict(data.get("payload") or {}),
            state=CronJobState.from_dict(data.get("state") or {}),
            created_at_ms=int(data.get("created_at_ms") or now_ms()),
            updated_at_ms=int(data.get("updated_at_ms") or now_ms()),
            delete_after_run=bool(data.get("delete_after_run", False)),
        )


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "CronJob",
    "CronJobState",
    "CronPayload",
    "CronRun",
    "CronSchedule",
    "Delivery",
    "ScheduleKind",
    "describe_schedule",
    "ms_to_iso",
    "new_job_id",
    "new_run_id",
    "now_ms",
]
