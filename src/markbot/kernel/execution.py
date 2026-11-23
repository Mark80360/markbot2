"""本地执行环境：在没有容器/远程 worker 时的默认实现。"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path

from markbot.api.sandbox import (
    ProcessResult,
    build_env,
    popen_kwargs,
    resolve_shell,
    run_sandboxed,
    run_shell_sandboxed,
    shell_name,
)
from markbot.kernel import os_sandbox
from markbot.kernel.paths import PathGuard

logger = logging.getLogger("markbot.execution")

_GLOB_CHARS = frozenset("*?[")


def _split_glob(pattern: str) -> tuple[str, str]:
    """把 glob 模式拆成「不含通配符的基准目录」与「相对模式」。

    ``/a/b/**/*.py`` -> (``/a/b``, ``**/*.py``)；``src/*.py`` -> (``src``, ``*.py``)。
    """
    text = pattern.replace("\\", "/")
    parts = text.split("/")
    base: list[str] = []
    for index, part in enumerate(parts):
        if _GLOB_CHARS & set(part):
            return "/".join(base) or ("/" if text.startswith("/") else "."), "/".join(parts[index:])
        base.append(part)
    # 没有通配符：整个就是一个路径，按「匹配它自己」处理
    head, _, tail = text.rstrip("/").rpartition("/")
    return head or ("/" if text.startswith("/") else "."), tail or text


class LocalExecution:
    """把文件访问限制在 ``root`` 与守卫开放的例外目录内的执行环境。

    ``guard`` 为空时退化为只允许 ``root``（旧行为）；Kernel 会传入策略里的守卫，
    使受保护目录（MarkBot home、学习层）在任何档位下都不可经文件工具读写。

    ``shell``：``[shell] program`` 的取值；为 ``None`` 时沿用系统默认 shell（旧行为）。
    ``passthrough_env``：额外透传给子进程的环境变量名。
    """

    def __init__(
        self,
        root: Path,
        *,
        guard: PathGuard | None = None,
        os_sandbox_mode: str = "off",
        shell: str | None = None,
        passthrough_env: Iterable[str] = (),
    ) -> None:
        self.root = Path(root).resolve()
        self.guard = guard if guard is not None else PathGuard.build(self.root)
        mode = (os_sandbox_mode or "off").lower()
        self.os_sandbox = mode if os_sandbox.available(mode) else "off"
        if mode != "off" and self.os_sandbox == "off":
            # 配置要求隔离但当前平台不支持：明确告警，而不是静默降级成“看起来有沙箱”
            logger.warning("os_sandbox=%s 在当前平台不可用，命令将不经 OS 级隔离执行", mode)
        self.shell_prefix: list[str] | None = resolve_shell(shell) if shell else None
        self.passthrough_env = tuple(str(n) for n in passthrough_env if str(n).strip())

    @property
    def shell(self) -> str:
        """当前执行命令所用 shell 的简称，供上下文告知模型。"""
        if self.os_sandbox == "macos":
            return "sh"
        if self.shell_prefix:
            return shell_name(self.shell_prefix)
        return "cmd" if os.name == "nt" else "sh"

    def resolve(self, raw: str, *, write: bool = False) -> Path:
        try:
            return self.guard.resolve(raw, write=write)
        except PermissionError as exc:
            raise PermissionError(f"{exc}（原始路径: {raw}）") from exc

    def read_text(self, raw: str) -> str:
        return self.resolve(raw).read_text(encoding="utf-8", errors="replace")

    def write_text(self, raw: str, content: str, *, create_dirs: bool = True) -> Path:
        """原子写入（临时文件 + ``os.replace``）：写到一半崩溃不会留下半个文件（ADR 0012）。

        仍按文本模式写（换行符按平台默认），与旧行为一致；需要精确字节时用 :meth:`write_bytes`。
        """
        path = self.resolve(raw, write=True)
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace(path, lambda fh: fh.write(content), mode="w")
        return path

    # ---- 以下为 v0.x 兼容新增的可选方法：扩展应先 ``getattr`` 判断（ADR 0012）
    def read_bytes(self, raw: str) -> bytes:
        return self.resolve(raw).read_bytes()

    def write_bytes(self, raw: str, data: bytes, *, create_dirs: bool = True) -> Path:
        path = self.resolve(raw, write=True)
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_replace(path, lambda fh: fh.write(data), mode="wb")
        return path

    def delete(self, raw: str) -> Path:
        path = self.resolve(raw, write=True)
        if path.is_dir():
            raise IsADirectoryError(f"不能删除目录: {raw}")
        path.unlink()
        return path

    def spawn(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.Popen:
        """启动后台进程（stdin/stdout 管道，stderr 合并到 stdout），调用方负责回收。

        与 :meth:`exec` 使用同一个 shell、环境白名单、进程组与 OS 级隔离。
        """
        workdir = self.resolve(cwd) if cwd else self.root
        shell = False
        argv: list[str] | str
        if self.os_sandbox == "macos":
            argv = os_sandbox.wrap(command, self.guard)
        elif self.shell_prefix:
            argv = [*self.shell_prefix, command]
        elif os.name == "nt":
            # 与 create_subprocess_shell 相同：交给 cmd /c，命令里的引号原样保留
            argv, shell = command, True
        else:
            argv = ["/bin/sh", "-c", command]
        return subprocess.Popen(
            argv,
            shell=shell,
            cwd=str(workdir),
            env=build_env(dict(env) if env else None, passthrough=self.passthrough_env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **popen_kwargs(),
        )

    def list_dir(self, raw: str, *, limit: int = 200) -> list[Path]:
        return sorted(self.resolve(raw).iterdir())[:limit]

    def glob(self, pattern: str, *, limit: int = 200) -> list[str]:
        """按 glob 模式查找文件。

        相对模式以 ``root`` 为基准，结果为相对 ``root`` 的 POSIX 路径（旧行为）。
        绝对模式（含 ``~``、盘符）先把不含通配符的前缀解析成基准目录并经守卫校验，
        例如 ``~/.markbot/workspace/**/*.py``；落在 ``root`` 之外的结果返回绝对路径。
        每个结果再逐一过守卫，受保护目录里的文件不会出现。
        """
        text = pattern.strip()
        if _is_absolute(text):
            # 只解析不含通配符的前缀：对带 * 的整段路径 resolve() 在 Windows 上会失败
            base_raw, rel = _split_glob(os.path.expanduser(self.guard._expand_vars(text)))
            base = self.resolve(base_raw)
        else:
            base, rel = self.root, text.replace("\\", "/")
        if not rel or not base.is_dir():
            return []
        matches: list[str] = []
        for path in base.glob(rel):
            try:
                resolved = path.resolve()
            except (OSError, RuntimeError):
                continue
            if self.guard.denial(resolved, write=False) is not None:
                continue
            try:
                matches.append(path.relative_to(self.root).as_posix())
            except ValueError:
                matches.append(path.as_posix())
            if len(matches) >= limit:
                break
        return sorted(matches)

    async def exec(
        self,
        command: str,
        *,
        timeout_s: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        extra = dict(env) if env else None
        if self.os_sandbox == "macos":
            return await run_sandboxed(
                os_sandbox.wrap(command, self.guard),
                cwd=self.root,
                timeout_s=timeout_s,
                env=extra,
                passthrough_env=self.passthrough_env,
            )
        return await run_shell_sandboxed(
            command,
            cwd=self.root,
            timeout_s=timeout_s,
            env=extra,
            shell=self.shell_prefix,
            passthrough_env=self.passthrough_env,
        )

    async def exec_argv(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        timeout_s: float = 30.0,
        env: Mapping[str, str] | None = None,
        readable: tuple[Path, ...] = (),
    ) -> ProcessResult:
        """不经 shell 执行 argv（技能脚本等）；开启 OS 级隔离时同样受保护。

        ``readable``：本次额外只读开放的目录（调用方负责保证它是可信的，例如已校验过的
        技能目录）。工作目录 ``cwd`` 在受保护目录里时也会被自动加入只读开放。
        """
        workdir = Path(cwd).resolve() if cwd is not None else self.root
        extra = dict(env) if env else None
        if self.os_sandbox == "macos":
            opened = tuple(readable)
            if self.guard.is_protected(workdir):
                opened = (*opened, workdir)
            argv = os_sandbox.wrap_argv(list(argv), self.guard, extra_readable=opened)
        return await run_sandboxed(
            list(argv),
            cwd=workdir,
            timeout_s=timeout_s,
            env=extra,
            passthrough_env=self.passthrough_env,
        )


def _atomic_replace(path: Path, write, *, mode: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        if "b" in mode:
            with open(tmp, mode) as fh:
                write(fh)
        else:
            with open(tmp, mode, encoding="utf-8") as fh:
                write(fh)
        try:
            # 保留原文件权限（例如可执行脚本）
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _is_absolute(pattern: str) -> bool:
    text = pattern.strip()
    return (
        text.startswith(("/", "~", "$HOME", "${HOME}", "$MARKBOT_HOME", "${MARKBOT_HOME}"))
        or (len(text) > 2 and text[1] == ":" and text[2] in "/\\")
    )


__all__ = ["LocalExecution"]
