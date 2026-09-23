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
                history_key TEXT,
                sent_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS message_deliveries (
                message_key TEXT NOT NULL,
                bot_id TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                sent_at TEXT,
                PRIMARY KEY (message_key, bot_id)
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS source_watermarks (
                source_group TEXT PRIMARY KEY,
                history_key TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            str(row[1]) for row in self.connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "media_path" not in columns:
            self.connection.execute("ALTER TABLE messages ADD COLUMN media_path TEXT")
        if "history_key" not in columns:
            self.connection.execute("ALTER TABLE messages ADD COLUMN history_key TEXT")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def enqueue(self, message: IncomingMessage, bot_ids: tuple[str, ...] | list[str] = ()) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO messages
            (message_key, source_group, sender, kind, content, media_path, status, observed_at, history_key)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (message.message_key, message.source_group, message.sender, message.kind, message.content, message.media_path, message.observed_at, message.history_key),
        )
        if cursor.rowcount == 1 and bot_ids:
            self._ensure_deliveries(message.message_key, bot_ids, default_status="pending")
        self.connection.commit()
        return cursor.rowcount == 1

    def enqueue_failed(
        self,
        message: IncomingMessage,
        error: str,
        bot_ids: tuple[str, ...] | list[str] = (),
    ) -> bool:
        """Persist an unsendable message directly in the failed queue."""
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO messages
            (message_key, source_group, sender, kind, content, media_path, status, observed_at, history_key, last_error)
            VALUES (?, ?, ?, ?, ?, ?, 'failed', ?, ?, ?)
            """,
            (
                message.message_key,
                message.source_group,
                message.sender,
                message.kind,
                message.content,
                message.media_path,
                message.observed_at,
                message.history_key,
                error[:1000],
            ),
        )
        if cursor.rowcount == 1 and bot_ids:
            self._ensure_deliveries(message.message_key, bot_ids, default_status="failed", error=error)
        self.connection.commit()
        return cursor.rowcount == 1

    def pending(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM messages WHERE status = 'pending' ORDER BY rowid LIMIT ?", (limit,)
        ))

    def _ensure_deliveries(
        self,
        message_key: str,
        bot_ids: tuple[str, ...] | list[str],
        *,
        default_status: str,
        error: str | None = None,
    ) -> None:
        for bot_id in dict.fromkeys(str(item).strip() for item in bot_ids if str(item).strip()):
            self.connection.execute(
                """
                INSERT OR IGNORE INTO message_deliveries
                (message_key, bot_id, status, last_error)
                VALUES (?, ?, ?, ?)
                """,
                (message_key, bot_id, default_status, error[:1000] if error else None),
            )

    def ensure_deliveries(self, bot_ids: tuple[str, ...] | list[str]) -> None:
        """Backfill delivery rows for messages created by the single-bot version."""
        normalized = tuple(dict.fromkeys(str(item).strip() for item in bot_ids if str(item).strip()))
        if not normalized:
            return
        rows = self.connection.execute("SELECT message_key, status, last_error FROM messages").fetchall()
        for row in rows:
            status = str(row["status"])
            delivery_status = status if status in {"pending", "sent", "failed", "discarded"} else "pending"
            self._ensure_deliveries(
                str(row["message_key"]),
                normalized,
                default_status=delivery_status,
                error=str(row["last_error"]) if row["last_error"] else None,
            )
        self.connection.commit()

    def pending_deliveries(self, message_key: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM message_deliveries WHERE message_key = ? AND status = 'pending' ORDER BY rowid",
            (message_key,),
        ))

    def deliveries(self, message_key: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM message_deliveries WHERE message_key = ? ORDER BY rowid",
            (message_key,),
        ))

    def mark_delivery_attempt(self, message_key: str, bot_id: str, error: str | None = None) -> None:
        self.connection.execute(
            "UPDATE message_deliveries SET attempts = attempts + 1, last_error = ? WHERE message_key = ? AND bot_id = ?",
            (error[:1000] if error else None, message_key, bot_id),
        )
        self.connection.execute(
            """
            UPDATE messages
            SET attempts = (SELECT MAX(attempts) FROM message_deliveries WHERE message_key = ?),
                last_error = ?
            WHERE message_key = ?
            """,
            (message_key, error[:1000] if error else None, message_key),
        )
        self.connection.commit()

    def mark_delivery_sent(self, message_key: str, bot_id: str) -> None:
        self.connection.execute(
            "UPDATE message_deliveries SET status = 'sent', sent_at = datetime('now'), last_error = NULL WHERE message_key = ? AND bot_id = ?",
            (message_key, bot_id),
        )
        self._refresh_message_status(message_key)
        self.connection.commit()

    def mark_delivery_failed(self, message_key: str, bot_id: str, error: str) -> None:
        self.connection.execute(
            "UPDATE message_deliveries SET status = 'failed', last_error = ? WHERE message_key = ? AND bot_id = ?",
            (error[:1000], message_key, bot_id),
        )
        self._refresh_message_status(message_key)
        self.connection.commit()

    def _refresh_message_status(self, message_key: str) -> None:
        rows = self.deliveries(message_key)
        if not rows:
            return
        statuses = {str(row["status"]) for row in rows}
        if statuses == {"sent"}:
            status = "sent"
        elif "pending" in statuses or "sending" in statuses:
            status = "pending"
        elif "failed" in statuses:
            status = "failed"
        else:
            status = "discarded"
        last_error = next((str(row["last_error"]) for row in rows if row["last_error"]), None)
        self.connection.execute(
            "UPDATE messages SET status = ?, last_error = ? WHERE message_key = ?",
            (status, last_error, message_key),
        )

    def message_fully_sent(self, message_key: str) -> bool:
        rows = self.deliveries(message_key)
        return bool(rows) and all(str(row["status"]) == "sent" for row in rows)

    def history_watermark(self, source_group: str) -> str | None:
        """Return the last history row fully delivered for one source.

        History reconciliation runs in a worker thread because UI Automation is
        blocking.  Use a short-lived read connection here instead of sharing
        the StateStore connection across threads.
        """
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT history_key FROM source_watermarks WHERE source_group = ?",
                (source_group,),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def set_history_watermark(self, source_group: str, history_key: str) -> None:
        if not source_group.strip() or not history_key.strip():
            return
        self.connection.execute(
            """
            INSERT INTO source_watermarks (source_group, history_key, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_group) DO UPDATE SET
                history_key = excluded.history_key,
                updated_at = CURRENT_TIMESTAMP
            """,
            (source_group, history_key),
        )
        self.connection.commit()

    def history_message_status(self, history_key: str) -> str | None:
        """Return the persisted delivery status for a history row, if known."""
        if not history_key.strip():
            return None
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                """
                SELECT status
                FROM messages
                WHERE history_key = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (history_key,),
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def advance_history_watermark(self, message_key: str) -> bool:
        """Advance a source watermark without skipping an earlier unsent row."""
        target = self.connection.execute(
            """
            SELECT rowid, source_group, history_key, status
            FROM messages
            WHERE message_key = ?
            """,
            (message_key,),
        ).fetchone()
        if target is None or not target["history_key"] or target["status"] != "sent":
            return False

        source_group = str(target["source_group"])
        target_rowid = int(target["rowid"])
        blocked = self.connection.execute(
            """
            SELECT 1
            FROM messages
            WHERE source_group = ?
              AND history_key IS NOT NULL
              AND rowid <= ?
              AND status != 'sent'
            LIMIT 1
            """,
            (source_group, target_rowid),
        ).fetchone()
        if blocked is not None:
            return False

        current = self.connection.execute(
            """
            SELECT m.rowid
            FROM source_watermarks AS w
            JOIN messages AS m
              ON m.source_group = w.source_group
             AND m.history_key = w.history_key
            WHERE w.source_group = ?
            ORDER BY m.rowid DESC
            LIMIT 1
            """,
            (source_group,),
        ).fetchone()
        if current is not None and int(current[0]) >= target_rowid:
            return False

        latest_history_key = str(target["history_key"])
        for row in self.connection.execute(
            """
            SELECT history_key, status
            FROM messages
            WHERE source_group = ?
              AND history_key IS NOT NULL
              AND rowid >= ?
            ORDER BY rowid
            """,
            (source_group, target_rowid),
        ):
            if row["status"] != "sent":
                break
            latest_history_key = str(row["history_key"])

        self.connection.execute(
            """
            INSERT INTO source_watermarks (source_group, history_key, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source_group) DO UPDATE SET
                history_key = excluded.history_key,
                updated_at = CURRENT_TIMESTAMP
            """,
            (source_group, latest_history_key),
        )
        self.connection.commit()
        return True

    def failed(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM messages WHERE status = 'failed' ORDER BY rowid DESC LIMIT ?", (limit,)
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
        self.connection.execute(
            "UPDATE message_deliveries SET status = 'sent', sent_at = datetime('now'), last_error = NULL WHERE message_key = ?",
            (message_key,),
        )
        self.connection.commit()

    def mark_failed(self, message_key: str, error: str) -> None:
        self.connection.execute(
            "UPDATE messages SET status = 'failed', last_error = ? WHERE message_key = ?",
            (error[:1000], message_key),
        )
        self.connection.execute(
            "UPDATE message_deliveries SET status = 'failed', last_error = ? WHERE message_key = ? AND status != 'sent'",
            (error[:1000], message_key),
        )
        self.connection.commit()

    def retry_failed(self, message_keys: list[str] | tuple[str, ...] | None = None) -> int:
        if message_keys is None:
            rows = self.connection.execute(
                "SELECT message_key FROM messages WHERE status = 'failed'"
            ).fetchall()
            normalized = tuple(str(row["message_key"]) for row in rows)
        else:
            normalized = tuple(dict.fromkeys(str(key) for key in message_keys if str(key)))
            if not normalized:
                return 0
            placeholders = ",".join("?" for _ in normalized)
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        self.connection.execute(
            f"UPDATE messages SET status = 'pending', attempts = 0, last_error = NULL "
            f"WHERE status = 'failed' AND message_key IN ({placeholders})",
            normalized,
        )
        cursor = self.connection.execute(
            f"UPDATE message_deliveries SET status = 'pending', attempts = 0, last_error = NULL "
            f"WHERE status = 'failed' AND message_key IN ({placeholders})",
            normalized,
        )
        self.connection.commit()
        return len(normalized)

    def discard_legacy_pending(self) -> int:
        """废弃旧 UIA 窗口扫描产生的 text 队列，不影响新的 toast_text 通知。"""
        cursor = self.connection.execute(
            """
            UPDATE messages
            SET status = 'discarded', last_error = '切换到 Windows 通知监听，旧窗口扫描记录不再发送'
            WHERE status = 'pending' AND kind = 'text'
            """
        )
        self.connection.execute(
            """
            UPDATE message_deliveries
            SET status = 'discarded', last_error = '切换到 Windows 通知监听，旧窗口扫描记录不再发送'
            WHERE message_key IN (
                SELECT message_key FROM messages WHERE status = 'discarded'
            )
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
        summary = {"pending": 0, "sent": 0, "failed": 0, "discarded": 0}
        for row in rows:
            summary[str(row["status"])] = int(row["count"])
        return summary
