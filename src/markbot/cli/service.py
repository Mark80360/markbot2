"""开机自启与进程托管：``markbot service install|uninstall|status``（ADR 0010）。

- Linux：生成 systemd **用户**服务 ``~/.config/systemd/user/markbot.service``，
  ``Restart=always``、``WatchdogSec``（serve 在全部后台组件存活时喂狗），
  运行 ``markbot gateway run``（前台守护 serve 与飞书渠道）。不登录也要运行时需要
  ``loginctl enable-linger $USER``。
- Windows：注册计划任务 ``MarkBot``：登录时启动、失败重启，另加每 5 分钟一次的触发器
  （已在运行则忽略），相当于看门狗。用 ``pythonw`` 启动，不弹控制台窗口。
- 其它平台：打印手动配置说明。

``--dry-run`` 只打印将要写入的文件与执行的命令。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from markbot.kernel.config import markbot_home

SERVICE_NAME = "markbot"
TASK_NAME = "MarkBot"
WATCHDOG_SEC = 180


def _repo_root() -> Path:
    # 能力包与飞书渠道脚本相对仓库根目录查找：服务的工作目录必须是仓库根
    return Path(__file__).resolve().parents[3]


def _unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / f"{SERVICE_NAME}.service"


def systemd_unit(*, python: str, home: Path, workdir: Path) -> str:
    return f"""[Unit]
Description=MarkBot personal agent (HTTP + channels)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={workdir}
Environment=MARKBOT_HOME={home}
Environment=PYTHONUTF8=1
Environment=PYTHONUNBUFFERED=1
ExecStart={python} -m markbot gateway run
Restart=always
RestartSec=5
# serve 进程在全部后台组件存活时发送 WATCHDOG=1；卡死或组件起不来时由 systemd 重启
NotifyAccess=all
WatchdogSec={WATCHDOG_SEC}
TimeoutStopSec=90
KillMode=mixed

[Install]
WantedBy=default.target
"""


def windows_launcher(*, home: Path, workdir: Path) -> str:
    return f'''# 由 `markbot service install` 生成：计划任务用 pythonw 运行本文件
import os
import runpy
import sys

os.environ.setdefault("MARKBOT_HOME", {str(home)!r})
os.environ.setdefault("PYTHONUTF8", "1")
os.chdir({str(workdir)!r})
sys.argv = ["markbot", "gateway", "run"]
runpy.run_module("markbot", run_name="__main__", alter_sys=True)
'''


def windows_task_xml(*, pythonw: str, launcher: Path, workdir: Path, user: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>MarkBot personal agent (HTTP + channels)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(pythonw)}</Command>
      <Arguments>"{escape(str(launcher))}"</Arguments>
      <WorkingDirectory>{escape(str(workdir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _run(cmd: list[str], *, dry_run: bool) -> int:
    print("$ " + " ".join(cmd))
    if dry_run:
        return 0
    try:
        return subprocess.run(cmd, check=False).returncode
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def install(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    home = markbot_home()
    workdir = _repo_root()
    if sys.platform.startswith("linux"):
        unit = systemd_unit(python=sys.executable, home=home, workdir=workdir)
        path = _unit_path()
        print(f"写入 {path}")
        if dry_run:
            print(unit)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(unit, encoding="utf-8")
        if shutil.which("systemctl") is None:
            print("未找到 systemctl：请手动启用上面的服务文件。", file=sys.stderr)
            return 1
        code = _run(["systemctl", "--user", "daemon-reload"], dry_run=dry_run)
        code = code or _run(
            ["systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service"],
            dry_run=dry_run,
        )
        print(
            "提示：要在未登录时也保持运行，执行一次 `loginctl enable-linger $USER`；"
            "查看日志：`journalctl --user -u markbot -f` 或 ~/.markbot/gateway/。"
        )
        return code
    if os.name == "nt":
        service_dir = home / "service"
        launcher = service_dir / "markbot-service.pyw"
        xml_path = service_dir / "markbot-task.xml"
        user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
        xml = windows_task_xml(
            pythonw=_pythonw(), launcher=launcher, workdir=workdir, user=user.strip("\\")
        )
        print(f"写入 {launcher}\n写入 {xml_path}")
        if dry_run:
            print(xml)
        else:
            service_dir.mkdir(parents=True, exist_ok=True)
            launcher.write_text(windows_launcher(home=home, workdir=workdir), encoding="utf-8")
            xml_path.write_text(xml, encoding="utf-16")
        code = _run(
            ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml_path), "/F"],
            dry_run=dry_run,
        )
        code = code or _run(["schtasks", "/Run", "/TN", TASK_NAME], dry_run=dry_run)
        print("计划任务「MarkBot」：登录时启动，每 5 分钟检查一次（已在运行则忽略）。")
        return code
    print(
        "当前平台没有自动安装支持。请用系统的服务管理器托管：\n"
        f"  工作目录: {workdir}\n  环境变量: MARKBOT_HOME={home}\n"
        f"  命令: {sys.executable} -m markbot gateway run\n  退出后自动重启"
    )
    return 1


def uninstall(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    if sys.platform.startswith("linux"):
        code = _run(
            ["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"],
            dry_run=dry_run,
        )
        path = _unit_path()
        if path.exists() and not dry_run:
            path.unlink()
            print(f"已删除 {path}")
        _run(["systemctl", "--user", "daemon-reload"], dry_run=dry_run)
        return code
    if os.name == "nt":
        _run(["schtasks", "/End", "/TN", TASK_NAME], dry_run=dry_run)
        return _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], dry_run=dry_run)
    print("当前平台没有自动安装支持。", file=sys.stderr)
    return 1


def status(argv: list[str]) -> int:
    if sys.platform.startswith("linux"):
        return _run(
            ["systemctl", "--user", "status", f"{SERVICE_NAME}.service", "--no-pager"],
            dry_run=False,
        )
    if os.name == "nt":
        return _run(["schtasks", "/Query", "/TN", TASK_NAME, "/V", "/FO", "LIST"], dry_run=False)
    print("当前平台没有自动安装支持。", file=sys.stderr)
    return 1


ACTIONS = {"install": install, "uninstall": uninstall, "status": status}


def service_command(argv: list[str]) -> int:
    action = argv[0] if argv else "status"
    if action not in ACTIONS:
        print("用法: markbot service install|uninstall|status [--dry-run]", file=sys.stderr)
        return 2
    return ACTIONS[action](argv[1:])


__all__ = ["service_command", "systemd_unit", "windows_launcher", "windows_task_xml"]
