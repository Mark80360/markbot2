"""飞书渠道本地存储：消息去重、会话映射、运行时授权（SQLite）。

从 channel.py 拆出（ADR 0005）；入口仍是 channel.py。
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path


class Store:
    """消息去重与会话映射。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, seen_at TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "chat_key TEXT PRIMARY KEY, session_id TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS grants ("
            "chat_key TEXT NOT NULL, program TEXT NOT NULL, created_at TEXT NOT NULL, "
            "PRIMARY KEY (chat_key, program))"
        )
        self._conn.commit()

    def first_seen(self, key: str) -> bool:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO seen(key, seen_at) VALUES (?, ?)", (key, now)
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def get_session(self, chat_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT session_id FROM sessions WHERE chat_key = ?", (chat_key,)
            ).fetchone()
        return row[0] if row else None

    def set_session(self, chat_key: str, session_id: str) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(chat_key, session_id, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(chat_key) DO UPDATE SET session_id = excluded.session_id, "
                "updated_at = excluded.updated_at",
                (chat_key, session_id, now),
            )
            self._conn.commit()

    def clear_session(self, chat_key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sessions WHERE chat_key = ?", (chat_key,))
            self._conn.commit()

    def add_grants(self, chat_key: str, programs: list[str]) -> list[str]:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        added: list[str] = []
        with self._lock:
            for program in programs:
                name = program.strip().lower()
                if not name:
                    continue
                cursor = self._conn.execute(
                    "INSERT OR IGNORE INTO grants(chat_key, program, created_at) VALUES (?, ?, ?)",
                    (chat_key, name, now),
                )
                if cursor.rowcount == 1:
                    added.append(name)
            self._conn.commit()
        return added

    def list_grants(self, chat_key: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT program FROM grants WHERE chat_key = ? ORDER BY program", (chat_key,)
            ).fetchall()
        return [row[0] for row in rows]

    def remove_grants(self, chat_key: str, programs: list[str] | None = None) -> None:
        with self._lock:
            if programs:
                for program in programs:
                    self._conn.execute(
                        "DELETE FROM grants WHERE chat_key = ? AND program = ?",
                        (chat_key, program.strip().lower()),
                    )
            else:
                self._conn.execute("DELETE FROM grants WHERE chat_key = ?", (chat_key,))
            self._conn.commit()

    def prune_seen(self, days: int = 7) -> None:
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        with self._lock:
            self._conn.execute("DELETE FROM seen WHERE seen_at < ?", (cutoff,))
            self._conn.commit()
