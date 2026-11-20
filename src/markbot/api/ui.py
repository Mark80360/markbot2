"""最小 UI 契约：确认框与文本输出。"""

from __future__ import annotations

import asyncio
import sys
from typing import Protocol


class UI(Protocol):
    def info(self, message: str) -> None: ...

    def status(self, message: str) -> None:
        """执行过程提示（模型请求、工具调用等）。"""
        ...

    def warn(self, message: str) -> None: ...

    def error(self, message: str) -> None: ...

    async def confirm(self, title: str, message: str) -> bool: ...

    async def prompt(self, label: str) -> str: ...


class ConsoleUI:
    """终端 UI；非交互时确认一律拒绝。"""

    def __init__(
        self, *, interactive: bool | None = None, show_status: bool = True
    ) -> None:
        self.interactive = sys.stdin.isatty() if interactive is None else interactive
        self.show_status = show_status

    def info(self, message: str) -> None:
        print(message)

    def status(self, message: str) -> None:
        if self.interactive and self.show_status:
            print(f"· {message}", file=sys.stderr, flush=True)

    def warn(self, message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)

    def error(self, message: str) -> None:
        print(f"error: {message}", file=sys.stderr)

    async def confirm(self, title: str, message: str) -> bool:
        if not self.interactive:
            return False
        answer = await asyncio.to_thread(input, f"{title}\n{message}\n[y/N] ")
        return answer.strip().lower() in {"y", "yes"}

    async def prompt(self, label: str) -> str:
        return await asyncio.to_thread(input, label)


class NullUI:
    def info(self, message: str) -> None:
        pass

    def status(self, message: str) -> None:
        pass

    def warn(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass

    async def confirm(self, title: str, message: str) -> bool:
        return False

    async def prompt(self, label: str) -> str:
        return ""


__all__ = ["ConsoleUI", "NullUI", "UI"]
