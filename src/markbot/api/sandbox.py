"""扩展可用的受限子进程执行工具（属于稳定契约的一部分）。"""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path

# SYSTEMROOT/TEMP 在 Windows 上是解释器启动的必需项。
ENV_ALLOWLIST = {"PATH", "HOME", "LANG", "TZ", "TMPDIR", "SYSTEMROOT", "TEMP", "TMP", "USERPROFILE"}


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def combined(self, limit: int = 20_000) -> str:
        text = self.stdout
        if self.stderr:
            text = f"{text}\n[stderr]\n{self.stderr}" if text else self.stderr
        if self.timed_out:
            text += "\n[timeout]"
        return text[:limit]


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ}
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# 子进程放进独立进程组（POSIX）：超时或回合被取消时整组杀掉。只杀 shell 本身的话，
# ``cmd &`` 起的后台子进程会变成孤儿，而且它继续占着 stdout 管道，
# 随后的 ``communicate()`` 会一直等到它自己退出。
_POSIX = os.name == "posix"
_REAP_TIMEOUT_S = 5.0


def _spawn_kwargs() -> dict:
    return {"start_new_session": True} if _POSIX else {}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if _POSIX:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass  # 进程组已全部退出，或 pid 不是组长：退回只杀主进程
    # Windows 没有进程组语义，只能杀主进程；不可信代码应走容器档位
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def _communicate(proc: asyncio.subprocess.Process, timeout_s: float) -> ProcessResult:
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_REAP_TIMEOUT_S)
        except asyncio.TimeoutError:
            out, err = b"", b""
    except BaseException:
        # 外层取消（工具超时、回合中断、Ctrl-C）：必须连同进程组一起杀掉，否则留下孤儿进程
        _kill_tree(proc)
        raise
    return ProcessResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=(out or b"").decode("utf-8", errors="replace"),
        stderr=(err or b"").decode("utf-8", errors="replace"),
        timed_out=timed_out,
    )


async def run_sandboxed(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_s: float = 30.0,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=build_env(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_spawn_kwargs(),
    )
    return await _communicate(proc, timeout_s)


async def run_shell_sandboxed(
    command: str,
    *,
    cwd: Path,
    timeout_s: float = 30.0,
    env: dict[str, str] | None = None,
) -> ProcessResult:
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        env=build_env(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_spawn_kwargs(),
    )
    return await _communicate(proc, timeout_s)


__all__ = [
    "ENV_ALLOWLIST",
    "ProcessResult",
    "build_env",
    "run_sandboxed",
    "run_shell_sandboxed",
]
