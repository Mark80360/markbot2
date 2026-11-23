"""本地执行环境：在没有容器/远程 worker 时的默认实现。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path

from markbot.api.sandbox import ProcessResult, run_sandboxed, run_shell_sandboxed
from markbot.kernel import os_sandbox
from markbot.kernel.paths import PathGuard

logger = logging.getLogger("markbot.execution")


class LocalExecution:
    """把文件访问限制在 ``root`` 与守卫开放的例外目录内的执行环境。

    ``guard`` 为空时退化为只允许 ``root``（旧行为）；Kernel 会传入策略里的守卫，
    使受保护目录（MarkBot home、学习层）在任何档位下都不可经文件工具读写。
    """

    def __init__(
        self, root: Path, *, guard: PathGuard | None = None, os_sandbox_mode: str = "off"
    ) -> None:
        self.root = Path(root).resolve()
        self.guard = guard if guard is not None else PathGuard.build(self.root)
        mode = (os_sandbox_mode or "off").lower()
        self.os_sandbox = mode if os_sandbox.available(mode) else "off"
        if mode != "off" and self.os_sandbox == "off":
            # 配置要求隔离但当前平台不支持：明确告警，而不是静默降级成“看起来有沙箱”
            logger.warning("os_sandbox=%s 在当前平台不可用，命令将不经 OS 级隔离执行", mode)

    def resolve(self, raw: str, *, write: bool = False) -> Path:
        try:
            return self.guard.resolve(raw, write=write)
        except PermissionError as exc:
            raise PermissionError(f"{exc}（原始路径: {raw}）") from exc

    def read_text(self, raw: str) -> str:
        return self.resolve(raw).read_text(encoding="utf-8", errors="replace")

    def write_text(self, raw: str, content: str, *, create_dirs: bool = True) -> Path:
        path = self.resolve(raw, write=True)
        if create_dirs:
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def list_dir(self, raw: str, *, limit: int = 200) -> list[Path]:
        return sorted(self.resolve(raw).iterdir())[:limit]

    def glob(self, pattern: str, *, limit: int = 200) -> list[str]:
        matches: list[str] = []
        for path in self.root.glob(pattern):
            try:
                matches.append(path.relative_to(self.root).as_posix())
            except ValueError:
                continue
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
            )
        return await run_shell_sandboxed(command, cwd=self.root, timeout_s=timeout_s, env=extra)

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
        return await run_sandboxed(list(argv), cwd=workdir, timeout_s=timeout_s, env=extra)


__all__ = ["LocalExecution"]
