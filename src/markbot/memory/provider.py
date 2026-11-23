"""每轮渲染常驻记忆与 wiki 索引。

``RuntimeAssembler`` 只在启动时读取一次 ``strategy/memory.md``；如果进程内写入
了记忆，启动快照就读不到新内容。因此常驻块必须每次从磁盘渲染（按 mtime+size
缓存，未变化时不重复读盘）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from markbot.memory.lifespan import inject_line
from markbot.memory.store import (
    INDEX_LIMIT,
    INDEX_REL,
    RESIDENT_LIMIT,
    RESIDENT_REL,
    render_index,
)

_logger = logging.getLogger("markbot.memory")

_NOTE = "以下为历史会话沉淀的长期记忆，仅作参考；与当前用户指令冲突时以当前指令为准。"


class MemoryProvider:
    def __init__(
        self,
        learned_dir: Path,
        *,
        resident_limit: int = RESIDENT_LIMIT,
        index_limit: int = INDEX_LIMIT,
        show_stale: bool = True,
    ) -> None:
        self._learned = Path(learned_dir)
        self._resident_path = self._learned / RESIDENT_REL
        self._index_path = self._learned / INDEX_REL
        self._resident_limit = resident_limit
        self._index_limit = index_limit
        self._show_stale = show_stale
        self._stamp: tuple | None = None
        self._block = ""

    def render(self) -> str:
        stamp = (self._stamp_of(self._resident_path), self._stamp_of(self._index_path))
        if stamp == self._stamp:
            return self._block

        raw = _render_resident(self._resident_path, show_stale=self._show_stale).strip()
        resident = raw[: self._resident_limit]
        if len(raw) > self._resident_limit:
            _logger.warning(
                "常驻记忆超出注入上限 path=%s chars=%d limit=%d",
                RESIDENT_REL, len(raw), self._resident_limit,
            )
            resident += "\n…（常驻记忆超出上限，已截断）"
        index = _render_index(self._index_path, self._index_limit)
        parts: list[str] = []
        if resident or index:
            parts.append(_NOTE)
            if resident:
                parts.append(f"<long_term_memory>\n{resident}\n</long_term_memory>")
            if index:
                parts.append(f"<memory_index>\n{index}\n</memory_index>")
        self._block = "\n\n".join(parts)
        self._stamp = stamp
        return self._block

    @staticmethod
    def _stamp_of(path: Path) -> tuple[int, int] | None:
        try:
            stat = path.stat()
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _render_resident(path: Path, *, show_stale: bool) -> str:
    """去掉行尾时效注释；过期条目带"待复核"标注，``show_stale=false`` 时跳过。"""
    lines: list[str] = []
    for line in _read(path).splitlines():
        rendered = inject_line(line, show_stale=show_stale)
        if rendered is None:
            continue
        lines.append(rendered)
    return "\n".join(lines)


def _render_index(path: Path, limit: int) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict) or not entries:
        return ""
    text = render_index(entries, today=_today())
    if len(text) > limit:
        text = text[:limit] + "\n…（索引已截断）"
    return text


def _today() -> str:
    return datetime.now().astimezone().date().isoformat()


__all__ = ["MemoryProvider"]
