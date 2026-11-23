"""后台进程：启动长时间运行的命令（dev server、watch、长构建），按需读取输出、写入输入、结束（ADR 0012）。

``run_command`` 最长 600 秒、只返回最终输出；这里的进程跨回合存活，输出进环形缓冲区，模型用
``process_read`` 增量读取（``offset`` 续读，可等待新输出）。

- 进程经执行环境的 ``spawn`` 启动：与 ``run_command`` 相同的 shell、环境白名单、进程组与 OS 级隔离；
  权限检查与 ``run_command`` 相同（``command`` 参数）。
- 进程归属启动它的会话：其它会话看不到、也不能操作。会话结束或进程退出时整组结束。
- 使用管道而不是伪终端：需要 TTY 的交互程序（vim、top）不适用。
"""

from __future__ import annotations

import atexit
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from markbot.api import EventName, ExtensionAPI, Tool, ToolResult
from markbot.api.sandbox import kill_pid_tree
from markbot.extensions import _registry

BUFFER_BYTES = 512 * 1024
MAX_PROCESSES = 8
MAX_READ_CHARS = 12_000
# 进程表放在共享模块里：热重载重新导入本扩展后句柄仍然有效
_PROCS = _registry.PROCS
_LOCK = _registry.PROCS_LOCK
_COUNTER = _registry.PROC_COUNTER


@dataclass
class _Proc:
    handle: str
    session_id: str
    command: str
    popen: Any
    started_at: float = field(default_factory=time.time)
    buffer: bytearray = field(default_factory=bytearray)
    dropped: int = 0  # 环形缓冲丢掉的字节数（偏移量从 0 开始连续计数）
    read_offset: int = 0
    exit_code: int | None = None
    ended_at: float | None = None
    cond: threading.Condition = field(default_factory=threading.Condition)

    @property
    def total(self) -> int:
        return self.dropped + len(self.buffer)

    def running(self) -> bool:
        return self.exit_code is None


def _reader(proc: _Proc) -> None:
    stream = proc.popen.stdout
    try:
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(4096)
            if not chunk:
                break
            with proc.cond:
                proc.buffer.extend(chunk)
                overflow = len(proc.buffer) - BUFFER_BYTES
                if overflow > 0:
                    del proc.buffer[:overflow]
                    proc.dropped += overflow
                proc.cond.notify_all()
    except (OSError, ValueError):
        pass
    code = proc.popen.wait()
    with proc.cond:
        proc.exit_code = code
        proc.ended_at = time.time()
        proc.cond.notify_all()


def _kill(proc: _Proc) -> None:
    if proc.running():
        kill_pid_tree(proc.popen.pid)
        try:
            proc.popen.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
    for stream in (proc.popen.stdin, proc.popen.stdout):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def kill_all(session_id: str | None = None) -> int:
    with _LOCK:
        targets = [p for p in _PROCS.values() if session_id is None or p.session_id == session_id]
    for proc in targets:
        _kill(proc)
    with _LOCK:
        for proc in targets:
            if not proc.running():
                _PROCS.pop(proc.handle, None)
    return len(targets)


atexit.register(kill_all)


def _session_id(ctx) -> str:
    return str(getattr(getattr(ctx, "session", None), "id", "") or "")


def _get(ctx, handle: str) -> _Proc | None:
    with _LOCK:
        proc = _PROCS.get(handle)
    if proc is None or proc.session_id != _session_id(ctx):
        return None
    return proc


def _status(proc: _Proc) -> str:
    if proc.running():
        return f"运行中（已 {int(time.time() - proc.started_at)}s）"
    return f"已退出，退出码 {proc.exit_code}"


class StartIn(BaseModel):
    command: str = Field(description="要在后台运行的命令", json_schema_extra={"x-markbot-role": "command"})
    cwd: str = Field(default="", description="工作目录（默认工作区根目录）", json_schema_extra={"x-markbot-role": "path"})
    wait_s: float = Field(default=2.0, ge=0, le=30, description="启动后等待多久返回首批输出")


class ReadIn(BaseModel):
    handle: str = Field(description="process_start 返回的进程句柄")
    offset: int | None = Field(default=None, ge=0, description="从这个字节偏移开始读；默认接着上次读到的位置")
    wait_s: float = Field(default=0, ge=0, le=30, description="没有新输出时最多等待的秒数")
    max_chars: int = Field(default=4000, ge=100, le=MAX_READ_CHARS)


class WriteIn(BaseModel):
    handle: str
    input: str = Field(description="写入进程标准输入的文本")
    newline: bool = Field(default=True, description="末尾是否追加换行")


class HandleIn(BaseModel):
    handle: str


class EmptyIn(BaseModel):
    pass


def _read(proc: _Proc, offset: int | None, wait_s: float, max_chars: int) -> ToolResult:
    start = proc.read_offset if offset is None else offset
    deadline = time.time() + wait_s
    with proc.cond:
        while proc.total <= start and proc.running() and time.time() < deadline:
            proc.cond.wait(timeout=max(0.05, deadline - time.time()))
        begin = max(start, proc.dropped)
        data = bytes(proc.buffer[begin - proc.dropped :])
        skipped = begin - start
        running = proc.running()
        code = proc.exit_code
    text = data.decode("utf-8", errors="replace")
    truncated = len(text) > max_chars
    if truncated:
        # 太多时保留最新的部分（日志尾部最有用）
        text = "…（前面的输出省略）\n" + text[-max_chars:]
    end = begin + len(data)
    proc.read_offset = end
    head = f"[{proc.handle}] {_status(proc)}；输出偏移 {start}→{end}"
    if skipped:
        head += f"（有 {skipped} 字节早期输出已从缓冲区丢弃）"
    body = text if text else "（暂无新输出）"
    return ToolResult(
        content=f"{head}\n{body}",
        details={"handle": proc.handle, "running": running, "exit_code": code, "next_offset": end},
    )


def setup(api: ExtensionAPI) -> None:
    async def process_start(ctx, args: StartIn) -> ToolResult:
        spawn = getattr(ctx.env, "spawn", None)
        if not callable(spawn):
            return ToolResult.error("当前执行环境不支持后台进程")
        session_id = _session_id(ctx)
        with _LOCK:
            running = [p for p in _PROCS.values() if p.running()]
        if len(running) >= MAX_PROCESSES:
            return ToolResult.error(
                f"后台进程已达上限 {MAX_PROCESSES} 个，请先用 process_kill 结束不需要的进程"
            )
        try:
            popen = spawn(args.command, cwd=args.cwd or None)
        except PermissionError as exc:
            return ToolResult.error(f"permission denied: {exc}", denied=True)
        except OSError as exc:
            return ToolResult.error(f"启动失败: {exc}")
        proc = _Proc(handle=f"p{next(_COUNTER)}", session_id=session_id, command=args.command, popen=popen)
        with _LOCK:
            _PROCS[proc.handle] = proc
        threading.Thread(target=_reader, args=(proc,), daemon=True, name=f"proc-{proc.handle}").start()
        result = _read(proc, 0, args.wait_s, 4000)
        return ToolResult(
            content=f"已启动 {proc.handle}: {args.command}\n{result.content}",
            details={**result.details, "pid": popen.pid},
        )

    async def process_read(ctx, args: ReadIn) -> ToolResult:
        proc = _get(ctx, args.handle)
        if proc is None:
            return ToolResult.error(f"没有这个进程: {args.handle}（只能访问本会话启动的进程）")
        return _read(proc, args.offset, args.wait_s, args.max_chars)

    async def process_write(ctx, args: WriteIn) -> ToolResult:
        proc = _get(ctx, args.handle)
        if proc is None:
            return ToolResult.error(f"没有这个进程: {args.handle}")
        if not proc.running():
            return ToolResult.error(f"进程已退出（退出码 {proc.exit_code}），不能写入")
        data = (args.input + ("\n" if args.newline else "")).encode("utf-8")
        try:
            proc.popen.stdin.write(data)
            proc.popen.stdin.flush()
        except (OSError, ValueError) as exc:
            return ToolResult.error(f"写入失败: {exc}")
        return ToolResult(content=f"已写入 {len(data)} 字节到 {proc.handle}")

    async def process_kill(ctx, args: HandleIn) -> ToolResult:
        proc = _get(ctx, args.handle)
        if proc is None:
            return ToolResult.error(f"没有这个进程: {args.handle}")
        was_running = proc.running()
        _kill(proc)
        tail = _read(proc, max(proc.read_offset, proc.total - 2000), 0.5, 2000)
        with _LOCK:
            _PROCS.pop(proc.handle, None)
        state = "已结束" if was_running else "进程此前已退出"
        return ToolResult(content=f"{state} {proc.handle}（退出码 {proc.exit_code}）\n{tail.content}")

    async def process_list(ctx, args: EmptyIn) -> ToolResult:
        session_id = _session_id(ctx)
        with _LOCK:
            mine = [p for p in _PROCS.values() if p.session_id == session_id]
        if not mine:
            return ToolResult(content="（本会话没有后台进程）")
        lines = [f"- {p.handle} pid={p.popen.pid} {_status(p)} 输出 {p.total} 字节: {p.command[:120]}" for p in mine]
        return ToolResult(content="\n".join(lines), details={"count": len(mine)})

    async def _on_session_end(*_args: Any, **kwargs: Any) -> None:
        session = kwargs.get("session")
        if session is not None:
            kill_all(str(getattr(session, "id", "") or ""))

    async def _on_shutdown(*_args: Any, **_kwargs: Any) -> None:
        kill_all()

    api.on(EventName.SESSION_END, _on_session_end)
    api.on(EventName.SHUTDOWN, _on_shutdown)

    common = {"concurrency_safe": False}
    api.register_tool(
        Tool(
            name="process_start",
            description=(
                "在后台启动长时间运行的命令（开发服务器、watch、长构建），立即返回句柄与首批输出；"
                "之后用 process_read 读输出、process_kill 结束。一次性命令请用 run_command"
            ),
            params=StartIn,
            handler=process_start,
            capabilities=frozenset({"exec:*"}),
            timeout_s=60.0,
            **common,
        )
    )
    api.register_tool(
        Tool(
            name="process_read",
            description="读取后台进程的新输出（默认接着上次的位置），可等待新输出",
            params=ReadIn,
            handler=process_read,
            timeout_s=45.0,
        )
    )
    api.register_tool(
        Tool(
            name="process_write",
            description="向后台进程的标准输入写入文本",
            params=WriteIn,
            handler=process_write,
            **common,
        )
    )
    api.register_tool(
        Tool(
            name="process_kill",
            description="结束后台进程（连同其子进程），返回最后的输出",
            params=HandleIn,
            handler=process_kill,
            **common,
        )
    )
    api.register_tool(
        Tool(
            name="process_list",
            description="列出本会话启动的后台进程及状态",
            params=EmptyIn,
            handler=process_list,
        )
    )


__all__ = ["kill_all", "setup"]
