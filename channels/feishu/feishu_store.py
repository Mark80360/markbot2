"""飞书渠道本地存储：消息去重、会话映射、运行时授权、收件箱/发件箱（SQLite）。

从 channel.py 拆出（ADR 0005）；入口仍是 channel.py。

收件箱（ADR 0011）：收到的事件先落库再入内存队列，处理状态 ``received → processing →
done / failed / handed_off``。渠道进程崩溃重启后，未完成的事件重新入队（已经提交给服务端的
任务不重复提交，结果由服务端主动推送）。发件箱：主动通知（定时任务结果、后台任务结果、告警）
先落库，发送失败按指数退避重试，超过次数进入 ``dead``。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


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
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS chat_projects ("
            "chat_key TEXT PRIMARY KEY, project TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS inbox ("
            "key TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL, "
            "attempts INTEGER NOT NULL DEFAULT 0, task_id TEXT, error TEXT, "
            "created_at REAL NOT NULL, updated_at REAL NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS outbox ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, text TEXT NOT NULL, "
            "state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL, "
            "error TEXT, created_at REAL NOT NULL, reply_to TEXT, "
            "reply_in_thread INTEGER NOT NULL DEFAULT 0)"
        )
        self._migrate_outbox()
        self._conn.commit()

    def _migrate_outbox(self) -> None:
        """旧库补上话题回复字段：后台任务结果要回到原话题，重试时也不能丢。"""
        columns = {
            str(row[1]) for row in self._conn.execute("PRAGMA table_info(outbox)").fetchall()
        }
        if "reply_to" not in columns:
            self._conn.execute("ALTER TABLE outbox ADD COLUMN reply_to TEXT")
        if "reply_in_thread" not in columns:
            self._conn.execute(
                "ALTER TABLE outbox ADD COLUMN reply_in_thread INTEGER NOT NULL DEFAULT 0"
            )

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

    # ------------------------------------------------------------ 项目绑定（ADR 0013）
    def get_project(self, chat_key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT project FROM chat_projects WHERE chat_key = ?", (chat_key,)
            ).fetchone()
        return row[0] if row else None

    def set_project(self, chat_key: str, project: str | None) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self._lock:
            if project:
                self._conn.execute(
                    "INSERT INTO chat_projects(chat_key, project, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(chat_key) DO UPDATE SET project = excluded.project, "
                    "updated_at = excluded.updated_at",
                    (chat_key, project, now),
                )
            else:
                self._conn.execute("DELETE FROM chat_projects WHERE chat_key = ?", (chat_key,))
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

    # ------------------------------------------------------------ 收件箱
    def inbox_add(self, key: str, payload: str) -> bool:
        """登记收到的事件；已存在（重复投递）返回 False。"""
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO inbox(key, payload, state, attempts, created_at, updated_at) "
                "VALUES (?, ?, 'received', 0, ?, ?)",
                (key, payload, now, now),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def inbox_mark(self, key: str, state: str, error: str | None = None) -> None:
        bump = 1 if state == "processing" else 0
        with self._lock:
            self._conn.execute(
                "UPDATE inbox SET state = ?, error = ?, attempts = attempts + ?, updated_at = ? "
                "WHERE key = ?",
                (state, error, bump, time.time(), key),
            )
            self._conn.commit()

    def inbox_set_task(self, key: str, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE inbox SET task_id = ?, updated_at = ? WHERE key = ?",
                (task_id, time.time(), key),
            )
            self._conn.commit()

    def inbox_get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT key, payload, state, attempts, task_id, error FROM inbox WHERE key = ?",
                (key,),
            ).fetchone()
        return _inbox_row(row) if row else None

    def inbox_pending(self, max_age_s: float = 86400) -> list[dict[str, Any]]:
        """未处理完的事件（received / processing），最旧的在前。"""
        cutoff = time.time() - max_age_s
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, payload, state, attempts, task_id, error FROM inbox "
                "WHERE state IN ('received', 'processing') AND created_at >= ? ORDER BY created_at",
                (cutoff,),
            ).fetchall()
        return [_inbox_row(row) for row in rows]

    # ------------------------------------------------------------ 发件箱
    def outbox_add(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to: str | None = None,
        reply_in_thread: bool = False,
        lease_s: float = 60,
    ) -> int:
        """登记一条待发送的消息，状态 sending（带租约：调用方马上尝试发送）。"""
        now = time.time()
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO outbox(chat_id, text, state, attempts, next_at, created_at, "
                "reply_to, reply_in_thread) VALUES (?, ?, 'sending', 0, ?, ?, ?, ?)",
                (chat_id, text, now + lease_s, now, reply_to, int(reply_in_thread)),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def outbox_due(self, *, limit: int = 20, lease_s: float = 60) -> list[dict[str, Any]]:
        """取出到期的消息并续租（标记 sending），避免并发重复发送。"""
        now = time.time()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, chat_id, text, attempts, reply_to, reply_in_thread FROM outbox "
                "WHERE state IN ('pending', 'sending') AND next_at <= ? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE outbox SET state = 'sending', next_at = ? WHERE id = ?",
                    (now + lease_s, row[0]),
                )
            self._conn.commit()
        return [
            {
                "id": row[0],
                "chat_id": row[1],
                "text": row[2],
                "attempts": row[3],
                "reply_to": row[4] or None,
                "reply_in_thread": bool(row[5]),
            }
            for row in rows
        ]

    def outbox_result(
        self, item_id: int, ok: bool, *, error: str = "", max_attempts: int = 8,
        base_backoff_s: float = 5.0,
    ) -> str:
        """记录一次发送结果；返回新状态（sent / pending / dead）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM outbox WHERE id = ?", (item_id,)
            ).fetchone()
            attempts = (int(row[0]) if row else 0) + 1
            if ok:
                state, next_at = "sent", time.time()
            elif attempts >= max_attempts:
                state, next_at = "dead", time.time()
            else:
                state = "pending"
                next_at = time.time() + min(1800.0, base_backoff_s * (2 ** (attempts - 1)))
            self._conn.execute(
                "UPDATE outbox SET state = ?, attempts = ?, next_at = ?, error = ? WHERE id = ?",
                (state, attempts, next_at, error[:500] or None, item_id),
            )
            self._conn.commit()
        return state

    def outbox_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT state, COUNT(*) FROM outbox GROUP BY state"
            ).fetchall()
        return {str(state): int(count) for state, count in rows}

    def prune_seen(self, days: int = 7) -> None:
        cutoff = (datetime.now().astimezone() - timedelta(days=days)).isoformat(
            timespec="seconds"
        )
        old = time.time() - days * 86400
        with self._lock:
            self._conn.execute("DELETE FROM seen WHERE seen_at < ?", (cutoff,))
            self._conn.execute(
                "DELETE FROM inbox WHERE updated_at < ? AND state NOT IN ('received', 'processing')",
                (old,),
            )
            self._conn.execute(
                "DELETE FROM outbox WHERE created_at < ? AND state IN ('sent', 'dead')", (old,)
            )
            self._conn.commit()


def _inbox_row(row: Any) -> dict[str, Any]:
    return {
        "key": row[0],
        "payload": row[1],
        "state": row[2],
        "attempts": int(row[3] or 0),
        "task_id": row[4],
        "error": row[5],
    }
