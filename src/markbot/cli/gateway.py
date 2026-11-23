"""后台托管 MarkBot HTTP 入口与飞书渠道。

命令：

    markbot gateway start      后台启动（脱离终端）
    markbot gateway run        前台运行并守护子进程（systemd / 计划任务用，ADR 0010）
    markbot gateway stop
    markbot gateway restart
    markbot gateway status

进程输出追加写入 ~/.markbot/gateway/YYYY-MM-DD.log。
"""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from markbot.kernel import gateway_log
from markbot.kernel.config import (
    PlaintextSecretsError,
    enforce_secret_policy,
    load_config,
    markbot_home,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
START_TIMEOUT_S = 45
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def gateway_dir(home: Path) -> Path:
    return gateway_log.gateway_dir(home)


def log_path(home: Path) -> Path:
    return gateway_log.gateway_log_path(home)


def state_path(home: Path) -> Path:
    return gateway_dir(home) / "state.json"


def log_line(home: Path, message: str) -> None:
    gateway_log.write(home, message)


def _read_state(home: Path) -> dict[str, Any]:
    path = state_path(home)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(home: Path, state: dict[str, Any]) -> None:
    path = state_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _clear_state(home: Path) -> None:
    try:
        state_path(home).unlink()
    except FileNotFoundError:
        pass


def _is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError:
            pass
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(20):
        if not _is_alive(pid):
            return
        time.sleep(0.25)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


_LAUNCHER = r"""
import os, runpy, sys

log_path = sys.argv[1]
fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1)
os.dup2(fd, 2)
if fd > 2:
    os.close(fd)
sys.stdout = os.fdopen(1, "w", encoding="utf-8", errors="replace", buffering=1)
sys.stderr = os.fdopen(2, "w", encoding="utf-8", errors="replace", buffering=1)

target = sys.argv[2:]
if target[0] == "-m":
    module = target[1]
    sys.argv = [module, *target[2:]]
    runpy.run_module(module, run_name="__main__", alter_sys=True)
else:
    sys.argv = [*target]
    runpy.run_path(target[0], run_name="__main__")
"""


def _spawn(target: list[str], log_file: Path) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("MARKBOT_HOME", str(markbot_home()))
    args = [sys.executable, "-c", _LAUNCHER, str(log_file), *target]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
        return subprocess.Popen(args, **kwargs)

    flags = (
        subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    )
    try:
        # 脱离调用方所在的 Job Object，父进程退出后仍能继续运行
        return subprocess.Popen(
            args, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs
        )
    except OSError:
        return subprocess.Popen(args, creationflags=flags, **kwargs)


def _health(port: int = DEFAULT_PORT) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"http://{DEFAULT_HOST}:{port}/health", timeout=2
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def _tail(home: Path, lines: int = 15) -> list[str]:
    try:
        content = log_path(home).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return content.splitlines()[-lines:]


def _feishu_app_id(home: Path) -> str:
    path = home / "config.toml"
    if not path.exists():
        return ""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    section = (data.get("channels") or {}).get("feishu") or {}
    return str(section.get("app_id") or "").strip()


def _channel_script() -> Path | None:
    repo = Path(__file__).resolve().parents[3]
    for candidate in (
        repo / "channels/feishu/channel.py",
        Path.cwd() / "channels/feishu/channel.py",
    ):
        if candidate.is_file():
            return candidate
    return None


def _alive_pids(state: dict[str, Any]) -> list[int]:
    pids = []
    for key in ("server", "channel"):
        entry = state.get(key) or {}
        pid = int(entry.get("pid") or 0)
        if _is_alive(pid):
            pids.append(pid)
    return pids


def _wait_health(proc: subprocess.Popen) -> dict[str, Any] | None:
    deadline = time.time() + START_TIMEOUT_S
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        health = _health()
        if health:
            # 端口可能被其它进程占用：确认子进程仍然存活
            time.sleep(1.0)
            return health if proc.poll() is None else None
        time.sleep(0.5)
    return None


def _fail(home: Path, message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    tail = _tail(home)
    if tail:
        print("--- 日志末尾 ---", file=sys.stderr)
        for line in tail:
            print(line, file=sys.stderr)
    return 1


def _secret_policy_error() -> str | None:
    """serve 子进程启动前先做明文密钥检查，直接给出可读的错误而不是只留日志尾巴。"""
    try:
        cfg = load_config()
        enforce_secret_policy(cfg)
    except PlaintextSecretsError as exc:
        return str(exc)
    except (OSError, ValueError):
        return None  # 配置读取问题交给 serve 自己报告
    return None


def start(argv: list[str]) -> int:
    home = markbot_home()
    blocked = _secret_policy_error()
    if blocked:
        print(f"error: {blocked}", file=sys.stderr)
        return 2
    state = _read_state(home)
    if _alive_pids(state):
        print("gateway 已在运行，可用 `markbot gateway status` 查看或 `markbot gateway restart` 重启。")
        return 1

    app_id = _feishu_app_id(home)
    script = _channel_script() if app_id else None
    if app_id and script is None:
        print("error: 未找到 channels/feishu/channel.py，无法启动飞书渠道。", file=sys.stderr)
        return 1

    gateway_dir(home).mkdir(parents=True, exist_ok=True)
    log_file = log_path(home)
    log_line(home, "=== gateway start ===")

    server = _spawn(
        ["-m", "markbot", "serve", "--host", DEFAULT_HOST, "--port", str(DEFAULT_PORT)],
        log_file,
    )
    health = _wait_health(server)
    if health is None:
        _kill_tree(server.pid)
        return _fail(
            home,
            f"markbot serve 启动失败（端口 {DEFAULT_PORT} 可能被占用）。",
        )

    channel: subprocess.Popen | None = None
    if script is not None:
        channel = _spawn([str(script)], log_file)
        time.sleep(1.0)
        if channel.poll() is not None:
            _kill_tree(server.pid)
            return _fail(home, "飞书渠道启动失败，请检查 config.toml 的 [channels.feishu]。")

    state = {
        "started_at": _now(),
        "log": str(log_file),
        "server": {"pid": server.pid, "host": DEFAULT_HOST, "port": DEFAULT_PORT},
        "channel": {"pid": channel.pid, "script": str(script)} if channel else None,
    }
    _write_state(home, state)

    print("gateway 已启动")
    print(f"  markbot serve: pid {server.pid}  http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    if channel:
        print(f"  feishu 渠道:  pid {channel.pid}  {script}")
    else:
        print("  feishu 渠道:  未配置（仅启动 HTTP 入口）")
    print(f"  日志: {log_file}")
    log_line(home, f"started server={server.pid} channel={channel.pid if channel else '-'}")
    return 0


# ------------------------------------------------------------------ 前台守护
RESTART_BACKOFF_S = 5.0
MAX_RESTART_BACKOFF_S = 300.0
STABLE_AFTER_S = 600.0


def _spawn_child(target: list[str], log_file: Path) -> subprocess.Popen:
    """前台守护模式的子进程：不脱离父进程（父进程被 systemd/计划任务停止时一起结束）。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("MARKBOT_HOME", str(markbot_home()))
    args = [sys.executable, "-c", _LAUNCHER, str(log_file), *target]
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "env": env,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    return subprocess.Popen(args, **kwargs)


class _Child:
    def __init__(self, name: str, target: list[str]) -> None:
        self.name = name
        self.target = target
        self.proc: subprocess.Popen | None = None
        self.started_at = 0.0
        self.backoff = RESTART_BACKOFF_S
        self.next_start = 0.0
        self.restarts = 0


def run(argv: list[str]) -> int:
    """前台运行 serve 与飞书渠道，子进程退出后按指数退避自动拉起。

    给 systemd（Restart=always）或 Windows 计划任务托管用：本进程是唯一的长期进程，
    收到 SIGTERM / Ctrl-C 时先停渠道再停 serve（serve 会优雅停机）。
    """
    home = markbot_home()
    blocked = _secret_policy_error()
    if blocked:
        print(f"error: {blocked}", file=sys.stderr)
        return 2
    state = _read_state(home)
    if _alive_pids(state) or _is_alive(int((state.get("supervisor") or {}).get("pid") or 0)):
        print("gateway 已在运行（可能由 `gateway start` 或其它守护进程启动）。", file=sys.stderr)
        return 1
    gateway_dir(home).mkdir(parents=True, exist_ok=True)
    log_file = log_path(home)
    log_line(home, "=== gateway run (foreground supervisor) ===")
    children = [
        _Child(
            "server",
            ["-m", "markbot", "serve", "--host", DEFAULT_HOST, "--port", str(DEFAULT_PORT)],
        )
    ]
    app_id = _feishu_app_id(home)
    script = _channel_script() if app_id else None
    if script is not None:
        children.append(_Child("channel", [str(script)]))
    stopping = {"flag": False}

    def request_stop(*_args: Any) -> None:
        stopping["flag"] = True

    for name in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, request_stop)
            except (OSError, ValueError):
                pass

    def save_state() -> None:
        current: dict[str, Any] = {
            "started_at": _now(),
            "log": str(log_file),
            "mode": "foreground",
            "managed_by": "systemd" if os.environ.get("INVOCATION_ID") else "",
            "supervisor": {"pid": os.getpid()},
        }
        for child in children:
            if child.proc is not None:
                entry: dict[str, Any] = {"pid": child.proc.pid, "restarts": child.restarts}
                if child.name == "server":
                    entry.update(host=DEFAULT_HOST, port=DEFAULT_PORT)
                else:
                    entry["script"] = str(script)
                current[child.name] = entry
        _write_state(home, current)

    try:
        while not stopping["flag"]:
            now = time.time()
            changed = False
            for child in children:
                proc = child.proc
                if proc is not None and proc.poll() is None:
                    if now - child.started_at > STABLE_AFTER_S:
                        child.backoff = RESTART_BACKOFF_S
                    continue
                if proc is not None:
                    log_line(
                        home,
                        f"{child.name} 退出 code={proc.returncode}，{child.backoff:.0f}s 后重启",
                    )
                    child.proc = None
                    child.next_start = now + child.backoff
                    child.backoff = min(MAX_RESTART_BACKOFF_S, child.backoff * 2)
                    child.restarts += 1
                    changed = True
                    continue
                if now < child.next_start:
                    continue
                if child.name == "channel" and _health() is None:
                    continue  # 渠道依赖 serve：等 serve 就绪
                child.proc = _spawn_child(child.target, log_file)
                child.started_at = now
                log_line(home, f"启动 {child.name} pid={child.proc.pid}")
                changed = True
            if changed:
                save_state()
            time.sleep(1.0)
    finally:
        log_line(home, "=== gateway run 停止 ===")
        for child in reversed(children):
            if child.proc is not None and child.proc.poll() is None:
                _terminate(child.proc)
        _clear_state(home)
    return 0


def _terminate(proc: subprocess.Popen, timeout_s: float = 75.0) -> None:
    """先礼后兵：POSIX 发 SIGTERM（serve 会优雅停机），超时再杀进程树。"""
    try:
        if os.name == "nt":
            # 无控制台的子进程不一定收得到 CTRL_BREAK：等待时间短一些，再强制结束进程树
            proc.send_signal(signal.CTRL_BREAK_EVENT)  # type: ignore[attr-defined]
            timeout_s = min(timeout_s, 20.0)
        else:
            proc.terminate()
        proc.wait(timeout=timeout_s)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        if os.name == "nt":
            _kill_tree(proc.pid)
        else:
            try:
                proc.kill()
            except OSError:
                pass


def stop(argv: list[str]) -> int:
    home = markbot_home()
    state = _read_state(home)
    if not state:
        print("gateway 未运行。")
        return 1
    if state.get("managed_by") == "systemd":
        print("gateway 由 systemd 管理，请运行：systemctl --user stop markbot", file=sys.stderr)
        return 1
    supervisor = int((state.get("supervisor") or {}).get("pid") or 0)
    if supervisor and _is_alive(supervisor):
        # 前台守护模式：通知守护进程退出，由它按顺序停止子进程
        log_line(home, "=== gateway stop (supervisor) ===")
        if os.name == "nt":
            _kill_tree(supervisor)
        else:
            try:
                os.kill(supervisor, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(320):
                if not _is_alive(supervisor):
                    break
                time.sleep(0.25)
        _clear_state(home)
        print("gateway 已停止。")
        return 0

    log_line(home, "=== gateway stop ===")
    for key, label in (("channel", "feishu 渠道"), ("server", "markbot serve")):
        entry = state.get(key) or {}
        pid = int(entry.get("pid") or 0)
        if _is_alive(pid):
            _kill_tree(pid)
            print(f"已停止 {label} (pid {pid})")
    _clear_state(home)
    print("gateway 已停止。")
    log_line(home, "stopped")
    return 0


def restart(argv: list[str]) -> int:
    if _read_state(markbot_home()):
        stop([])
        time.sleep(1.0)
    return start(argv)


def status(argv: list[str]) -> int:
    home = markbot_home()
    state = _read_state(home)
    if not state:
        print("gateway 未运行。")
        print(f"日志目录: {gateway_dir(home)}")
        return 1

    print(f"启动时间: {state.get('started_at')}")
    print(f"日志: {state.get('log')}")

    server = state.get("server") or {}
    server_pid = int(server.get("pid") or 0)
    server_alive = _is_alive(server_pid)
    print(
        f"markbot serve: {'运行中' if server_alive else '已停止'}  pid={server_pid}  "
        f"http://{server.get('host')}:{server.get('port')}"
    )

    channel = state.get("channel") or {}
    channel_alive = False
    if channel:
        channel_pid = int(channel.get("pid") or 0)
        channel_alive = _is_alive(channel_pid)
        print(f"feishu 渠道: {'运行中' if channel_alive else '已停止'}  pid={channel_pid}")
    else:
        print("feishu 渠道: 未配置（仅 HTTP 入口）")

    health = _health()
    print(f"健康检查: {'ok' if health else '不可用'}  {health or ''}")

    ok = server_alive and (channel_alive if channel else True)
    return 0 if ok else 1


ACTIONS = {
    "start": start,
    "run": run,
    "stop": stop,
    "restart": restart,
    "status": status,
}


def gateway_command(argv: list[str]) -> int:
    action = argv[0] if argv else "status"
    if action not in ACTIONS:
        print("用法: markbot gateway start|run|stop|restart|status", file=sys.stderr)
        return 2
    return ACTIONS[action](argv[1:])
