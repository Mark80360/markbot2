"""扩展可用的受限子进程执行工具（属于稳定契约的一部分）。"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# 子进程环境白名单。SYSTEMROOT/TEMP 等在 Windows 上是解释器启动的必需项；
# APPDATA/LOCALAPPDATA/PATHEXT/COMSPEC 缺失时 pip、git、npm 等工具会行为异常；
# 代理变量缺失时子进程访问外网失败（ADR 0009）。
_BASE_ENV = {"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "TERM"}
_WINDOWS_ENV = {
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE", "USERNAME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "COMMONPROGRAMFILES", "PATHEXT", "COMSPEC", "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
}
PROXY_ENV = {
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
}
ENV_ALLOWLIST = frozenset(_BASE_ENV | _WINDOWS_ENV | PROXY_ENV)

_WINDOWS = os.name == "nt"


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


def build_env(
    extra: dict[str, str] | None = None, *, passthrough: Iterable[str] = ()
) -> dict[str, str]:
    """白名单环境 + ``passthrough``（额外透传的变量名）+ ``extra``（如注入的密钥）。

    Windows 环境变量名不区分大小写，按大写比较。
    """
    wanted = set(ENV_ALLOWLIST) | {str(name) for name in passthrough if str(name).strip()}
    if _WINDOWS:
        upper = {name.upper() for name in wanted}
        env = {key: value for key, value in os.environ.items() if key.upper() in upper}
    else:
        env = {key: os.environ[key] for key in wanted if key in os.environ}
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# ---------------------------------------------------------------------- shell
SHELL_CHOICES = frozenset({"auto", "bash", "sh", "pwsh", "powershell", "cmd"})


def resolve_shell(program: str | None = "auto") -> list[str]:
    """把 ``[shell] program`` 解析成执行命令用的 argv 前缀（命令追加在末尾）。

    - ``auto``：POSIX 用 bash（没有时回退 /bin/sh）；Windows 用 pwsh（没有时回退
      Windows PowerShell，再回退 cmd）。
    - 显式指定但找不到时同样按上面的顺序回退，不让命令完全无法执行。
    """
    choice = (program or "auto").strip().lower()
    if choice not in SHELL_CHOICES:
        # 允许直接写可执行文件路径，例如 "C:/Program Files/Git/bin/bash.exe"
        path = shutil.which(program or "") or (program if Path(program or "").exists() else None)
        if path:
            return _shell_prefix(Path(path).stem.lower(), path)
        choice = "auto"
    if choice in {"bash", "sh"} or (choice == "auto" and not _WINDOWS):
        for name in ("bash", "sh") if choice != "sh" else ("sh",):
            path = shutil.which(name)
            if path:
                return _shell_prefix(name, path)
        return ["/bin/sh", "-c"]
    if choice in {"pwsh", "powershell"} or choice == "auto":
        order = ("pwsh", "powershell") if choice != "powershell" else ("powershell",)
        for name in order:
            path = shutil.which(name)
            if path:
                return _shell_prefix(name, path)
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    return [comspec, "/d", "/s", "/c"]


def _shell_prefix(name: str, path: str) -> list[str]:
    if name in {"pwsh", "powershell"}:
        return [path, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
    if name == "cmd":
        return [path, "/d", "/s", "/c"]
    return [path, "-c"]


def shell_name(prefix: list[str]) -> str:
    """argv 前缀对应的 shell 简称（bash / sh / pwsh / powershell / cmd）。"""
    if not prefix:
        return "sh"
    return Path(prefix[0]).stem.lower()


# 子进程放进独立进程组（POSIX）：超时或回合被取消时整组杀掉。只杀 shell 本身的话，
# ``cmd &`` 起的后台子进程会变成孤儿，而且它继续占着 stdout 管道，
# 随后的 ``communicate()`` 会一直等到它自己退出。
_POSIX = os.name == "posix"
_REAP_TIMEOUT_S = 5.0
_CREATE_NO_WINDOW = 0x08000000


def _spawn_kwargs() -> dict:
    if _POSIX:
        return {"start_new_session": True}
    # Windows：独立进程组，便于 taskkill /T 连同子进程一起结束；不弹控制台窗口
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW}


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if _POSIX:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass  # 进程组已全部退出，或 pid 不是组长：退回只杀主进程
    elif proc.returncode is None:
        # Windows 没有进程组信号语义：用 taskkill /T 结束整棵进程树
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def popen_kwargs() -> dict:
    """``subprocess.Popen`` 的进程组参数：与 :func:`run_sandboxed` 一致，便于整组结束。"""
    return _spawn_kwargs()


def kill_pid_tree(pid: int) -> None:
    """结束进程及其子进程（POSIX 进程组 / Windows taskkill /T）。进程不存在时静默返回。"""
    if pid <= 0:
        return
    if _POSIX:
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
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
        stdout=_decode(out),
        stderr=_decode(err),
        timed_out=timed_out,
    )


def _decode(data: bytes | None) -> str:
    raw = data or b""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Windows 控制台程序常输出本地代码页（如 GBK）
        if _WINDOWS:
            try:
                return raw.decode(_console_encoding(), errors="replace")
            except LookupError:
                pass
        return raw.decode("utf-8", errors="replace")


def _console_encoding() -> str:
    try:
        import locale

        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:  # noqa: BLE001
        return sys.getdefaultencoding()


async def run_sandboxed(
    cmd: list[str],
    *,
    cwd: Path,
    timeout_s: float = 30.0,
    env: dict[str, str] | None = None,
    passthrough_env: Iterable[str] = (),
) -> ProcessResult:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        env=build_env(env, passthrough=passthrough_env),
        stdin=asyncio.subprocess.DEVNULL,
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
    shell: list[str] | None = None,
    passthrough_env: Iterable[str] = (),
) -> ProcessResult:
    """执行 shell 命令。

    ``shell`` 为 :func:`resolve_shell` 返回的 argv 前缀；为空时沿用系统默认 shell
    （POSIX 为 /bin/sh，Windows 为 cmd.exe），保持旧行为。
    """
    if shell:
        return await run_sandboxed(
            [*shell, command],
            cwd=cwd,
            timeout_s=timeout_s,
            env=env,
            passthrough_env=passthrough_env,
        )
    proc = await asyncio.create_subprocess_shell(
        command,
        cwd=str(cwd),
        env=build_env(env, passthrough=passthrough_env),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **_spawn_kwargs(),
    )
    return await _communicate(proc, timeout_s)


__all__ = [
    "ENV_ALLOWLIST",
    "PROXY_ENV",
    "SHELL_CHOICES",
    "ProcessResult",
    "build_env",
    "kill_pid_tree",
    "popen_kwargs",
    "resolve_shell",
    "run_sandboxed",
    "run_shell_sandboxed",
    "shell_name",
]
