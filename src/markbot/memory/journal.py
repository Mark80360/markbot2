"""跨会话历史检索：直接扫描内核归档，不复制第二份事实。

事实来源是 ``~/.markbot/sessions/<id>/events.jsonl``，经 ``schema.session_log.SessionLog``
投影成「轨迹」与「压缩段」两个视图（旧归档没有事件日志时回退读 ``trajectory.jsonl`` /
``compacted.jsonl``）。检索只返回 ``session_id + 视图名 + 序号 + 片段``。

整理游标放在运行时私有目录 ``~/.markbot/memory/cursor.json``，不进入
``learned/``，避免污染 ``tree_hash`` 与晋升评估。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from markbot.api.redaction import redact_secrets
from markbot.schema.session_log import SessionLog

_logger = logging.getLogger("markbot.memory")

CURSOR_REL = "memory/cursor.json"
SNIPPET_CHARS = 240
EXCERPT_CHARS = 3000


@dataclass(frozen=True)
class JournalHit:
    session_id: str
    source: str  # trajectory | compacted
    line: int
    text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "source": self.source,
            "line": self.line,
            "text": self.text,
        }


@dataclass
class SessionBatch:
    """一个待整理的会话及其已归档规模。"""

    session_id: str
    messages: int
    compactions: int
    ended: bool = False
    page: str = "trajectory.jsonl"


@dataclass
class Cursor:
    consumed: dict[str, dict[str, int]] = field(default_factory=dict)
    last_run: str = ""

    def advance(self, batches: Iterable[SessionBatch]) -> None:
        for batch in batches:
            self.consumed[batch.session_id] = {
                "messages": batch.messages,
                "compactions": batch.compactions,
            }


def cursor_path(home: Path) -> Path:
    return Path(home) / CURSOR_REL


def load_cursor(home: Path) -> Cursor:
    path = cursor_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Cursor()
    except (OSError, ValueError) as exc:
        _logger.warning("记忆游标不可读，按空游标处理 path=%s error=%s", path, exc)
        return Cursor()
    if not isinstance(data, dict):
        _logger.warning("记忆游标格式异常，按空游标处理 path=%s", path)
        return Cursor()
    consumed = data.get("consumed")
    clean: dict[str, dict[str, int]] = {}
    if isinstance(consumed, dict):
        for key, value in consumed.items():
            if not isinstance(value, dict):
                continue
            clean[str(key)] = {
                "messages": _count(value.get("messages")),
                "compactions": _count(value.get("compactions")),
            }
    return Cursor(consumed=clean, last_run=str(data.get("last_run") or ""))


def _count(value: Any) -> int:
    """游标里损坏的计数按 0 处理：宁可多整理一次，也不要卡死整个流程。"""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def save_cursor(home: Path, cursor: Cursor) -> None:
    path = cursor_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "consumed": cursor.consumed,
        "last_run": cursor.last_run,
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class Journal:
    def __init__(self, home: Path) -> None:
        self.home = Path(home)
        self.root = self.home / "sessions"

    # ------------------------------------------------------------------ scan
    def search(self, query: str, *, limit: int = 5) -> list[JournalHit]:
        terms = [term.lower() for term in str(query or "").split() if term]
        if not terms:
            return []
        hits: list[JournalHit] = []
        for directory in self._session_dirs():
            for source, rows in self._views(directory):
                for number, row in rows:
                    text = _row_text(row)
                    if not text or not any(term in text.lower() for term in terms):
                        continue
                    hits.append(
                        JournalHit(
                            session_id=directory.name,
                            source=source,
                            line=number,
                            text=_snippet(text),
                        )
                    )
                    if len(hits) >= limit:
                        return hits
        return hits

    def scan_since(self, cursor: Cursor, *, max_sessions: int = 20) -> list[SessionBatch]:
        """返回相对游标有增量的会话；只有整批成功才应推进游标。"""
        batches: list[SessionBatch] = []
        for directory in self._session_dirs():
            counts = self._counts(directory)
            if counts is None:
                continue
            messages, compactions = counts
            if messages <= 0:
                continue
            seen = cursor.consumed.get(directory.name) or {}
            if (
                messages <= int(seen.get("messages") or 0)
                and compactions <= int(seen.get("compactions") or 0)
            ):
                continue
            batches.append(
                SessionBatch(
                    session_id=directory.name,
                    messages=messages,
                    compactions=compactions,
                    ended=_ended(directory),
                )
            )
            if len(batches) >= max_sessions:
                break
        return batches

    def excerpt(self, batch: SessionBatch, *, chars: int = EXCERPT_CHARS) -> str:
        """为整理器抽取会话文本（有上限，避免把整段历史塞进提示）。"""
        directory = self.root / batch.session_id
        lines: list[str] = []
        used = 0
        # 优先 compacted：其中是被压缩掉的原文，信息密度更高
        for source, rows in reversed(self._views(directory)):
            for _, row in rows:
                text = redact_secrets(_row_text(row))
                if not text:
                    continue
                role = str(row.get("role") or source)
                line = f"[{role}] {text}"
                if used + len(line) > chars:
                    lines.append("…（已截断）")
                    return "\n".join(lines)
                lines.append(line)
                used += len(line)
        return "\n".join(lines)

    # --------------------------------------------------------------- helpers
    def _session_dirs(self) -> list[Path]:
        if not self.root.exists():
            return []
        dirs = [path for path in self.root.iterdir() if path.is_dir()]
        dirs.sort(key=_mtime, reverse=True)
        return dirs

    def _views(self, directory: Path) -> list[tuple[str, list[tuple[int, dict[str, Any]]]]]:
        """``[("trajectory", rows), ("compacted", rows)]``；序号从 1 开始，空视图省略。"""
        log = SessionLog(directory)
        views: list[tuple[str, list[tuple[int, dict[str, Any]]]]] = []
        for source, rows in (("trajectory", log.trajectory()), ("compacted", log.compacted())):
            if rows:
                views.append((source, list(enumerate(rows, start=1))))
        return views

    def _counts(self, directory: Path) -> tuple[int, int] | None:
        # 只整理已经归档过的会话：长驻会话的 events.jsonl 实时追加，没归档前不进入整理，
        # 与旧行为一致。归档的标志是 meta.json；旧归档只有 trajectory.jsonl（也是归档时写的）
        if not ((directory / "meta.json").is_file() or (directory / "trajectory.jsonl").is_file()):
            return None
        log = SessionLog(directory)
        messages = len(log.trajectory())
        if messages <= 0:
            return None
        return messages, len(log.compacted())


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _ended(directory: Path) -> bool:
    try:
        data = json.loads((directory / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("ended_at"))


def _rows(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        _logger.warning("会话归档不可读 path=%s error=%s", path.name, exc)
        return
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield number, row


def _row_count(path: Path) -> int:
    return sum(1 for _ in _rows(path))


def _row_text(row: dict[str, Any]) -> str:
    content = row.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    calls = row.get("tool_calls")
    if isinstance(calls, list):
        parts = [
            f"{call.get('name')}({json.dumps(call.get('arguments') or {}, ensure_ascii=False)})"
            for call in calls
            if isinstance(call, dict)
        ]
        return " ".join(parts).strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        ]
        return " ".join(part for part in parts if part).strip()
    nested = row.get("messages")
    if isinstance(nested, list):
        return " ".join(
            _row_text(item) for item in nested if isinstance(item, dict)
        ).strip()
    return ""


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS] + "…"


__all__ = [
    "CURSOR_REL",
    "Cursor",
    "Journal",
    "JournalHit",
    "SessionBatch",
    "cursor_path",
    "load_cursor",
    "save_cursor",
]
