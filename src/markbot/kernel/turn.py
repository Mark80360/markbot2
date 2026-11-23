"""回合上下文与并发闸门。

旧模型：Kernel 是有状态单例，HTTP 入口靠 ``set_ui`` / ``push_grants`` 临时改全局状态，
再用一把全局 ``run_lock`` 让所有请求串行——一个长任务会卡住所有聊天与定时任务。

新模型：

- :class:`TurnContext` 携带「只属于本回合」的东西（确认 UI、运行时授权），经
  ``contextvars`` 传到循环与权限判断。并发回合之间互不可见，回合结束自动失效，
  不再修改 Kernel / PermissionPolicy 上的共享字段。
- :class:`RunGate` 是读写闸门：

  * ``with gate.session(sid):`` —— 普通回合。同一会话串行（会话状态是单写者），
    不同会话并发，总并发数受 ``max_concurrent`` 限制；
  * ``with gate:`` / ``with gate.exclusive():`` —— 维护任务（运行时重载、记忆整理、
    空闲归档）。等待在途回合结束后独占执行，期间新回合排队。

  ``with gate:`` 等价于独占，因此旧代码里 ``with run_lock:`` 的写法语义不变（更保守）。

每个 HTTP 请求线程仍用自己的 ``asyncio.run``：单一常驻事件循环会让任何同步阻塞的工具
卡住所有会话，而 provider 每次请求新建 HTTP 客户端、MCP 连接由扩展私有的事件循环线程
持有，不受多事件循环影响。
"""

from __future__ import annotations

import contextvars
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TurnContext:
    ui: Any = None
    grants: frozenset[str] = field(default_factory=frozenset)
    # 本回合的工作区根目录（绑定了项目时；ADR 0013）。None = Kernel 默认工作区
    workspace: Any = None

    @classmethod
    def create(
        cls, *, ui: Any = None, grants: Iterable[str] = (), workspace: Any = None
    ) -> "TurnContext":
        names = frozenset(
            str(item).strip().lower() for item in (grants or ()) if str(item).strip()
        )
        return cls(ui=ui, grants=names, workspace=workspace)


_CURRENT: contextvars.ContextVar[TurnContext | None] = contextvars.ContextVar(
    "markbot_turn", default=None
)


def current_turn() -> TurnContext | None:
    return _CURRENT.get()


def turn_grants() -> frozenset[str]:
    turn = _CURRENT.get()
    return turn.grants if turn is not None else frozenset()


def turn_workspace() -> Any:
    turn = _CURRENT.get()
    return turn.workspace if turn is not None else None


def turn_ui(default: Any = None) -> Any:
    turn = _CURRENT.get()
    return turn.ui if turn is not None and turn.ui is not None else default


@contextmanager
def bind_turn(turn: TurnContext | None) -> Iterator[None]:
    if turn is None:
        yield
        return
    token = _CURRENT.set(turn)
    try:
        yield
    finally:
        _CURRENT.reset(token)


class RunGate:
    """读写闸门：会话级串行 + 会话间并发 + 维护任务独占。"""

    def __init__(self, max_concurrent: int = 4) -> None:
        self.max_concurrent = max(1, int(max_concurrent))
        self._cond = threading.Condition()
        self._active = 0
        self._exclusive = False
        self._waiting_exclusive = 0
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_refs: dict[str, int] = {}

    # ------------------------------------------------------------ 普通回合
    @contextmanager
    def session(self, session_id: str | None) -> Iterator[None]:
        key = session_id or ""
        lock: threading.Lock | None = None
        if key:
            with self._cond:
                lock = self._session_locks.setdefault(key, threading.Lock())
                self._session_refs[key] = self._session_refs.get(key, 0) + 1
            lock.acquire()
        try:
            with self._cond:
                # 维护任务优先：有独占者在等时新回合排队，避免维护任务饿死
                self._cond.wait_for(
                    lambda: not self._exclusive
                    and self._waiting_exclusive == 0
                    and self._active < self.max_concurrent
                )
                self._active += 1
            try:
                yield
            finally:
                with self._cond:
                    self._active -= 1
                    self._cond.notify_all()
        finally:
            if lock is not None:
                lock.release()
                with self._cond:
                    refs = self._session_refs.get(key, 1) - 1
                    if refs <= 0:
                        self._session_refs.pop(key, None)
                        self._session_locks.pop(key, None)
                    else:
                        self._session_refs[key] = refs

    # ------------------------------------------------------------ 维护任务
    @contextmanager
    def exclusive(self) -> Iterator[None]:
        self._enter_exclusive()
        try:
            yield
        finally:
            self._exit_exclusive()

    def _enter_exclusive(self) -> None:
        with self._cond:
            self._waiting_exclusive += 1
            try:
                self._cond.wait_for(lambda: not self._exclusive and self._active == 0)
            finally:
                self._waiting_exclusive -= 1
            self._exclusive = True

    def _exit_exclusive(self) -> None:
        with self._cond:
            self._exclusive = False
            self._cond.notify_all()

    # 兼容 ``with run_lock:``：等价于独占
    def __enter__(self) -> "RunGate":
        self._enter_exclusive()
        return self

    def __exit__(self, *exc: object) -> None:
        self._exit_exclusive()

    # 兼容 ``threading.Lock`` 的 acquire/release：独占语义；非阻塞时有在途回合即失败
    def acquire(self, blocking: bool = True) -> bool:
        if blocking:
            self._enter_exclusive()
            return True
        with self._cond:
            if self._exclusive or self._active > 0:
                return False
            self._exclusive = True
            return True

    def release(self) -> None:
        self._exit_exclusive()

    def stats(self) -> dict[str, int | bool]:
        with self._cond:
            return {
                "active": self._active,
                "max_concurrent": self.max_concurrent,
                "exclusive": self._exclusive,
                "waiting_exclusive": self._waiting_exclusive,
                "sessions": len(self._session_locks),
            }


@contextmanager
def session_scope(gate: Any, session_id: str | None) -> Iterator[None]:
    """``gate`` 是 :class:`RunGate` 时按会话加锁；普通锁（测试、旧调用方）退化为整体互斥。"""
    scoped = getattr(gate, "session", None)
    if callable(scoped):
        with scoped(session_id):
            yield
    else:
        with gate:
            yield


__all__ = [
    "RunGate",
    "TurnContext",
    "bind_turn",
    "current_turn",
    "session_scope",
    "turn_grants",
    "turn_ui",
    "turn_workspace",
]
