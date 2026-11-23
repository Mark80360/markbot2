"""后台组件守护：线程崩溃自动重启、心跳与健康状态、systemd 通知（ADR 0010）。

``markbot serve`` 里有一批常驻线程（定时任务调度、记忆整理、空闲归档、热重载、日志清理）。
它们以前都是 ``daemon=True`` 的裸线程：循环体外的异常会让线程静默退出，没有人发现。

- :meth:`Supervisor.spawn`：在受监护的线程里运行 ``target``，抛异常或意外返回后按指数退避重启；
- :meth:`Supervisor.watch`：监控外部管理的线程（如定时任务调度器），不在时调用 ``restart``；
- :meth:`Supervisor.status`：供 ``/health`` 输出各组件 ``alive / restarts / last_error``；
- :func:`sd_notify`：在 systemd 下发送 ``READY=1`` 与 ``WATCHDOG=1``（``NOTIFY_SOCKET`` 不存在时什么都不做）。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("markbot.supervisor")

MAX_BACKOFF_S = 300.0
# 最近这段时间内重启过的组件视为 degraded
DEGRADED_WINDOW_S = 600.0


@dataclass
class _Component:
    name: str
    kind: str  # spawn | watch
    thread: threading.Thread | None = None
    restarts: int = 0
    last_error: str = ""
    last_error_at: float = 0.0
    last_restart_at: float = 0.0
    started_at: float = 0.0
    check: Callable[[], bool] | None = None
    restart: Callable[[], Any] | None = None


class Supervisor:
    def __init__(self, *, on_crash: Callable[[str, str], None] | None = None) -> None:
        self._components: dict[str, _Component] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._on_crash = on_crash
        self._monitor: threading.Thread | None = None

    # ------------------------------------------------------------ 受监护线程
    def spawn(
        self, name: str, target: Callable[..., Any], *args: Any, backoff_s: float = 5.0
    ) -> None:
        component = _Component(name=name, kind="spawn", started_at=time.time())

        def run() -> None:
            delay = max(0.1, backoff_s)
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    target(*args)
                    reason = "线程意外返回"
                except Exception as exc:  # noqa: BLE001 - 守护的意义就是兜住它
                    reason = f"{type(exc).__name__}: {exc}"
                    logger.exception("后台组件崩溃 component=%s", name)
                if self._stop.is_set():
                    return
                self._record_crash(component, reason)
                # 稳定运行过一段时间后重置退避
                if time.monotonic() - started > DEGRADED_WINDOW_S:
                    delay = max(0.1, backoff_s)
                if self._stop.wait(delay):
                    return
                delay = min(MAX_BACKOFF_S, delay * 2)

        component.thread = threading.Thread(target=run, daemon=True, name=f"sv-{name}")
        with self._lock:
            self._components[name] = component
        component.thread.start()

    def watch(
        self,
        name: str,
        is_alive: Callable[[], bool],
        restart: Callable[[], Any],
    ) -> None:
        """登记外部管理的组件：监控线程每轮检查 ``is_alive``，不在时调用 ``restart``。"""
        component = _Component(
            name=name, kind="watch", started_at=time.time(), check=is_alive, restart=restart
        )
        with self._lock:
            self._components[name] = component

    def start_monitor(self, interval_s: float = 15.0) -> None:
        if self._monitor is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_s):
                self.check_watched()
                sd_notify_watchdog(self.healthy())

        self._monitor = threading.Thread(target=loop, daemon=True, name="sv-monitor")
        self._monitor.start()

    def check_watched(self) -> None:
        with self._lock:
            watched = [c for c in self._components.values() if c.kind == "watch"]
        for component in watched:
            try:
                alive = bool(component.check and component.check())
            except Exception:  # noqa: BLE001
                alive = False
            if alive or self._stop.is_set():
                continue
            self._record_crash(component, "组件不在运行，已尝试重启")
            try:
                if component.restart is not None:
                    component.restart()
            except Exception as exc:  # noqa: BLE001
                logger.exception("组件重启失败 component=%s", component.name)
                component.last_error = f"重启失败: {type(exc).__name__}: {exc}"

    def _record_crash(self, component: _Component, reason: str) -> None:
        now = time.time()
        with self._lock:
            component.restarts += 1
            component.last_error = reason[:500]
            component.last_error_at = now
            component.last_restart_at = now
        logger.warning(
            "后台组件重启 component=%s restarts=%d reason=%s",
            component.name, component.restarts, reason[:200],
        )
        if self._on_crash is not None:
            try:
                self._on_crash(component.name, reason)
            except Exception:  # noqa: BLE001 - 告警失败不影响守护
                logger.exception("组件崩溃告警失败")

    # ------------------------------------------------------------ 状态
    def status(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        out: dict[str, dict[str, Any]] = {}
        with self._lock:
            components = list(self._components.values())
        for component in components:
            if component.kind == "watch":
                try:
                    alive = bool(component.check and component.check())
                except Exception:  # noqa: BLE001
                    alive = False
            else:
                alive = bool(component.thread and component.thread.is_alive())
            recent = component.last_restart_at and now - component.last_restart_at < DEGRADED_WINDOW_S
            out[component.name] = {
                "alive": alive,
                "restarts": component.restarts,
                "last_error": component.last_error,
                "last_error_at": component.last_error_at or None,
                "state": "ok" if alive and not recent else ("degraded" if alive else "down"),
            }
        return out

    def healthy(self) -> bool:
        return all(item["alive"] for item in self.status().values())

    def overall(self) -> str:
        states = {item["state"] for item in self.status().values()}
        if "down" in states:
            return "down"
        if "degraded" in states:
            return "degraded"
        return "ok"

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------- systemd
def sd_notify(message: str) -> bool:
    """向 systemd 发送状态通知；不在 systemd 下（无 NOTIFY_SOCKET）时返回 False。"""
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:  # type: ignore[attr-defined]
            sock.connect(address)
            sock.sendall(message.encode("utf-8"))
        return True
    except OSError as exc:
        logger.debug("sd_notify 失败: %s", exc)
        return False


def sd_notify_watchdog(healthy: bool) -> None:
    """只有全部组件都在时才喂狗：组件挂了且重启不了，让 systemd 重启整个服务。"""
    if healthy and os.environ.get("WATCHDOG_USEC"):
        sd_notify("WATCHDOG=1")


__all__ = ["Supervisor", "sd_notify", "sd_notify_watchdog"]
