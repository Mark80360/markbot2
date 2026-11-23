"""Shell 冒烟测试（ADR 0016，T4-03）：在真实 shell 里跑命令，Linux（bash）与 Windows（pwsh / powershell）都要绿。

覆盖：默认 shell 的解析、输出与退出码、工作目录、环境变量注入与白名单、管道、超时与进程树清理、
``run_command`` 工具端到端、后台进程启动与结束。命令按当前 shell 的语法选择。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from markbot.api.sandbox import build_env, resolve_shell, run_shell_sandboxed, shell_name
from markbot.kernel.execution import LocalExecution

WINDOWS = os.name == "nt"
PREFIX = resolve_shell("auto")
SHELL = shell_name(PREFIX)
PS = SHELL in {"pwsh", "powershell"}

# 每种 shell 的写法：(打印环境变量, 打印当前目录, 睡眠 N 秒, 管道)
SYNTAX = {
    "pwsh": ("Write-Output $env:{name}", "(Get-Location).Path", "Start-Sleep -Seconds {n}", "'b','a' | Sort-Object"),
    "cmd": ("echo %{name}%", "cd", "ping -n {n1} 127.0.0.1 >nul", "(echo b & echo a) | sort"),
    "posix": ('echo "${name}"', "pwd", "sleep {n}", "printf 'b\\na\\n' | sort"),
}


def _syntax() -> tuple[str, str, str, str]:
    if PS:
        return SYNTAX["pwsh"]
    if SHELL == "cmd":
        return SYNTAX["cmd"]
    return SYNTAX["posix"]


def _run(command: str, cwd: Path, **kwargs):
    return asyncio.run(run_shell_sandboxed(command, cwd=cwd, shell=PREFIX, **kwargs))


def test_default_shell_matches_platform():
    if WINDOWS:
        assert SHELL in {"pwsh", "powershell", "cmd"}
    else:
        assert SHELL in {"bash", "sh"}
    assert PREFIX and Path(PREFIX[0]).name


def test_echo_and_exit_code(tmp_path):
    ok = _run("echo markbot-smoke", tmp_path)
    assert ok.returncode == 0 and "markbot-smoke" in ok.stdout
    failed = _run("exit 3", tmp_path)
    assert failed.returncode == 3 and not failed.ok


def test_working_directory(tmp_path):
    _, pwd, _, _ = _syntax()
    result = _run(pwd, tmp_path)
    assert result.returncode == 0
    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_env_injection_and_allowlist(tmp_path, monkeypatch):
    show, _, _, _ = _syntax()
    injected = _run(show.format(name="MB_SMOKE_VALUE", n=0, n1=1), tmp_path, env={"MB_SMOKE_VALUE": "injected-42"})
    assert "injected-42" in injected.stdout
    # 白名单之外的父进程变量不透传
    monkeypatch.setenv("MB_SMOKE_PRIVATE", "should-not-leak")
    assert "MB_SMOKE_PRIVATE" not in build_env()
    leaked = _run(show.format(name="MB_SMOKE_PRIVATE", n=0, n1=1), tmp_path)
    assert "should-not-leak" not in leaked.stdout
    assert "PATH" in build_env() or "Path" in build_env()


def test_pipeline(tmp_path):
    _, _, _, pipe = _syntax()
    result = _run(pipe, tmp_path)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    assert result.returncode == 0 and lines == ["a", "b"]


def test_timeout_kills_command(tmp_path):
    _, _, sleep, _ = _syntax()
    started = time.monotonic()
    result = _run(sleep.format(n=30, n1=31, name=""), tmp_path, timeout_s=2)
    elapsed = time.monotonic() - started
    assert result.timed_out and not result.ok
    assert elapsed < 20, f"超时后没有及时结束（{elapsed:.1f}s）"


def test_stderr_is_captured(tmp_path):
    command = "[Console]::Error.WriteLine('to-stderr')" if PS else "echo to-stderr 1>&2"
    result = _run(command, tmp_path)
    assert "to-stderr" in result.stderr


@pytest.mark.skipif(WINDOWS, reason="Windows 控制台代码页因机器而异，中文输出在 Linux/macOS 上验证")
def test_unicode_output(tmp_path):
    result = _run("echo 你好，MarkBot", tmp_path)
    assert "你好，MarkBot" in result.stdout


def test_local_execution_uses_configured_shell(tmp_path):
    env = LocalExecution(tmp_path, shell="auto")
    assert env.shell == SHELL
    result = asyncio.run(env.exec("echo via-local-execution", timeout_s=30))
    assert result.ok and "via-local-execution" in result.stdout


def test_run_command_tool_end_to_end(make_kernel, project):
    from markbot.kernel.config import load_config
    from markbot.kernel.providers.base import ModelReply, ToolCall

    cfg = load_config(home=project.parent / "home", project_root=project)
    cfg.permissions.profile = "trusted"
    cfg.permissions.trusted_exec = "all"
    cfg.shell.program = "auto"
    kernel = make_kernel(
        [
            ModelReply(tool_calls=[ToolCall(id="c1", name="run_command", args={"command": "echo tool-smoke-ok"})]),
            "完成",
        ],
        config=cfg,
    )
    result = asyncio.run(kernel.run("跑一下 echo", archive=False))
    assert result.stopped_reason == "completed", result.error
    tool_messages = [m for m in kernel.session.messages if m.role == "tool"]
    assert tool_messages and "tool-smoke-ok" in tool_messages[0].content
    assert not tool_messages[0].is_error


def test_spawned_process_can_be_stopped(tmp_path):
    from markbot.api.sandbox import kill_pid_tree

    _, _, sleep, _ = _syntax()
    env = LocalExecution(tmp_path, shell="auto")
    proc = env.spawn(sleep.format(n=60, n1=61, name="")) if hasattr(env, "spawn") else None
    if proc is None:
        pytest.skip("执行环境不支持 spawn")
    try:
        assert proc.poll() is None
    finally:
        kill_pid_tree(proc.pid)
    deadline = time.monotonic() + 15
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.2)
    assert proc.poll() is not None, "进程树没有被结束"


def test_python_subprocess_through_shell(tmp_path):
    exe = sys.executable
    command = f"& '{exe}' -c \"print(6*7)\"" if PS else f'"{exe}" -c "print(6*7)"'
    result = _run(command, tmp_path)
    assert result.returncode == 0 and "42" in result.stdout
