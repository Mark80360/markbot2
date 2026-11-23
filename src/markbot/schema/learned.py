"""学习层可写路径白名单：优化器门禁与记忆写入共用的唯一口径（纯规则，无内部依赖）。"""

from __future__ import annotations

import re

LEARNED_PATTERNS = [
    re.compile(r"^manifest\.json$"),
    re.compile(r"^strategy/(system_prompt|guardrails|memory)\.md$"),
    # 长期记忆的按需知识页与索引（由 memory 工具写入）
    re.compile(r"^memory/wiki/[a-z0-9][a-z0-9_\-]*\.md$"),
    re.compile(r"^memory/wiki/archive/[a-z0-9][a-z0-9._\-]*\.md$"),
    re.compile(r"^memory/wiki/index\.json$"),
    re.compile(r"^tool_notes/[a-z0-9][a-z0-9_\-]*\.md$"),
    re.compile(r"^skills/[a-z0-9][a-z0-9_\-]*/SKILL\.md$"),
    re.compile(r"^skills/[a-z0-9][a-z0-9_\-]*/(scripts|references|assets)/[A-Za-z0-9_.\-]+$"),
    re.compile(r"^sub_agents/[a-z0-9][a-z0-9_\-]*\.md$"),
]


def allowed_path(rel: str) -> bool:
    """``rel`` 是相对 ``learned/`` 的 POSIX 路径。"""
    return any(rx.match(rel) for rx in LEARNED_PATTERNS)


__all__ = ["LEARNED_PATTERNS", "allowed_path"]
