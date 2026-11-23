"""系统运维（ADR 0015）：查看本机状态、进程、磁盘、端口与服务。

只读工具不声明能力（不访问工作区文件、不执行任意命令）：内部只运行固定的系统查询命令。
``kill_process`` 声明 ``exec:*``，由权限策略按执行命令的规则确认（strict 禁止）；不允许结束自身、父进程与系统关键进程。

有 psutil（``pip install -e ".[sysops]"``）时信息更完整；没有时退回 /proc、ps、tasklist 等。
"""

from __future__ import annotations

import asyncio
import csv
import importlib.util
import io
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from markbot.api import ExtensionAPI, Tool, ToolResult

IS_WINDOWS = sys.platform == "win32"
PROTECTED_NAMES = {
    "system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe",
    "init", "systemd", "launchd", "kthreadd", "sshd",
}


def _psutil() -> Any | None:
    if importlib.util.find_spec("psutil") is None:
        return None
    import psutil

    return psutil


def _run(argv: list[str], timeout: float = 15) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - 固定的系统查询命令
            argv, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace"
        )
    except FileNotFoundError:
        return 127, f"找不到命令 {argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]} 超时"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def human(n: float) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    return (f"{days} 天 " if days else "") + f"{hours} 小时 {minutes} 分"


# ------------------------------------------------------------------ 采集（纯函数便于测试）
def memory_info() -> dict[str, int]:
    ps = _psutil()
    if ps is not None:
        vm = ps.virtual_memory()
        return {"total": int(vm.total), "available": int(vm.available)}
    if IS_WINDOWS:
        import ctypes

        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return {"total": int(status.ullTotalPhys), "available": int(status.ullAvailPhys)}
    try:
        values: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, _, rest = line.partition(":")
            values[key] = int(rest.split()[0]) * 1024
        return {"total": values.get("MemTotal", 0), "available": values.get("MemAvailable", 0)}
    except (OSError, ValueError, IndexError):
        return {}


def uptime_seconds() -> float | None:
    ps = _psutil()
    if ps is not None:
        return time.time() - ps.boot_time()
    if IS_WINDOWS:
        import ctypes

        return ctypes.windll.kernel32.GetTickCount64() / 1000  # type: ignore[attr-defined]
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return None


def process_rows(limit: int = 15, sort: str = "cpu", name: str = "") -> list[dict[str, Any]]:
    ps = _psutil()
    rows: list[dict[str, Any]] = []
    if ps is not None:
        procs = list(ps.process_iter(["pid", "name", "username", "memory_info", "cmdline"]))
        for proc in procs:
            try:
                proc.cpu_percent(None)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.3)
        for proc in procs:
            try:
                info = proc.info
                rows.append({
                    "pid": info["pid"],
                    "name": info.get("name") or "",
                    "cpu": round(proc.cpu_percent(None), 1),
                    "rss": int(getattr(info.get("memory_info"), "rss", 0) or 0),
                    "user": info.get("username") or "",
                    "cmd": " ".join(info.get("cmdline") or [])[:160],
                })
            except Exception:  # noqa: BLE001 - 进程已退出或无权限
                continue
    elif IS_WINDOWS:
        code, out = _run(["tasklist", "/fo", "csv", "/nh"])
        if code == 0:
            for record in csv.reader(io.StringIO(out)):
                if len(record) < 5:
                    continue
                digits = "".join(ch for ch in record[4] if ch.isdigit())
                rows.append({"pid": int(record[1]), "name": record[0], "cpu": None,
                             "rss": int(digits or 0) * 1024, "user": "", "cmd": ""})
    else:
        code, out = _run(["ps", "-eo", "pid=,pcpu=,rss=,user=,comm=,args="])
        if code == 0:
            for line in out.splitlines():
                parts = line.split(None, 5)
                if len(parts) < 5:
                    continue
                rows.append({"pid": int(parts[0]), "name": parts[4], "cpu": float(parts[1]),
                             "rss": int(parts[2]) * 1024, "user": parts[3],
                             "cmd": (parts[5] if len(parts) > 5 else "")[:160]})
    if name:
        needle = name.lower()
        rows = [row for row in rows if needle in row["name"].lower() or needle in row["cmd"].lower()]
    key = "rss" if sort == "memory" or all(row["cpu"] is None for row in rows) else "cpu"
    rows.sort(key=lambda row: row[key] or 0, reverse=True)
    return rows[:limit]


def render_processes(rows: list[dict[str, Any]]) -> str:
    lines = ["  PID    CPU%   内存      名称 / 命令"]
    for row in rows:
        cpu = "-" if row["cpu"] is None else f"{row['cpu']:.1f}"
        lines.append(f"{row['pid']:>6} {cpu:>6} {human(row['rss']):>9}  {row['name']}"
                     + (f"  {row['cmd']}" if row["cmd"] and row["cmd"] != row["name"] else ""))
    return "\n".join(lines)


def check_tcp(host: str, port: int, timeout: float) -> tuple[bool, str, float]:
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, "可连接", (time.monotonic() - started) * 1000
    except socket.timeout:
        return False, "连接超时", (time.monotonic() - started) * 1000
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc.strerror or exc}", (time.monotonic() - started) * 1000


# ------------------------------------------------------------------ 参数
class InfoIn(BaseModel):
    pass


class ProcIn(BaseModel):
    sort: Literal["cpu", "memory"] = "cpu"
    limit: int = Field(default=15, ge=1, le=100)
    name: str = Field(default="", description="只看名称或命令行包含该关键词的进程")


class DiskIn(BaseModel):
    path: str = Field(default=".", description="要查看的目录", json_schema_extra={"x-markbot-role": "path"})
    top: int = Field(default=10, ge=0, le=50, description="列出占用最大的子目录/文件数（0 = 只看分区）")


class PortIn(BaseModel):
    host: str = Field(description="主机名或 IP", json_schema_extra={"x-markbot-role": "url"})
    port: int = Field(ge=1, le=65535)
    timeout_s: float = Field(default=3.0, ge=0.2, le=30)


class ServiceIn(BaseModel):
    name: str = Field(description="服务名（systemd 单元 / Windows 服务或计划任务名）")
    lines: int = Field(default=20, ge=0, le=200, description="附带最近日志行数（Linux journalctl）")


class KillIn(BaseModel):
    pid: int = Field(ge=1)
    force: bool = Field(default=False, description="强制结束（SIGKILL / taskkill /F）")


def _dir_sizes(root: Path, top: int, budget_s: float = 8.0) -> tuple[list[tuple[int, str]], bool]:
    deadline = time.monotonic() + budget_s
    sizes: list[tuple[int, str]] = []
    truncated = False
    for child in root.iterdir():
        total = 0
        try:
            if child.is_symlink():
                continue
            if child.is_file():
                total = child.stat().st_size
            else:
                for dirpath, _dirs, files in os.walk(child):
                    for fname in files:
                        try:
                            total += os.lstat(os.path.join(dirpath, fname)).st_size
                        except OSError:
                            continue
                    if time.monotonic() > deadline:
                        truncated = True
                        break
        except OSError:
            continue
        sizes.append((total, child.name + ("/" if child.is_dir() else "")))
        if time.monotonic() > deadline:
            truncated = True
            break
    sizes.sort(reverse=True)
    return sizes[:top], truncated


# ------------------------------------------------------------------ 工具
def setup(api: ExtensionAPI) -> None:
    config = getattr(api, "config", None)
    tools_cfg = (config or {}).get("tools") if isinstance(config, dict) else None
    section = (tools_cfg or {}).get("sysops") if isinstance(tools_cfg, dict) else None
    if isinstance(section, dict) and section.get("enabled") is False:
        return

    async def system_info(ctx: Any, args: InfoIn) -> ToolResult:
        def collect() -> str:
            mem = memory_info()
            lines = [
                f"主机: {socket.gethostname()}  系统: {platform.system()} {platform.release()} ({platform.machine()})",
                f"Python: {platform.python_version()}  MarkBot 进程: pid {os.getpid()}",
                f"CPU: {os.cpu_count()} 核" + (
                    "  负载: " + " / ".join(f"{value:.2f}" for value in os.getloadavg())
                    if hasattr(os, "getloadavg") else ""
                ),
            ]
            ps = _psutil()
            if ps is not None:
                lines[-1] += f"  使用率: {ps.cpu_percent(interval=0.5):.0f}%"
                try:
                    rss = ps.Process(os.getpid()).memory_info().rss
                    lines[1] += f"（内存 {human(rss)}）"
                except Exception:  # noqa: BLE001
                    pass
            if mem.get("total"):
                used = mem["total"] - mem.get("available", 0)
                lines.append(f"内存: 已用 {human(used)} / {human(mem['total'])}（{used / mem['total']:.0%}）")
            uptime = uptime_seconds()
            if uptime:
                lines.append(f"已运行: {_duration(uptime)}")
            home = getattr(getattr(ctx.env, "guard", None), "home", None)
            for label, target in (("工作区", Path(ctx.env.root)), ("MarkBot home", Path(home) if home else None)):
                if target is None:
                    continue
                try:
                    usage = shutil.disk_usage(target)
                    lines.append(
                        f"磁盘（{label} {target}）: 剩余 {human(usage.free)} / {human(usage.total)}"
                        f"（已用 {usage.used / usage.total:.0%}）"
                    )
                except OSError:
                    continue
            return "\n".join(lines)

        return ToolResult(content=await asyncio.to_thread(collect))

    async def list_processes(ctx: Any, args: ProcIn) -> ToolResult:
        rows = await asyncio.to_thread(process_rows, args.limit, args.sort, args.name)
        if not rows:
            return ToolResult(content="没有匹配的进程")
        note = "" if _psutil() is not None else "\n（未安装 psutil，信息有限）"
        return ToolResult(content=render_processes(rows) + note, details={"processes": rows})

    async def disk_usage(ctx: Any, args: DiskIn) -> ToolResult:
        target = Path(args.path).expanduser()
        if not target.is_absolute():
            target = Path(ctx.env.root) / target
        if not target.exists():
            return ToolResult.error(f"路径不存在: {target}")

        def collect() -> str:
            usage = shutil.disk_usage(target)
            lines = [f"分区: 剩余 {human(usage.free)} / {human(usage.total)}（已用 {usage.used / usage.total:.0%}）"]
            if args.top and target.is_dir():
                sizes, truncated = _dir_sizes(target, args.top)
                lines.append(f"{target} 下占用最大的项目" + ("（统计超时，结果不完整）" if truncated else "") + ":")
                lines.extend(f"  {human(size):>10}  {name}" for size, name in sizes)
            return "\n".join(lines)

        return ToolResult(content=await asyncio.to_thread(collect))

    async def check_port(ctx: Any, args: PortIn) -> ToolResult:
        ok, reason, ms = await asyncio.to_thread(check_tcp, args.host.strip(), args.port, args.timeout_s)
        text = f"{args.host}:{args.port} {reason}（{ms:.0f} ms）"
        return ToolResult(content=text, is_error=not ok, details={"open": ok, "ms": round(ms)})

    async def service_status(ctx: Any, args: ServiceIn) -> ToolResult:
        name = args.name.strip()
        if not name or any(ch in name for ch in ";&|`$<>\n"):
            return ToolResult.error("服务名不合法")

        def collect() -> str:
            if IS_WINDOWS:
                code, out = _run(["sc", "query", name])
                if code == 0:
                    return out.strip()
                code, out = _run(["schtasks", "/query", "/tn", name, "/v", "/fo", "list"])
                return out.strip() if code == 0 else f"没有找到服务或计划任务: {name}"
            parts: list[str] = []
            for scope in (["--user"], []):
                code, out = _run(["systemctl", *scope, "status", name, "--no-pager", "-n", "0"])
                if code in (0, 3) and "could not be found" not in out:
                    parts.append(out.strip())
                    if args.lines:
                        _, logs = _run(["journalctl", *scope, "-u", name, "-n", str(args.lines), "--no-pager"])
                        parts.append("最近日志:\n" + logs.strip())
                    break
            return "\n".join(parts) or f"没有找到 systemd 单元: {name}"

        return ToolResult(content=(await asyncio.to_thread(collect))[:12000])

    async def kill_process(ctx: Any, args: KillIn) -> ToolResult:
        if args.pid in {os.getpid(), os.getppid(), 1, 4}:
            return ToolResult.error("不允许结束 MarkBot 自身、它的父进程或系统进程", denied=True)
        rows = {row["pid"]: row for row in await asyncio.to_thread(process_rows, 100000, "memory", "")}
        row = rows.get(args.pid)
        if row is None:
            return ToolResult.error(f"没有进程 {args.pid}")
        if row["name"].lower() in PROTECTED_NAMES:
            return ToolResult.error(f"{row['name']} 是系统关键进程，不允许结束", denied=True)
        # 确认由权限策略按 exec 规则发起（standard 档位总要确认），这里不再重复询问
        ps = _psutil()
        try:
            if ps is not None:
                proc = ps.Process(args.pid)
                proc.kill() if args.force else proc.terminate()
            elif IS_WINDOWS:
                code, out = _run(["taskkill", "/PID", str(args.pid), *(["/F"] if args.force else [])])
                if code != 0:
                    return ToolResult.error(f"taskkill 失败: {out.strip()[:300]}")
            else:
                os.kill(args.pid, signal.SIGKILL if args.force else signal.SIGTERM)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.error(f"结束进程失败: {type(exc).__name__}: {exc}")
        return ToolResult(content=f"已向进程 {args.pid}（{row['name']}）发送{'强制结束' if args.force else '结束'}信号")

    specs: list[tuple[str, str, Any, Any, frozenset[str], float]] = [
        ("system_info", "查看本机状态：系统、CPU、内存、磁盘、运行时长", InfoIn, system_info, frozenset(), 30.0),
        ("list_processes", "列出占用 CPU / 内存最多的进程，可按名称过滤", ProcIn, list_processes, frozenset(), 30.0),
        ("disk_usage", "查看分区剩余空间与目录下占用最大的项目", DiskIn, disk_usage, frozenset({"fs.read:./**"}), 30.0),
        ("check_port", "检查主机端口能否连接（排查服务是否在线）", PortIn, check_port, frozenset({"net:*"}), 40.0),
        ("service_status", "查看 systemd 服务 / Windows 服务或计划任务的状态与最近日志", ServiceIn, service_status, frozenset(), 30.0),
        ("kill_process", "结束进程（按执行命令的权限规则确认）", KillIn, kill_process, frozenset({"exec:*"}), 30.0),
    ]
    for name, description, params, fn, caps, timeout in specs:
        api.register_tool(
            Tool(name=name, description=description, params=params, handler=fn, capabilities=caps, timeout_s=timeout)
        )


__all__ = ["check_tcp", "human", "memory_info", "process_rows", "render_processes", "setup"]
