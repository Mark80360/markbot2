"""HTTP 应用层：`markbot serve` 的服务实现（与 CLI 参数解析分离）。

职责：
- ``POST /chat``（含 NDJSON 流式）、``POST /confirm``、``GET /health``；
- 异步任务 ``POST /tasks``、``GET /tasks/<id>[/events]``、``POST /tasks/<id>/cancel|detach``、
  会话控制 ``POST /sessions/<id>/cancel``、``GET /sessions/<id>/status``（ADR 0010）；
- 后台组件受 :class:`Supervisor` 守护，崩溃自动重启；SIGTERM 时优雅停机；
- 本机令牌鉴权、回调地址白名单、运行时授权清洗（ADR 0004）；
- 后台线程：运行时热重载、定时任务调度、记忆整理、空闲归档；
- 并发：``RunGate`` 让不同会话并发、同一会话串行、维护任务独占（ADR 0005）。

CLI（``markbot.cli.main.cmd_serve``）只负责解析参数、校验监听地址、构建 Kernel，
然后调用 :func:`run_server`。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from markbot.api.ui import NullUI
from markbot.cron.runner import build_failure_notifier, build_job_runner
from markbot.cron.service import CronService, set_service
from markbot.kernel import local_auth
from markbot.kernel.app import Kernel
from markbot.kernel.config import markbot_home
from markbot.kernel.session import new_session_id
from markbot.kernel.turn import RunGate, session_scope
from markbot.memory.consolidator import Consolidator
from markbot.server.supervisor import Supervisor, sd_notify
from markbot.server.tasks import TaskManager

_WATCH_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", ".ruff_cache"}
# 学习层里已按回合热刷新的部分：技能索引按目录指纹刷新，常驻记忆每轮重新渲染。
# 它们变化时不需要整份 reload（reload 会重新导入扩展、重连 MCP），否则每次写记忆都触发一次。
_HOT_LEARNED_DIRS = {"memory", "skills"}
_HOT_LEARNED_FILES = {"strategy/memory.md"}


def _runtime_fingerprint(kernel: Kernel) -> tuple[tuple[str, int, int], ...]:
    """收集扩展相关路径的 mtime/size 指纹，用于发现插件安装、删除与信任变更。"""
    roots = [
        kernel.home / "packages",
        kernel.home / "learned",
        kernel.home / "trust.json",
        kernel.project_root / "capabilities",
        kernel.project_root / ".markbot",
    ]
    entries: list[tuple[str, int, int]] = []
    for root in roots:
        if not root.exists():
            entries.append((str(root), -1, -1))
            continue
        if root.is_file():
            stat = root.stat()
            entries.append((str(root), stat.st_mtime_ns, stat.st_size))
            continue
        learned = root == kernel.home / "learned"
        for dirpath, dirnames, filenames in os.walk(root):
            at_learned_root = learned and Path(dirpath) == root
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in _WATCH_SKIP_DIRS
                and not (at_learned_root and d in _HOT_LEARNED_DIRS)
            )
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if learned and path.relative_to(root).as_posix() in _HOT_LEARNED_FILES:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append((str(path), stat.st_mtime_ns, stat.st_size))
    return tuple(entries)


def _watch_runtime(
    kernel: Kernel, run_lock: Any, interval_s: float = 2.0
) -> None:
    fingerprint = _runtime_fingerprint(kernel)
    while True:
        time.sleep(interval_s)
        current = _runtime_fingerprint(kernel)
        if current == fingerprint:
            continue
        time.sleep(0.5)  # 等待写入完成，避免读到半个文件
        fingerprint = _runtime_fingerprint(kernel)
        with run_lock:
            try:
                view = kernel.reload()
            except Exception as exc:  # noqa: BLE001
                print(f"[watch] 重新加载失败: {exc}", file=sys.stderr)
                continue
        print(
            f"[watch] 检测到扩展变更，已重新加载 tools={len(view.tools)} "
            f"skills={len(view.skills)}",
            file=sys.stderr,
        )
        for warning in kernel.warnings:
            print(f"[watch] warning: {warning}", file=sys.stderr)


_CONFIRM_LOCK = threading.Lock()
_PENDING_CONFIRMS: dict[str, dict[str, Any]] = {}


class _ChatConfirmUI(NullUI):
    """把工具的确认请求转发到聊天，并等待用户回复。

    服务端无法直接给用户发消息，因此把问题 POST 给渠道（``hook``），渠道负责
    发消息；用户在聊天里回复后，渠道回调 ``POST /confirm`` 唤醒这里。
    """

    def __init__(
        self,
        hook: str,
        chat_id: str,
        user_id: str = "",
        timeout_s: float = 180.0,
        *,
        token: str = "",
    ) -> None:
        self._hook = hook
        self._chat_id = chat_id
        self._user_id = user_id
        self._timeout_s = timeout_s
        self._token = token

    async def confirm(self, title: str, message: str) -> bool:
        token = uuid.uuid4().hex
        slot = {"event": threading.Event(), "approved": False}
        with _CONFIRM_LOCK:
            _PENDING_CONFIRMS[token] = slot
        try:
            body = json.dumps(
                {
                    "token": token,
                    "chat_id": self._chat_id,
                    "user_id": self._user_id,
                    "title": title,
                    "message": message,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if self._token:
                headers[local_auth.HEADER] = self._token
            request = urllib.request.Request(
                self._hook,
                data=body,
                headers=headers,
                method="POST",
            )
            urllib.request.urlopen(request, timeout=10).read()
            delivered = True
        except Exception as exc:  # noqa: BLE001 - 渠道不可达时按拒绝处理
            logging.getLogger("markbot.serve").warning("确认请求发送失败: %s", exc)
            delivered = False
        if not delivered:
            with _CONFIRM_LOCK:
                _PENDING_CONFIRMS.pop(token, None)
            return False
        try:
            await asyncio.to_thread(slot["event"].wait, self._timeout_s)
        finally:
            with _CONFIRM_LOCK:
                _PENDING_CONFIRMS.pop(token, None)
        return bool(slot["approved"] and slot["event"].is_set())


def _resolve_confirm(token: str, approved: bool) -> bool:
    with _CONFIRM_LOCK:
        slot = _PENDING_CONFIRMS.get(token)
    if slot is None:
        return False
    slot["approved"] = bool(approved)
    slot["event"].set()
    return True


def _memory_idle_loop(kernel: Kernel, run_lock: threading.Lock, idle_minutes: int) -> None:
    """定期整理记忆；模型调用不占锁，只有落盘/回滚阶段与聊天串行。"""
    logger = logging.getLogger("markbot.memory")
    interval = max(60, int(idle_minutes) * 60)
    while True:
        time.sleep(interval)
        try:
            report = asyncio.run(
                Consolidator(
                    kernel.home / "learned",
                    home=kernel.home,
                    # 记忆整理走 fast 角色模型（未配置时就是主模型，ADR 0011）
                    provider=kernel.role_provider("fast")
                    if hasattr(kernel, "role_provider")
                    else kernel.provider,
                    max_sessions=kernel.config.memory.max_sessions_per_run,
                    apply_lock=run_lock,
                ).run()
            )
            logger.info(
                "记忆整理 sessions=%d written=%d skipped=%d failed=%d cursor=%s",
                report.sessions,
                report.written,
                report.skipped,
                report.failed,
                report.cursor_advanced,
            )
        except Exception:  # noqa: BLE001 - 后台线程不能因单次失败退出
            logger.exception("记忆整理异常")


def _idle_archive_loop(
    kernel: Kernel, run_lock: threading.Lock, idle_minutes: int
) -> None:
    """长驻会话空闲后补写归档，让整理器能看到聊到一半的内容。

    每 60 秒检查一次；内核忙时直接跳过，下个周期再试。
    """
    logger = logging.getLogger("markbot.memory")
    while True:
        time.sleep(60)
        if not run_lock.acquire(blocking=False):
            continue
        try:
            sessions = asyncio.run(kernel.archive_idle(idle_minutes))
            if sessions:
                logger.info("空闲归档 sessions=%d", len(sessions))
        except Exception:  # noqa: BLE001 - 后台线程不能因单次失败退出
            logger.exception("空闲归档异常")
        finally:
            run_lock.release()


# 运行时授权只接受普通程序名；"*"、路径、带空白的值一律丢弃
_GRANT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")


def _sanitize_grants(raw: Any) -> tuple[list[str], list[str]]:
    """返回 (合法授权, 被丢弃的值)。"""
    if not isinstance(raw, list):
        return [], [] if raw in (None, "", []) else [str(raw)]
    kept: list[str] = []
    dropped: list[str] = []
    for item in raw:
        text = str(item).strip()
        if not text:
            continue
        (kept if _GRANT_NAME.match(text) else dropped).append(text)
    return kept, dropped


def _hook_url(hook: Any) -> tuple[str, str | None]:
    """取回调地址；非回环地址返回错误原因。"""
    if not isinstance(hook, dict):
        return "", None
    url = str(hook.get("url") or "").strip()
    # 渠道 webhook 模式可能绑定在本机局域网 IP 上：允许本机地址，外部地址一律拒绝
    if url and not local_auth.is_local_url(url):
        return "", f"callback url must be a local address: {url[:80]}"
    return url, None




def _log_retention_loop(kernel: Kernel, interval_s: float = 6 * 3600) -> None:
    """长驻进程每 6 小时按保留策略清理一次按日日志（启动时 Kernel 已清理过一次）。"""
    while True:
        time.sleep(interval_s)
        try:
            kernel.prune_logs()
        except Exception:  # noqa: BLE001 - 后台线程不能因单次失败退出
            logging.getLogger("markbot.retention").exception("日志清理异常")



# 优雅停机：停止接收请求后，最多等待在途回合这么久，再取消剩余回合
DRAIN_TIMEOUT_S = 60.0
# 同一组件的崩溃告警最短间隔
ALERT_INTERVAL_S = 600.0
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.~-]{1,160}$")
_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_CHANNEL = re.compile(r"^[a-z][a-z0-9_-]{0,19}$")


def parse_chat_request(data: Any) -> tuple[dict[str, Any] | None, str | None]:
    """解析 /chat 与 /tasks 共用的请求体；返回 ``(参数, 错误)``。"""
    if not isinstance(data, dict):
        return None, "invalid json"
    prompt = str(data.get("prompt") or "")
    session_id = str(data.get("session_id") or "") or None
    if session_id and not _SESSION_ID.match(session_id):
        return None, "invalid session_id"
    grants, dropped = _sanitize_grants(data.get("grants"))
    confirm_hook = data.get("confirm_hook") or {}
    notify_hook = data.get("notify_hook") or {}
    if not isinstance(confirm_hook, dict):
        confirm_hook = {}
    if not isinstance(notify_hook, dict):
        notify_hook = {}
    for hook in (confirm_hook, notify_hook):
        _, reason = _hook_url(hook)
        if reason:
            return None, reason
    if not prompt:
        return None, "prompt is required"
    # 只接受已登记项目的名称（不接受路径）：请求体不能把工作区指到任意目录（ADR 0013）
    project = str(data.get("project") or "").strip() or None
    if project and not _PROJECT.match(project):
        return None, "invalid project"
    # 渠道名只用于告诉模型「回复发到哪、能不能发图片」，不影响权限
    channel = str(data.get("channel") or "").strip().lower() or None
    if channel and not _CHANNEL.match(channel):
        channel = None
    return {
        "channel": channel,
        "project": project,
        "prompt": prompt,
        "session_id": session_id,
        "grants": grants,
        "dropped": dropped,
        "confirm_hook": confirm_hook,
        "notify_hook": notify_hook,
        "stream": bool(data.get("stream")),
    }, None


def execute_chat(
    kernel: Kernel,
    run_lock: Any,
    auth_token: str,
    prompt: str,
    session_id: str | None,
    grants: list[str],
    confirm_hook: dict[str, Any],
    notify_hook: dict[str, Any],
    *,
    on_delta: Any = None,
    on_event: Any = None,
    project: str | None = None,
    channel: str | None = None,
    should_run: Any = None,
):
    """执行一个聊天回合（/chat 与异步任务共用）。确认 UI 与授权只属于本回合。

    ``should_run``：拿到会话锁之后再问一次是否还要执行。排在同一会话后面的任务在等锁期间
    被 /stop 取消时，不能拿到锁之后照常跑（2026-11-23：/stop 后排队的消息又跑了 112 秒）。
    """
    with session_scope(run_lock, session_id):
        if should_run is not None and not should_run():
            from markbot.kernel.loop import TurnResult

            return TurnResult(
                stopped_reason="cancelled", error="任务在开始前被取消", session_id=session_id
            )
        turn_ui = None
        hook_url, _ = _hook_url(confirm_hook)
        if hook_url:
            turn_ui = _ChatConfirmUI(
                hook_url,
                str(confirm_hook.get("chat_id") or ""),
                str(confirm_hook.get("user_id") or ""),
                token=auth_token,
            )
        # 渠道把“主动发消息”的回调地址带进来，cron 工具据此决定结果投递到哪
        notify_url, _ = _hook_url(notify_hook)
        notify_chat = str(notify_hook.get("chat_id") or "")
        metadata: dict[str, Any] = {}
        if notify_url and notify_chat:
            metadata["notify"] = {"url": notify_url, "chat_id": notify_chat}
        if channel:
            metadata["channel"] = channel
        return asyncio.run(
            kernel.run(
                prompt,
                session_id=session_id,
                # 预先分配的新会话 id（异步任务）走 session_id 分支：找不到归档时按该 id 新建
                new_session=not session_id,
                # 多个聊天并存：开新会话时不能结束其他聊天的会话
                end_previous=False,
                metadata=metadata or None,
                on_delta=on_delta,
                on_event=on_event,
                ui=turn_ui,
                grants=grants,
                project=project,
            )
        )


class _Alerts:
    """后台组件崩溃等运维告警：发到最近一次对话的渠道（个人助手只有一个主人），按组件限频。"""

    def __init__(self, token: str, deliver: Any) -> None:
        self._token = token
        self._deliver = deliver
        self._target: dict[str, str] = {}
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.logger = logging.getLogger("markbot.alerts")

    def remember(self, notify_hook: dict[str, Any]) -> None:
        url, _ = _hook_url(notify_hook)
        chat_id = str(notify_hook.get("chat_id") or "")
        if url and chat_id:
            with self._lock:
                self._target = {"url": url, "chat_id": chat_id}

    def send(self, key: str, text: str) -> None:
        now = time.time()
        with self._lock:
            target = dict(self._target)
            if now - self._last.get(key, 0.0) < ALERT_INTERVAL_S:
                return
            self._last[key] = now
        self.logger.warning("告警 %s: %s", key, text[:300])
        if target:
            try:
                self._deliver(target, text)
            except Exception:  # noqa: BLE001 - 告警失败只记录
                self.logger.exception("告警发送失败")


def run_server(
    kernel: Kernel,
    *,
    host: str,
    port: int,
    log_file: Path | str,
    auth_token: str,
) -> int:
    """启动 HTTP 入口并阻塞到退出；返回进程退出码。"""
    logger = logging.getLogger("markbot.serve")
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    # 同一会话串行、不同会话并发；运行时重载/记忆整理/空闲归档以独占方式执行
    run_lock = RunGate(kernel.config.agent.max_concurrent_turns)

    def task_runner(record, args, on_delta, on_event):
        return execute_chat(
            kernel,
            run_lock,
            auth_token,
            args["prompt"],
            record.session_id,
            list(args.get("grants") or []),
            dict(args.get("confirm_hook") or {}),
            dict(args.get("notify_hook") or {}),
            on_delta=on_delta,
            on_event=on_event,
            project=args.get("project"),
            channel=args.get("channel"),
            should_run=lambda: not record.cancel_requested,
        )

    tasks = TaskManager(kernel.home, task_runner, token=auth_token, cancel=kernel.cancel)
    alerts = _Alerts(auth_token, tasks._post_notify)
    supervisor = Supervisor(
        on_crash=lambda name, reason: alerts.send(
            f"component:{name}", f"⚠️ MarkBot 后台组件「{name}」异常，已自动重启：{reason[:200]}"
        )
    )
    if hasattr(kernel.provider, "on_circuit_open"):
        kernel.provider.on_circuit_open = lambda label, count: alerts.send(
            f"circuit:{label}",
            f"⚠️ 模型 {label} 连续失败 {count} 次，已暂时切换到备用模型。",
        )
    supervisor.spawn("log-retention", _log_retention_loop, kernel)
    supervisor.spawn("runtime-watch", _watch_runtime, kernel, run_lock)

    cron: CronService | None = None
    if kernel.config.cron.enabled:
        cron_cfg = kernel.config.cron
        cron = CronService(
            kernel.home / "cron" / "jobs.json",
            on_job=build_job_runner(kernel, run_lock=run_lock),
            default_delivery=cron_cfg.default_delivery,
            default_timezone=cron_cfg.timezone or None,
            max_concurrent=cron_cfg.max_concurrent,
            job_timeout_s=cron_cfg.job_timeout_s,
            max_retries=cron_cfg.max_retries,
            retry_backoff_s=cron_cfg.retry_backoff_s,
            misfire_policy=cron_cfg.misfire_policy,
            misfire_grace_s=cron_cfg.misfire_grace_s,
            alert_after_failures=cron_cfg.alert_after_failures,
            on_failure=build_failure_notifier(kernel),
        )
        set_service(cron)
        cron.start_in_thread()
        supervisor.watch(
            "cron-scheduler",
            lambda: cron is not None and cron.is_running(),
            lambda: cron.start_in_thread() if cron is not None else None,
        )
        logger.info("定时任务调度器已启动 store=%s", cron.store_path)
    else:
        logger.info("定时任务调度器已关闭（[cron] enabled = false）")

    if (
        kernel.config.memory.enabled
        and kernel.config.memory.auto_consolidate
        and kernel.config.memory.idle_minutes > 0
    ):
        supervisor.spawn(
            "memory-idle", _memory_idle_loop, kernel, run_lock, kernel.config.memory.idle_minutes
        )
        logger.info(
            "记忆整理已启用 interval_minutes=%d", kernel.config.memory.idle_minutes
        )
    else:
        logger.info("记忆整理未启用（[memory] enabled/auto_consolidate/idle_minutes）")

    if kernel.config.memory.enabled and kernel.config.memory.idle_archive_minutes > 0:
        supervisor.spawn(
            "memory-idle-archive",
            _idle_archive_loop,
            kernel,
            run_lock,
            kernel.config.memory.idle_archive_minutes,
        )
        logger.info(
            "空闲归档已启用 idle_minutes=%d",
            kernel.config.memory.idle_archive_minutes,
        )
    else:
        logger.info("空闲归档未启用（[memory] enabled/idle_archive_minutes）")
    supervisor.start_monitor()

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self) -> tuple[list[str], dict[str, str]]:
            parsed = urllib.parse.urlsplit(self.path)
            parts = [urllib.parse.unquote(p) for p in parsed.path.split("/") if p]
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            return parts, query

        def _health(self) -> dict[str, Any]:
            jobs = 0
            if cron is not None:
                try:
                    jobs = len(cron.list_jobs())
                except Exception:  # noqa: BLE001 - 健康检查不能因坏文件失败
                    jobs = -1
            return {
                "status": supervisor.overall(),
                "tools": len(kernel.runtime.tools),
                "skills": len(kernel.runtime.skills),
                "cron": {
                    "enabled": cron is not None,
                    "jobs": jobs,
                    "running": cron.running_jobs() if cron is not None else [],
                },
                "turns": run_lock.stats(),
                "tasks": {"active": tasks.active_count()},
                "components": supervisor.status(),
                "models": kernel.provider.status() if hasattr(kernel.provider, "status") else [],
                "tokens_today": kernel.logs.metrics.tokens_today(),
            }

        def do_GET(self) -> None:  # noqa: N802
            parts, query = self._route()
            if parts == ["health"]:
                self._json(200, self._health())
                return
            if parts == ["metrics"]:
                # 只有计数与耗时，不含对话内容：与 /health 一样不需要令牌，便于 Prometheus 抓取
                self._metrics()
                return
            # 任务与会话信息含对话内容：与 POST 一样需要本机令牌
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            try:
                self._get(parts, query)
            except Exception:  # noqa: BLE001
                logging.getLogger("markbot.http").exception("处理请求失败 path=%s", self.path)
                try:
                    self._json(500, {"error": "internal error"})
                except OSError:
                    pass

        def _metrics(self) -> None:
            stats = run_lock.stats()
            components = supervisor.status()
            extra = {
                "markbot_turns_active": float(stats.get("active", 0)),
                "markbot_tasks_active": float(tasks.active_count()),
                "markbot_components_down": float(
                    sum(1 for item in components.values() if not item["alive"])
                ),
                "markbot_cron_running": float(len(cron.running_jobs()) if cron is not None else 0),
            }
            body = kernel.logs.metrics.prometheus(extra).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _get(self, parts: list[str], query: dict[str, str]) -> None:
            if parts == ["projects"]:
                self._json(200, {"projects": kernel.projects()})
            elif parts == ["tasks"]:
                items = tasks.list(
                    session_id=query.get("session_id") or None,
                    active=query.get("active") in {"1", "true", "yes"},
                    limit=_int(query.get("limit"), 20),
                )
                self._json(200, {"tasks": [item.as_dict() for item in items]})
            elif len(parts) == 2 and parts[0] == "tasks":
                record = tasks.get(parts[1])
                if record is None:
                    self._json(404, {"error": "task not found"})
                else:
                    self._json(200, record.as_dict())
            elif len(parts) == 3 and parts[0] == "tasks" and parts[2] == "events":
                payload = tasks.events(
                    parts[1],
                    after=_int(query.get("after"), 0),
                    wait_s=_float(query.get("wait"), 0.0),
                )
                if payload is None:
                    self._json(404, {"error": "task not found"})
                else:
                    self._json(200, payload)
            elif len(parts) == 3 and parts[0] == "sessions" and parts[2] == "status":
                info = kernel.turn_status(parts[1])
                info["tasks"] = [
                    item.as_dict() for item in tasks.list(session_id=parts[1], limit=5)
                ]
                self._json(200, info)
            else:
                self._json(404, {"error": "not found"})

        def _authorized(self) -> bool:
            return local_auth.verify(self.headers.get(local_auth.HEADER), auth_token)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                logger.warning("拒绝未鉴权请求 path=%s peer=%s", self.path, self.client_address[0])
                self._json(401, {"error": "unauthorized"})
                return
            parts, _ = self._route()
            try:
                if parts == ["confirm"]:
                    self._confirm()
                elif parts == ["chat"]:
                    self._chat()
                elif parts == ["tasks"]:
                    self._submit_task()
                elif len(parts) == 3 and parts[0] == "tasks" and parts[2] in {"cancel", "detach"}:
                    action = tasks.cancel if parts[2] == "cancel" else tasks.detach
                    record = action(parts[1])
                    if record is None:
                        self._json(404, {"error": "task not found"})
                    else:
                        self._json(200, record.as_dict())
                elif len(parts) == 3 and parts[0] == "sessions" and parts[2] == "cancel":
                    cancelled = tasks.cancel_session(parts[1])
                    direct = kernel.cancel(parts[1])
                    self._json(
                        200,
                        {"cancelled": bool(cancelled or direct), "tasks": cancelled},
                    )
                else:
                    self._json(404, {"error": "not found"})
            except Exception:  # noqa: BLE001 - 兜底，避免请求线程静默退出
                logging.getLogger("markbot.http").exception("处理请求失败 path=%s", self.path)
                try:
                    self._json(500, {"error": "internal error"})
                except OSError:
                    pass

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return None

        def _confirm(self) -> None:
            data = self._read_json()
            if not isinstance(data, dict):
                self._json(400, {"error": "invalid json"})
                return
            token = str(data.get("token") or "")
            ok = _resolve_confirm(token, bool(data.get("approved")))
            logger.info("/confirm token=%s approved=%s ok=%s", token[:8], data.get("approved"), ok)
            self._json(200 if ok else 404, {"ok": ok})

        def _parse(self) -> dict[str, Any] | None:
            request, error = parse_chat_request(self._read_json())
            if error:
                if error.startswith("callback url"):
                    logger.warning("拒绝回调地址: %s", error)
                self._json(400, {"error": error})
                return None
            assert request is not None
            if request["dropped"]:
                logger.warning(
                    "丢弃不合法的运行时授权: %s", ",".join(request["dropped"])[:200]
                )
            alerts.remember(request["notify_hook"])
            return request

        def _submit_task(self) -> None:
            request = self._parse()
            if request is None:
                return
            session_id = request["session_id"] or new_session_id()
            notify_url, _ = _hook_url(request["notify_hook"])
            notify = (
                {"url": notify_url, "chat_id": str(request["notify_hook"].get("chat_id") or "")}
                if notify_url and request["notify_hook"].get("chat_id")
                else {}
            )
            record = tasks.submit(
                prompt=request["prompt"],
                session_id=session_id,
                notify=notify,
                args={
                    "grants": request["grants"],
                    "confirm_hook": request["confirm_hook"],
                    "notify_hook": request["notify_hook"],
                    "project": request["project"],
                    "channel": request["channel"],
                },
            )
            logger.info(
                "/tasks 提交 task=%s session=%s prompt_chars=%d",
                record.task_id, session_id, len(request["prompt"]),
            )
            self._json(202, record.as_dict())

        def _chat(self) -> None:
            request = self._parse()
            if request is None:
                return
            logger.info(
                "/chat 请求 session=%s prompt_chars=%d grants=%s stream=%s",
                request["session_id"] or "-",
                len(request["prompt"]),
                ",".join(request["grants"]) or "-",
                request["stream"],
            )
            if request["stream"]:
                self._stream_chat(request)
                return
            result = self._run_chat(request)
            logger.info(
                "/chat 完成 session=%s reason=%s error=%s",
                result.session_id,
                result.stopped_reason,
                result.error,
            )
            self._json(
                200,
                {
                    "text": result.text,
                    "stopped_reason": result.stopped_reason,
                    "error": result.error,
                    "session_id": result.session_id,
                },
            )

        def _run_chat(self, request: dict[str, Any], on_delta=None):
            return execute_chat(
                kernel,
                run_lock,
                auth_token,
                request["prompt"],
                request["session_id"],
                request["grants"],
                request["confirm_hook"],
                request["notify_hook"],
                on_delta=on_delta,
                project=request["project"],
                channel=request["channel"],
            )

        def _stream_chat(self, request: dict[str, Any]) -> None:
            """NDJSON 流式返回：每行一个事件，最后一行是 done/error。"""
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            disconnected = False

            def emit(payload: dict[str, Any]) -> None:
                nonlocal disconnected
                if disconnected:
                    return
                try:
                    self.wfile.write(
                        (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
                    )
                    self.wfile.flush()
                except OSError:
                    disconnected = True

            def on_delta(text: str) -> None:
                emit({"type": "delta", "text": text})

            try:
                result = self._run_chat(request, on_delta=on_delta)
            except Exception as exc:  # noqa: BLE001 - 已发响应头，只能写错误事件
                logger.exception("/chat 流式处理失败")
                emit({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
                return
            logger.info(
                "/chat 流式完成 session=%s reason=%s error=%s",
                result.session_id,
                result.stopped_reason,
                result.error,
            )
            emit(
                {
                    "type": "done",
                    "text": result.text,
                    "stopped_reason": result.stopped_reason,
                    "error": result.error,
                    "session_id": result.session_id,
                }
            )

        def log_message(self, fmt: str, *args: Any) -> None:
            logging.getLogger("markbot.http").info(fmt % args)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(
        f"MarkBot2 HTTP 入口: http://{host}:{port}  "
        "(GET /health, POST /chat, POST /tasks, POST /confirm)"
    )
    print(f"运行日志: {log_file}")
    print(f"鉴权令牌: {local_auth.token_path(markbot_home())}（请求需带 {local_auth.HEADER} 头）")
    print(
        f"定时任务: {'已启用 ' + str(cron.store_path) if cron is not None else '已禁用'}"
    )

    stopping = threading.Event()

    def request_stop(*_args: Any) -> None:
        if stopping.is_set():
            return
        stopping.set()
        logger.info("收到停止信号，开始优雅停机")
        sd_notify("STOPPING=1")
        # serve_forever 在主线程里阻塞：shutdown() 必须从别的线程调用
        threading.Thread(target=server.shutdown, daemon=True, name="http-shutdown").start()

    for name in ("SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None and threading.current_thread() is threading.main_thread():
            try:
                signal.signal(sig, request_stop)
            except (OSError, ValueError):
                pass

    if tasks.recovered:
        # 渠道可能和服务一起刚启动：稍等再通知被中断的任务
        threading.Timer(10.0, tasks.notify_recovered).start()
    sd_notify("READY=1")
    exit_code = 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 - 记录后退出，避免无提示挂起
        logger.exception("HTTP 服务异常退出")
        exit_code = 1
    finally:
        supervisor.stop()
        if cron is not None:
            cron.stop()
            set_service(None)
        server.server_close()
        _drain(kernel, tasks, logger)
        asyncio.run(kernel.shutdown())
    return exit_code


def _drain(kernel: Kernel, tasks: TaskManager, logger: logging.Logger) -> None:
    """等待在途回合结束；超时后取消剩余回合（它们会补齐中断结果并归档）。"""
    deadline = time.monotonic() + DRAIN_TIMEOUT_S
    while kernel.running_sessions() and time.monotonic() < deadline:
        time.sleep(0.5)
    remaining = kernel.running_sessions()
    if remaining:
        logger.warning("停机等待超时，取消在途回合 sessions=%s", ",".join(remaining))
        for session_id in remaining:
            kernel.cancel(session_id)
        tasks.wait_idle(10.0)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "_ChatConfirmUI",
    "_hook_url",
    "_idle_archive_loop",
    "_memory_idle_loop",
    "_resolve_confirm",
    "_runtime_fingerprint",
    "_sanitize_grants",
    "_watch_runtime",
    "execute_chat",
    "parse_chat_request",
    "run_server",
]
