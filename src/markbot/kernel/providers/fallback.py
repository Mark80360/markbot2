"""主备模型切换与熔断（ADR 0010）。

单一模型网关挂掉时整个 Agent 不可用。``FallbackProvider`` 按顺序尝试主模型与备用模型：

- 某个模型在自身重试耗尽后仍失败（网络、5xx、429、鉴权失败、请求被拒），换下一个；
- 连续失败 ``failure_threshold`` 次的模型熔断 ``cooldown_s`` 秒，期间直接跳过
  （全部熔断时仍按顺序尝试，不会无模型可用）；
- 流式已经向用户输出过文本后中断：不能换模型重放（会重复输出），直接报错；
- 全部模型都以内容审核拒绝失败时，抛出原始 :class:`ContentRejectedError`，
  让 Agent Loop 的「定位并移除触发内容」恢复逻辑照常工作；
- 内容审核探测（``AgentLoop._probe``）只用主模型：用备用模型探测会把「主模型拒绝的内容」
  误判为可以接受。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from markbot.kernel.providers.base import (
    ContentRejectedError,
    DeltaCallback,
    ModelReply,
    Provider,
    ProviderError,
    describe_error,
)

logger = logging.getLogger("markbot.provider")


class FallbackProvider:
    def __init__(
        self,
        providers: list[tuple[str, Provider]],
        *,
        failure_threshold: int = 3,
        cooldown_s: float = 300.0,
    ) -> None:
        if not providers:
            raise ValueError("FallbackProvider 至少需要一个模型")
        self._providers = list(providers)
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_s = max(0.0, float(cooldown_s))
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self.last_used: str = providers[0][0]
        self.name = getattr(providers[0][1], "name", "fallback")
        # 熔断打开时的回调 (label, 连续失败次数)：serve 用它向聊天发告警（ADR 0011）
        self.on_circuit_open: Any = None

    @property
    def primary(self) -> Provider:
        return self._providers[0][1]

    @property
    def labels(self) -> list[str]:
        return [label for label, _ in self._providers]

    def _order(self) -> list[tuple[str, Provider]]:
        now = time.monotonic()
        with self._lock:
            closed = [item for item in self._providers if self._open_until.get(item[0], 0) <= now]
        return closed or list(self._providers)

    def _record(self, label: str, ok: bool) -> None:
        opened = False
        with self._lock:
            if ok:
                self._failures.pop(label, None)
                self._open_until.pop(label, None)
                return
            count = self._failures.get(label, 0) + 1
            self._failures[label] = count
            if count >= self.failure_threshold and self.cooldown_s > 0:
                opened = self._open_until.get(label, 0) <= time.monotonic()
                self._open_until[label] = time.monotonic() + self.cooldown_s
                logger.warning(
                    "模型熔断 %s：连续失败 %d 次，%ds 内跳过", label, count, int(self.cooldown_s)
                )
        if opened and callable(self.on_circuit_open):
            try:
                self.on_circuit_open(label, count)
            except Exception:  # noqa: BLE001 - 告警失败不影响模型调用
                logger.exception("熔断告警失败")

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "model": label,
                    "failures": self._failures.get(label, 0),
                    "open_for_s": max(0, round(self._open_until.get(label, 0) - now)),
                }
                for label, _ in self._providers
            ]

    async def complete(
        self, payload: dict[str, Any], on_delta: DeltaCallback | None = None
    ) -> ModelReply:
        errors: list[tuple[str, BaseException]] = []
        order = self._order()
        for index, (label, provider) in enumerate(order):
            emitted = False

            def tracking(chunk: str, _cb: Any = on_delta) -> None:
                nonlocal emitted
                if chunk:
                    emitted = True
                _cb(chunk)

            try:
                if on_delta is None:
                    reply = await provider.complete(payload)
                else:
                    reply = await provider.complete(payload, on_delta=tracking)
            except ProviderError as exc:
                self._record(label, False)
                errors.append((label, exc))
                if emitted:
                    raise
                if index + 1 < len(order):
                    logger.warning(
                        "模型 %s 调用失败，切换到 %s: %s",
                        label, order[index + 1][0], describe_error(exc)[:200],
                    )
                continue
            self._record(label, True)
            if label != self._providers[0][0]:
                logger.warning("本次请求由备用模型 %s 完成", label)
            self.last_used = label
            return reply
        if errors and all(isinstance(exc, ContentRejectedError) for _, exc in errors):
            raise errors[0][1]
        detail = "；".join(f"{label}: {describe_error(exc)[:200]}" for label, exc in errors)
        raise ProviderError(f"所有模型都调用失败（{detail}）")


__all__ = ["FallbackProvider"]
