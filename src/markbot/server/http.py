"""HTTP 应用层：`markbot serve` 的服务实现（与 CLI 参数解析分离）。

职责：
- ``POST /chat``（含 NDJSON 流式）、``POST /confirm``、``GET /health``；
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
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from markbot.api.ui import NullUI
from markbot.cron.runner import build_job_runner
from markbot.cron.service import CronService, set_service
from markbot.kernel import local_auth
from markbot.kernel.app import Kernel
from markbot.kernel.config import markbot_home
from markbot.kernel.turn import RunGate, session_scope
from markbot.memory.consolidator import Consolidator

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
                    provider=kernel.provider,
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
    threading.Thread(
        target=_log_retention_loop, args=(kernel,), daemon=True, name="log-retention"
    ).start()
    threading.Thread(
        target=_watch_runtime, args=(kernel, run_lock), daemon=True, name="runtime-watch"
    ).start()

    cron: CronService | None = None
    if kernel.config.cron.enabled:
        cron = CronService(
            kernel.home / "cron" / "jobs.json",
            on_job=build_job_runner(kernel, run_lock=run_lock),
            default_delivery=kernel.config.cron.default_delivery,
            default_timezone=kernel.config.cron.timezone or None,
        )
        set_service(cron)
        cron.start_in_thread()
        logger.info("定时任务调度器已启动 store=%s", cron.store_path)
    else:
        logger.info("定时任务调度器已关闭（[cron] enabled = false）")

    if (
        kernel.config.memory.enabled
        and kernel.config.memory.auto_consolidate
        and kernel.config.memory.idle_minutes > 0
    ):
        threading.Thread(
            target=_memory_idle_loop,
            args=(kernel, run_lock, kernel.config.memory.idle_minutes),
            daemon=True,
            name="memory-idle",
        ).start()
        logger.info(
            "记忆整理已启用 interval_minutes=%d", kernel.config.memory.idle_minutes
        )
    else:
        logger.info("记忆整理未启用（[memory] enabled/auto_consolidate/idle_minutes）")

    if kernel.config.memory.enabled and kernel.config.memory.idle_archive_minutes > 0:
        threading.Thread(
            target=_idle_archive_loop,
            args=(kernel, run_lock, kernel.config.memory.idle_archive_minutes),
            daemon=True,
            name="memory-idle-archive",
        ).start()
        logger.info(
            "空闲归档已启用 idle_minutes=%d",
            kernel.config.memory.idle_archive_minutes,
        )
    else:
        logger.info("空闲归档未启用（[memory] enabled/idle_archive_minutes）")

    class Handler(BaseHTTPRequestHandler):
        def _json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/health":
                jobs = 0
                if cron is not None:
                    try:
                        jobs = len(cron.list_jobs())
                    except Exception:  # noqa: BLE001 - 健康检查不能因坏文件失败
                        jobs = -1
                self._json(
                    200,
                    {
                        "status": "ok",
                        "tools": len(kernel.runtime.tools),
                        "skills": len(kernel.runtime.skills),
                        "cron": {"enabled": cron is not None, "jobs": jobs},
                        "turns": run_lock.stats(),
                    },
                )
            else:
                self._json(404, {"error": "not found"})

        def _authorized(self) -> bool:
            return local_auth.verify(self.headers.get(local_auth.HEADER), auth_token)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                logger.warning("拒绝未鉴权请求 path=%s peer=%s", self.path, self.client_address[0])
                self._json(401, {"error": "unauthorized"})
                return
            try:
                if self.path == "/confirm":
                    self._confirm()
                else:
                    self._chat()
            except Exception:  # noqa: BLE001 - 兜底，避免请求线程静默退出
                logging.getLogger("markbot.http").exception("处理请求失败 path=%s", self.path)
                try:
                    self._json(500, {"error": "internal error"})
                except OSError:
                    pass

        def _confirm(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "invalid json"})
                return
            token = str(data.get("token") or "")
            ok = _resolve_confirm(token, bool(data.get("approved")))
            logger.info("/confirm token=%s approved=%s ok=%s", token[:8], data.get("approved"), ok)
            self._json(200 if ok else 404, {"ok": ok})

        def _chat(self) -> None:
            if self.path != "/chat":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                prompt = str(data.get("prompt") or "")
                session_id = str(data.get("session_id") or "") or None
                grants, dropped = _sanitize_grants(data.get("grants"))
                confirm_hook = data.get("confirm_hook") or {}
                notify_hook = data.get("notify_hook") or {}
                if not isinstance(confirm_hook, dict):
                    confirm_hook = {}
                if not isinstance(notify_hook, dict):
                    notify_hook = {}
                stream = bool(data.get("stream"))
            except (ValueError, AttributeError):
                self._json(400, {"error": "invalid json"})
                return
            if dropped:
                logger.warning("丢弃不合法的运行时授权: %s", ",".join(dropped)[:200])
            for hook in (confirm_hook, notify_hook):
                _, reason = _hook_url(hook)
                if reason:
                    logger.warning("拒绝回调地址: %s", reason)
                    self._json(400, {"error": reason})
                    return
            if not prompt:
                self._json(400, {"error": "prompt is required"})
                return
            logger.info(
                "/chat 请求 session=%s prompt_chars=%d grants=%s stream=%s",
                session_id or "-",
                len(prompt),
                ",".join(grants) or "-",
                stream,
            )
            if stream:
                self._stream_chat(prompt, session_id, grants, confirm_hook, notify_hook)
                return
            result = self._run_chat(prompt, session_id, grants, confirm_hook, notify_hook)
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

        def _run_chat(self, prompt, session_id, grants, confirm_hook, notify_hook, on_delta=None):
            with session_scope(run_lock, session_id):
                # 确认 UI 与授权只属于本回合（TurnContext），不修改 Kernel 的共享状态
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
                metadata = (
                    {"notify": {"url": notify_url, "chat_id": notify_chat}}
                    if notify_url and notify_chat
                    else None
                )
                return asyncio.run(
                    kernel.run(
                        prompt,
                        session_id=session_id,
                        new_session=not session_id,
                        # 多个聊天并存：开新会话时不能结束其他聊天的会话
                        end_previous=False,
                        metadata=metadata,
                        on_delta=on_delta,
                        ui=turn_ui,
                        grants=grants,
                    )
                )

        def _stream_chat(self, prompt, session_id, grants, confirm_hook, notify_hook) -> None:
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
                result = self._run_chat(
                    prompt,
                    session_id,
                    grants,
                    confirm_hook,
                    notify_hook,
                    on_delta=on_delta,
                )
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
    print(f"MarkBot2 HTTP 入口: http://{host}:{port}  (GET /health, POST /chat, POST /confirm)")
    print(f"运行日志: {log_file}")
    print(f"鉴权令牌: {local_auth.token_path(markbot_home())}（请求需带 {local_auth.HEADER} 头）")
    print(
        f"定时任务: {'已启用 ' + str(cron.store_path) if cron is not None else '已禁用'}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    except Exception:  # noqa: BLE001 - 记录后退出，避免无提示挂起
        logger.exception("HTTP 服务异常退出")
        return 1
    finally:
        if cron is not None:
            cron.stop()
            set_service(None)
        server.server_close()
        asyncio.run(kernel.shutdown())
    return 0


__all__ = [
    "_ChatConfirmUI",
    "_hook_url",
    "_idle_archive_loop",
    "_memory_idle_loop",
    "_resolve_confirm",
    "_runtime_fingerprint",
    "_sanitize_grants",
    "_watch_runtime",
    "run_server",
]
