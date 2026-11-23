"""执行环境契约：工具访问文件与子进程的唯一入口（稳定契约的一部分）。

Kernel 注入实现，扩展只依赖本协议。这样把「在哪执行」和「执行什么」分开：
未来接入容器、远程 worker 或只读沙箱时，不需要改扩展代码。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from markbot.api.sandbox import ProcessResult


@runtime_checkable
class ExecutionEnvironment(Protocol):
    root: Path

    def resolve(self, raw: str) -> Path:
        """解析路径并保证不越出 ``root``；越界抛 ``PermissionError``。"""
        ...

    def read_text(self, raw: str) -> str:
        ...

    def write_text(self, raw: str, content: str, *, create_dirs: bool = True) -> Path:
        ...

    def list_dir(self, raw: str, *, limit: int = 200) -> list[Path]:
        ...

    def glob(self, pattern: str, *, limit: int = 200) -> list[str]:
        """返回相对 ``root`` 的 POSIX 路径，便于模型直接引用。"""
        ...

    async def exec(
        self,
        command: str,
        *,
        timeout_s: float = 30.0,
        env: Mapping[str, str] | None = None,
    ) -> ProcessResult:
        """执行命令。``env`` 是额外环境变量（如注入的密钥），叠加在白名单环境之上。

        ``env`` 为 v0.x 兼容新增参数：调用方只在确有需要时传入，旧实现不受影响。
        """
        ...

    # 可选（v0.x 兼容新增）：以 argv 形式执行，不经 shell。扩展应先 ``getattr`` 判断。
    # async def exec_argv(self, argv: list[str], *, cwd: Path | None = None,
    #                     timeout_s: float = 30.0, env: Mapping[str, str] | None = None,
    #                     readable: tuple[Path, ...] = ()) -> ProcessResult: ...
    # ``readable`` 为本次额外只读开放的目录（OS 级隔离开启时生效），调用方负责保证可信。


__all__ = ["ExecutionEnvironment"]
