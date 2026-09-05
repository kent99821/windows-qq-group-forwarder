from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import IncomingMessage


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                message_key TEXT PRIMARY KEY,
                source_group TEXT NOT NULL,
                sender TEXT,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                media_path TEXT,
                status TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                sent_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """
        )
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "media_path" not in columns:
            self.connection.execute("ALTER TABLE messages ADD COLUMN media_path TEXT")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def enqueue(self, message: IncomingMessage) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO messages
            (message_key, source_group, sender, kind, content, media_path, status, observed_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (message.message_key, message.source_group, message.sender, message.kind, message.content, message.media_path, message.observed_at),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def pending(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM messages WHERE status = 'pending' ORDER BY rowid LIMIT ?", (limit,)
        ))

    def mark_attempt(self, message_key: str, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE messages SET attempts = attempts + 1, last_error = ? WHERE message_key = ?",
            (error, message_key),
        )
        self.connection.commit()

    def mark_sent(self, message_key: str) -> None:
        self.connection.execute(
            "UPDATE messages SET status = 'sent', sent_at = datetime('now'), last_error = NULL WHERE message_key = ?",
            (message_key,),
        )
        self.connection.commit()

    def discard_legacy_pending(self) -> int:
        """废弃旧 UIA 窗口扫描产生的 text 队列，不影响新的 toast_text 通知。"""
        cursor = self.connection.execute(
            """
            UPDATE messages
            SET status = 'discarded', last_error = '切换到 Windows 通知监听，旧窗口扫描记录不再发送'
            WHERE status = 'pending' AND kind = 'text'
            """
        )
        self.connection.commit()
        return cursor.rowcount

    def count(self, status: str) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM messages WHERE status = ?", (status,)).fetchone()
        return int(row["count"])

    def summary(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT status, COUNT(*) AS count FROM messages GROUP BY status"
        ).fetchall()
        summary = {"pending": 0, "sent": 0}
        for row in rows:
            summary[str(row["status"])] = int(row["count"])
        return summary
