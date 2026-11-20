"""事件总线：按注册顺序执行，异常隔离，block 短路。"""

from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Handler = Callable[..., Any]


@dataclass
class EventOutcome:
    blocked: bool = False
    block_reason: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[tuple[str, Handler]]] = defaultdict(list)

    def subscribe(self, event: str, owner: str, handler: Handler) -> Callable[[], None]:
        entry = (owner, handler)
        self._handlers[event].append(entry)

        def unsubscribe() -> None:
            if entry in self._handlers[event]:
                self._handlers[event].remove(entry)

        return unsubscribe

    def handler_count(self, event: str | None = None) -> int:
        if event is None:
            return sum(len(v) for v in self._handlers.values())
        return len(self._handlers.get(event, ()))

    async def emit(self, event: str, **kwargs: Any) -> EventOutcome:
        outcome = EventOutcome()
        for owner, handler in list(self._handlers.get(event, ())):
            try:
                result = handler(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001 - 单个扩展失败不影响其它扩展
                outcome.data.setdefault("errors", []).append({"owner": owner, "error": str(exc)})
                continue
            if isinstance(result, dict):
                if result.get("block"):
                    outcome.blocked = True
                    outcome.block_reason = result.get("reason")
                    break
                if isinstance(result.get("data"), dict):
                    outcome.data.update(result["data"])
        return outcome

    def emit_sync(self, event: str, **kwargs: Any) -> EventOutcome:
        """同步上下文触发非阻塞事件；已经在事件循环里时转为后台任务。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.emit(event, **kwargs))
        asyncio.ensure_future(self.emit(event, **kwargs))
        return EventOutcome()


__all__ = ["EventBus", "EventOutcome", "Handler"]
