"""上下文组装：每次请求重新合成，不做增量拼接。

按分节预算组装：``system`` / ``guardrails`` 是硬约束，永不裁剪；其余分节按
优先级整块保留或整块丢弃，只有列表型内容（技能索引）允许带标记截断。任何
省略都会在上下文里留下可见标记，禁止静默消失。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from markbot.kernel.runtime_view import RuntimeView
from markbot.memory.provider import MemoryProvider

_logger = logging.getLogger("markbot.context")

SKILL_INDEX_MAX = 8_000


@dataclass
class Section:
    name: str
    text: str
    priority: int
    hard: bool = False
    cap: int | None = None
    allow_partial: bool = False


def compose_sections(sections: list[Section], budget: int) -> str:
    fixed = [section for section in sections if section.hard]
    rest = sorted(
        (section for section in sections if not section.hard),
        key=lambda section: section.priority,
        reverse=True,
    )
    kept = [section.text.strip() for section in fixed if section.text.strip()]
    remaining = budget - sum(len(text) for text in kept)
    dropped: list[str] = []
    for section in rest:
        text = section.text.strip()
        if not text:
            continue
        if section.cap is not None:
            text = text[: section.cap]
        if len(text) <= remaining:
            kept.append(text)
            remaining -= len(text)
        elif section.allow_partial and remaining >= 50:
            kept.append(text[:remaining] + f"\n[… 已省略，完整内容见 {section.name}]")
            remaining = 0
        else:
            dropped.append(section.name)
    if dropped:
        kept.append(f"[本轮省略：{'、'.join(dropped)}]")
    return "\n\n".join(kept)


class ContextBuilder:
    def __init__(
        self,
        runtime: RuntimeView,
        *,
        budget_chars: int = 60_000,
        memory: MemoryProvider | None = None,
    ) -> None:
        self.runtime = runtime
        self.budget_chars = budget_chars
        # ``strategy.memory`` 只是启动快照；常驻块必须每轮从 learned/ 重新渲染，
        # 否则本回合写入、下回合读不到。
        learned = getattr(runtime, "learned_dir", None)
        self.memory = memory if memory is not None else (
            MemoryProvider(learned) if learned is not None else None
        )

    async def build(self, session: Any, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "system": self._compose_system(),
            "messages": session.messages_for_model(),
            "tools": tool_schemas,
        }

    def _compose_system(self) -> str:
        strategy = self.runtime.strategy
        resident = self.memory.render() if self.memory is not None else strategy.memory
        sections = [
            Section("system_prompt", strategy.system_prompt, priority=100, hard=True),
            Section("guardrails", strategy.guardrails, priority=95, hard=True),
            Section("memory", resident, priority=70),
            Section("tool_notes", strategy.tool_notes, priority=65),
            Section("prompts", "\n\n".join(strategy.prompts), priority=60),
            Section(
                "skills",
                self.runtime.skills.index_prompt(max_chars=SKILL_INDEX_MAX),
                priority=50,
                cap=SKILL_INDEX_MAX,
                allow_partial=True,
            ),
        ]
        text = compose_sections(sections, self.budget_chars)
        _logger.debug(
            "上下文组装 chars=%d budget=%d sections=%s",
            len(text),
            self.budget_chars,
            ",".join(str(len(section.text)) for section in sections),
        )
        return text


__all__ = ["ContextBuilder"]
