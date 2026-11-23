"""异步任务：提交后立即返回 ``task_id``，执行与 HTTP 连接解耦（ADR 0010）。

旧模型下渠道用一次同步 ``POST /chat`` 等到回合结束（超时 600s）。长任务超时后渠道放弃，
服务端仍在执行，结果回写时 ``BrokenPipe``，用户既拿不到结果也看不到进度。

新模型：

- ``submit`` 登记任务并在独立线程里执行（仍经 ``RunGate`` 限流、同会话串行）；
- 进度（流式文本、工具调用、状态）进入内存事件缓冲，客户端按序号长轮询续读，
  断线重连不丢不重；
- 任务元数据持久化到 ``<home>/tasks/<task_id>.json``（只有状态与结果摘要，会话事实仍以
  ``sessions/<id>/events.jsonl`` 为准，不重复落盘）；
- 结束时如果客户端已「放手」（detach），或 30 秒内没有任何轮询（渠道进程重启了），
  就经 ``notify_hook`` 把结果主动推回聊天；
- 进程重启后，未结束的任务标记为 ``interrupted`` 并通知原聊天。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from markbot.api.redaction import redact_secrets
from markbot.kernel import local_auth

logger = logging.getLogger("markbot.tasks")

ACTIVE_STATES = frozenset({"queued", "running"})
FINAL_STATES = frozenset({"completed", "failed", "cancelled", "interrupted"})
# 结束时距最近一次轮询超过这个时间，视为没有客户端在等，改为主动推送
UNCLAIMED_AFTER_S = 30.0
# 结束后事件缓冲在内存里保留的时间；之后只能查元数据
KEEP_EVENTS_S = 3600.0
MAX_EVENTS = 20_000
MAX_WAIT_S = 30.0
RETENTION_DAYS = 7
PREVIEW_CHARS = 60


def _now() -> float:
    return time.time()


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


@dataclass
class TaskRecord:
    task_id: str
    session_id: str
    prompt_preview: str
    created_at: float
    state: str = "queued"
    started_at: float | None = None
    finished_at: float | None = None
    text: str = ""
    error: str | None = None
    stopped_reason: str = ""
    notify: dict[str, str] = field(default_factory=dict)
    detached: bool = False
    delivered: bool = False
    cancel_requested: bool = False
    tool_calls: int = 0
    current_tool: str | None = None

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["elapsed_s"] = round(
            (self.finished_at or _now()) - (self.started_at or self.created_at), 1
        )
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskRecord:
        fields = cls.__dataclass_fields__
        return cls(**{key: value for key, value in data.items() if key in fields})


TaskRunner = Callable[[TaskRecord, dict[str, Any], Callable[[str], None], Callable[[Any], None]], Any]


class TaskManager:
    """任务登记、执行、事件缓冲、持久化与结果推送。"""

    def __init__(
        self,
        home: Path,
        runner: TaskRunner,
        *,
        token: str = "",
        cancel: Callable[[str], bool] | None = None,
        deliver: Callable[[dict[str, str], str], bool] | None = None,
    ) -> None:
        self.dir = Path(home) / "tasks"
        self._runner = runner
        self._token = token
        self._cancel_session = cancel
        self._deliver = deliver or self._post_notify
        self._cond = threading.Condition()
        self._records: dict[str, TaskRecord] = {}
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._args: dict[str, dict[str, Any]] = {}
        self._last_poll: dict[str, float] = {}
        self._threads: dict[str, threading.Thread] = {}
        self.recovered: list[TaskRecord] = []
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("任务目录不可写，任务状态不会持久化: %s", exc)
        self._recover()

    # ------------------------------------------------------------------ 提交
    def submit(
        self,
        *,
        prompt: str,
        session_id: str,
        notify: dict[str, str] | None = None,
        args: dict[str, Any] | None = None,
    ) -> TaskRecord:
        record = TaskRecord(
            task_id="task_" + uuid.uuid4().hex[:12],
            session_id=session_id,
            prompt_preview=_preview(redact_secrets(prompt)),
            created_at=_now(),
            notify={k: str(v) for k, v in (notify or {}).items() if v},
        )
        with self._cond:
            self._gc_locked()
            self._records[record.task_id] = record
            self._events[record.task_id] = []
            self._args[record.task_id] = {"prompt": prompt, **(args or {})}
            self._last_poll[record.task_id] = _now()
        self._persist(record)
        self._emit(record.task_id, {"type": "status", "state": "queued"})
        thread = threading.Thread(
            target=self._run, args=(record.task_id,), daemon=True, name=f"task-{record.task_id}"
        )
        with self._cond:
            self._threads[record.task_id] = thread
        thread.start()
        logger.info("任务已提交 task=%s session=%s", record.task_id, session_id)
        return record

    def _run(self, task_id: str) -> None:
        with self._cond:
            record = self._records[task_id]
            args = self._args.get(task_id, {})
            cancelled_early = record.cancel_requested
            if not cancelled_early:
                record.state = "running"
                record.started_at = _now()
        if cancelled_early:
            self._finish(task_id, state="cancelled", text="", error="任务在开始前被取消",
                         stopped_reason="cancelled")
            return
        self._persist(record)
        self._emit(task_id, {"type": "status", "state": "running"})

        def on_delta(text: str) -> None:
            if text:
                self._emit(task_id, {"type": "delta", "text": text})

        def on_event(event: Any) -> None:
            kind = getattr(event, "type", "")
            data = getattr(event, "data", None) or {}
            if kind == "assistant_message":
                names = [
                    str(call.get("name"))
                    for call in (data.get("tool_calls") or [])
                    if isinstance(call, dict) and call.get("name")
                ]
                if names:
                    with self._cond:
                        record.tool_calls += len(names)
                        record.current_tool = names[-1]
                    self._emit(task_id, {"type": "tool", "names": names})
            elif kind == "tool_result":
                with self._cond:
                    record.current_tool = None

        try:
            result = self._runner(record, args, on_delta, on_event)
        except Exception as exc:  # noqa: BLE001 - 任务线程不能静默死掉
            logger.exception("任务执行异常 task=%s", task_id)
            self._finish(task_id, state="failed", text="", error=f"{type(exc).__name__}: {exc}",
                         stopped_reason="error")
            return
        reason = str(getattr(result, "stopped_reason", "") or "completed")
        state = {"completed": "completed", "cancelled": "cancelled"}.get(reason, "failed")
        self._finish(
            task_id,
            state=state,
            text=str(getattr(result, "text", "") or ""),
            error=getattr(result, "error", None),
            stopped_reason=reason,
            session_id=getattr(result, "session_id", None),
        )

    def _finish(
        self,
        task_id: str,
        *,
        state: str,
        text: str,
        error: str | None,
        stopped_reason: str,
        session_id: str | None = None,
    ) -> None:
        with self._cond:
            record = self._records[task_id]
            record.state = state
            record.text = redact_secrets(text)
            record.error = redact_secrets(error) if error else None
            record.stopped_reason = stopped_reason
            record.finished_at = _now()
            record.current_tool = None
            if session_id:
                record.session_id = session_id
            self._args.pop(task_id, None)
            self._threads.pop(task_id, None)
            claimed = _now() - self._last_poll.get(task_id, 0.0) < UNCLAIMED_AFTER_S
            push = bool(record.notify) and (record.detached or not claimed)
        self._persist(record)
        self._emit(
            task_id,
            {
                "type": "done",
                "state": state,
                "text": record.text,
                "error": record.error,
                "stopped_reason": stopped_reason,
                "session_id": record.session_id,
            },
        )
        logger.info(
            "任务结束 task=%s state=%s reason=%s push=%s", task_id, state, stopped_reason, push
        )
        if push:
            self._push_result(record)

    # ------------------------------------------------------------------ 事件
    def _emit(self, task_id: str, event: dict[str, Any]) -> None:
        with self._cond:
            events = self._events.get(task_id)
            if events is None:
                return
            if len(events) >= MAX_EVENTS and event.get("type") == "delta":
                # 极端长输出：后续增量不再缓存，最终结果仍在 done 事件里
                return
            event = {"seq": len(events) + 1, "ts": round(_now(), 3), **event}
            events.append(event)
            self._cond.notify_all()

    def events(
        self, task_id: str, *, after: int = 0, wait_s: float = 0.0
    ) -> dict[str, Any] | None:
        """返回 ``after`` 之后的事件；没有新事件时最多等待 ``wait_s`` 秒（长轮询）。"""
        deadline = _now() + max(0.0, min(float(wait_s or 0), MAX_WAIT_S))
        with self._cond:
            record = self._records.get(task_id)
            if record is None:
                return None
            self._last_poll[task_id] = _now()
            while True:
                events = self._events.get(task_id)
                if events is None:
                    # 事件缓冲已回收（或重启后的旧任务）：用元数据合成一个 done 事件
                    done = record.state in FINAL_STATES
                    synthetic = (
                        [{"seq": after + 1, "type": "done", "state": record.state,
                          "text": record.text, "error": record.error,
                          "stopped_reason": record.stopped_reason,
                          "session_id": record.session_id}]
                        if done
                        else []
                    )
                    return {"task": record.as_dict(), "events": synthetic, "next": after + len(synthetic)}
                fresh = events[after:] if after >= 0 else events
                remaining = deadline - _now()
                if fresh or record.state in FINAL_STATES or remaining <= 0:
                    self._last_poll[task_id] = _now()
                    return {
                        "task": record.as_dict(),
                        "events": list(fresh),
                        "next": len(events),
                    }
                self._cond.wait(timeout=remaining)

    # ------------------------------------------------------------------ 控制
    def get(self, task_id: str) -> TaskRecord | None:
        with self._cond:
            return self._records.get(task_id)

    def list(
        self, *, session_id: str | None = None, active: bool = False, limit: int = 20
    ) -> list[TaskRecord]:
        with self._cond:
            items = list(self._records.values())
        if session_id:
            items = [item for item in items if item.session_id == session_id]
        if active:
            items = [item for item in items if item.state in ACTIVE_STATES]
        items.sort(key=lambda item: item.created_at, reverse=True)
        return items[: max(1, int(limit))]

    def cancel(self, task_id: str) -> TaskRecord | None:
        with self._cond:
            record = self._records.get(task_id)
            if record is None:
                return None
            if record.state in FINAL_STATES:
                return record
            record.cancel_requested = True
            running = record.state == "running"
            session_id = record.session_id
        if running and self._cancel_session is not None:
            self._cancel_session(session_id)
        self._emit(task_id, {"type": "status", "state": "cancelling"})
        return record

    def cancel_session(self, session_id: str) -> list[str]:
        """取消会话的全部未结束任务；返回被取消的 task_id。"""
        ids = [item.task_id for item in self.list(session_id=session_id, active=True, limit=100)]
        for task_id in ids:
            self.cancel(task_id)
        return ids

    def detach(self, task_id: str) -> TaskRecord | None:
        """客户端不再等待：任务结束后改为主动推送结果。已结束的任务直接返回结果。"""
        with self._cond:
            record = self._records.get(task_id)
            if record is None:
                return None
            if record.state in FINAL_STATES:
                record.delivered = True  # 客户端拿到结果自行回复，不再推送
            else:
                record.detached = True
        self._persist(record)
        return record

    def active_count(self) -> int:
        with self._cond:
            return sum(1 for item in self._records.values() if item.state in ACTIVE_STATES)

    def wait_idle(self, timeout_s: float) -> bool:
        """等待所有任务线程结束（优雅停机）；超时返回 False。"""
        deadline = _now() + max(0.0, timeout_s)
        while _now() < deadline:
            with self._cond:
                threads = list(self._threads.values())
            if not threads:
                return True
            threads[0].join(timeout=max(0.05, min(1.0, deadline - _now())))
        with self._cond:
            return not self._threads

    # ------------------------------------------------------------------ 推送
    def _push_result(self, record: TaskRecord) -> None:
        if record.delivered or not record.notify:
            return
        if record.state == "completed":
            body = record.text or "（任务完成，但没有返回内容）"
            text = f"✅ 后台任务完成：{record.prompt_preview}\n\n{body}"
        elif record.state == "cancelled":
            text = f"⏹ 任务已取消：{record.prompt_preview}" + (f"\n\n{record.text}" if record.text else "")
        else:
            text = (
                f"❌ 任务未完成：{record.prompt_preview}\n原因：{record.error or record.stopped_reason}"
                + (f"\n\n{record.text}" if record.text else "")
                + "\n\n回复 /retry 可以重新执行。"
            )
        ok = False
        try:
            ok = bool(self._deliver(record.notify, text))
        except Exception as exc:  # noqa: BLE001 - 推送失败只记录
            logger.warning("任务结果推送失败 task=%s: %s", record.task_id, exc)
        with self._cond:
            record.delivered = ok
        self._persist(record)

    def _post_notify(self, notify: dict[str, str], text: str) -> bool:
        url = str(notify.get("url") or "")
        chat_id = str(notify.get("chat_id") or "")
        if not url or not chat_id or not local_auth.is_local_url(url):
            return False
        body = json.dumps({"chat_id": chat_id, "text": text}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers[local_auth.HEADER] = self._token
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        last: Exception | None = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    response.read()
                return True
            except Exception as exc:  # noqa: BLE001 - 渠道可能正在重启，稍后重试
                last = exc
                time.sleep(2 * (attempt + 1))
        logger.warning("任务结果推送失败 chat=%s: %s", chat_id, last)
        return False

    # ------------------------------------------------------------------ 持久化
    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def _persist(self, record: TaskRecord) -> None:
        with self._cond:
            data = asdict(record)
        path = self._path(record.task_id)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("任务状态写入失败 task=%s: %s", record.task_id, exc)
            try:
                tmp.unlink()
            except OSError:
                pass

    def _recover(self) -> None:
        """启动时加载近期任务；上次进程退出时未结束的任务标记为 interrupted 并通知。"""
        if not self.dir.exists():
            return
        cutoff = _now() - RETENTION_DAYS * 86400
        loaded: list[TaskRecord] = []
        for path in sorted(self.dir.glob("task_*.json")):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    continue
                record = TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("任务记录不可读，已跳过 %s: %s", path.name, exc)
                continue
            loaded.append(record)
        loaded.sort(key=lambda item: item.created_at)
        for record in loaded[-200:]:
            self._records[record.task_id] = record
            if record.state in ACTIVE_STATES:
                record.state = "interrupted"
                record.error = "服务重启，任务被中断"
                record.stopped_reason = "interrupted"
                record.finished_at = _now()
                self._persist(record)
                self.recovered.append(record)

    def notify_recovered(self) -> int:
        """把重启中断的任务告知原聊天（在服务就绪后调用，渠道可能也刚启动）。"""
        sent = 0
        for record in self.recovered:
            if not record.notify or record.delivered:
                continue
            text = (
                f"⚠️ 服务重启，任务被中断：{record.prompt_preview}\n"
                "已完成的部分保留在会话里；回复 /retry 重新执行，或直接继续对话。"
            )
            try:
                record.delivered = bool(self._deliver(record.notify, text))
            except Exception as exc:  # noqa: BLE001
                logger.warning("中断通知失败 task=%s: %s", record.task_id, exc)
            self._persist(record)
            sent += int(record.delivered)
        self.recovered = []
        return sent

    def _gc_locked(self) -> None:
        """回收已结束较久的任务的事件缓冲与内存记录（持久化文件保留到 RETENTION_DAYS）。"""
        now = _now()
        for task_id, record in list(self._records.items()):
            if record.state not in FINAL_STATES or not record.finished_at:
                continue
            age = now - record.finished_at
            if age > KEEP_EVENTS_S:
                self._events.pop(task_id, None)
                self._last_poll.pop(task_id, None)
            if age > RETENTION_DAYS * 86400:
                self._records.pop(task_id, None)


__all__ = ["ACTIVE_STATES", "FINAL_STATES", "TaskManager", "TaskRecord"]
