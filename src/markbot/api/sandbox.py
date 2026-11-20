"""扩展可用的受限子进程执行工具（属于稳定契约的一部分）。"""

from __future__ import annotations

import asyncio
import os
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


async def _communicate(proc: asyncio.subprocess.Process, timeout_s: float) -> ProcessResult:
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        timed_out = False
    except asyncio.TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        timed_out = True
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
    )
    return await _communicate(proc, timeout_s)


__all__ = [
    "ENV_ALLOWLIST",
    "ProcessResult",
    "build_env",
    "run_sandboxed",
    "run_shell_sandboxed",
]
