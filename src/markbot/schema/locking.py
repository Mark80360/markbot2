"""学习层写锁：进程内可重入 + 跨进程互斥（POSIX flock / Windows msvcrt）。

``learned/`` 有多个写入方：优化器晋升/回滚、记忆整理（Dream）、记忆工具
（remember/forget）、``markbot memory restore``。它们可能在不同进程里同时运行
（``markbot optimize`` 与 ``markbot serve``），只靠进程内锁会互相覆盖。

约定：任何修改 ``learned/`` 的代码都必须包在 ``learned_write_lock(learned_dir)`` 里。
同一线程可以嵌套获取（整理器持锁时调用 MemoryStore.remember 不会死锁）。

锁文件放在 ``learned/`` 的上一级（``<home>/.learned.lock``），不污染学习层内容哈希。
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_GUARD = threading.Lock()
_RLOCKS: dict[str, threading.RLock] = {}
_LOCAL = threading.local()


def lock_path(learned_dir: Path) -> Path:
    learned = Path(learned_dir).expanduser().resolve()
    return learned.parent / f".{learned.name}.lock"


def _rlock(key: str) -> threading.RLock:
    with _GUARD:
        return _RLOCKS.setdefault(key, threading.RLock())


def _depths() -> dict[str, int]:
    depths = getattr(_LOCAL, "depths", None)
    if depths is None:
        depths = {}
        _LOCAL.depths = depths
    return depths


def _os_lock(fd: int, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    if os.name == "posix":
        import fcntl

        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("学习层写锁等待超时（另一个 markbot 进程正在写 learned/）")
                time.sleep(0.05)
    else:  # pragma: no cover - Windows
        import msvcrt

        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("学习层写锁等待超时（另一个 markbot 进程正在写 learned/）")
                time.sleep(0.05)


def _os_unlock(fd: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextmanager
def learned_write_lock(learned_dir: Path, *, timeout_s: float = 60.0) -> Iterator[None]:
    path = lock_path(learned_dir)
    key = str(path)
    rlock = _rlock(key)
    if not rlock.acquire(timeout=timeout_s):
        raise TimeoutError("学习层写锁等待超时（本进程内另一个线程正在写 learned/）")
    depths = _depths()
    outermost = depths.get(key, 0) == 0
    fd: int | None = None
    try:
        if outermost:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                _os_lock(fd, timeout_s)
            except BaseException:
                os.close(fd)
                fd = None
                raise
            _LOCAL.fds = {**getattr(_LOCAL, "fds", {}), key: fd}
        depths[key] = depths.get(key, 0) + 1
        try:
            yield
        finally:
            depths[key] -= 1
            if depths[key] <= 0:
                depths.pop(key, None)
                held = getattr(_LOCAL, "fds", {}).pop(key, None)
                if held is not None:
                    try:
                        _os_unlock(held)
                    finally:
                        os.close(held)
    finally:
        rlock.release()


__all__ = ["learned_write_lock", "lock_path"]
