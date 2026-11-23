"""上下文组装：每次请求重新合成，不做增量拼接。

按分节预算组装：``system`` / ``guardrails`` 是硬约束，永不裁剪；其余分节按
优先级整块保留或整块丢弃，只有列表型内容（技能索引）允许带标记截断。任何
省略都会在上下文里留下可见标记，禁止静默消失。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from markbot.kernel.runtime_view import RuntimeView

_logger = logging.getLogger("markbot.context")

SKILL_INDEX_MAX = 8_000

# 扩展通过 ``api.append_entry(CONTEXT_PROVIDERS, factory)`` 注册常驻上下文来源；
# ``factory(learned_dir)`` 返回带 ``render()`` 的对象或 ``None``。Kernel 不直接依赖
# 记忆实现（markbot.memory），记忆只是扩展车道上的一个提供者。
CONTEXT_PROVIDERS = "context_providers"


@runtime_checkable
class ContextProvider(Protocol):
    def render(self) -> str: ...


ContextProviderFactory = Callable[[Path], "ContextProvider | None"]


def providers_from_runtime(runtime: Any) -> list[ContextProvider]:
    learned = getattr(runtime, "learned_dir", None)
    if learned is None:
        return []
    out: list[ContextProvider] = []
    for factory in (getattr(runtime, "entries", None) or {}).get(CONTEXT_PROVIDERS, []):
        try:
            provider = factory(Path(learned))
        except Exception as exc:  # noqa: BLE001 - 单个提供者失败不影响组装
            _logger.warning("上下文提供者初始化失败: %s", exc)
            continue
        if provider is not None:
            out.append(provider)
    return out


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
        memory: ContextProvider | None = None,
    ) -> None:
        self.runtime = runtime
        self.budget_chars = budget_chars
        # ``strategy.memory`` 只是启动快照；常驻块必须每轮从 learned/ 重新渲染，
        # 否则本回合写入、下回合读不到。未显式传入时取扩展注册的第一个提供者。
        if memory is None:
            found = providers_from_runtime(runtime)
            memory = found[0] if found else None
        self.memory = memory

    async def build(self, session: Any, tool_schemas: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "system": self._compose_system(session),
            "messages": session.messages_for_model(),
            "tools": tool_schemas,
        }

    def _active_skills(self, session: Any) -> str:
        """渲染本轮 ``$skill`` 显式指定的技能正文，调用结束后不留在会话里。"""
        blocks: list[str] = []
        for name in session.metadata.get("_active_skills") or []:
            try:
                meta, body = self.runtime.skills.read_body(str(name))
            except KeyError:
                continue
            blocks.append(f"### {meta.name}\n{body}")
        if not blocks:
            return ""
        text = "用户显式指定的技能（按正文执行）：\n\n" + "\n\n".join(blocks)
        if len(text) > 20_000:
            text = text[:20_000] + "\n[… 技能正文过长，已截断]"
        return text

    def _compose_system(self, session: Any) -> str:
        strategy = self.runtime.strategy
        resident = self.memory.render() if self.memory is not None else strategy.memory
        sections = [
            Section("system_prompt", strategy.system_prompt, priority=100, hard=True),
            Section("guardrails", strategy.guardrails, priority=95, hard=True),
            Section(
                "active_skills",
                self._active_skills(session),
                priority=85,
            ),
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


__all__ = [
    "CONTEXT_PROVIDERS",
    "ContextBuilder",
    "ContextProvider",
    "ContextProviderFactory",
    "providers_from_runtime",
]
