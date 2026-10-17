"""Durable record of attendance messages and their reply outcomes."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import time
from types import TracebackType


STATUSES = frozenset({"pending", "sent", "manual", "expired", "uncertain", "retryable"})


class StateStore:
    """Keep a single outcome per Slack message, identified by channel and ts.

    Successfully sent replies and replies observed from the user are immutable.
    An uncertain reply stays visible for reconciliation after a restart.
    """

    def __init__(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Create the database with private permissions before SQLite opens it.
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        path.chmod(0o600)
        self._connection = sqlite3.connect(path, timeout=30.0)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS replies (
                    channel TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN (
                            'pending', 'sent', 'manual', 'expired', 'uncertain', 'retryable'
                        )
                    ),
                    reply_ts TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (channel, ts)
                )
                """
            )

    def get_status(self, channel: str, ts: str) -> str | None:
        row = self._connection.execute(
            "SELECT status FROM replies WHERE channel = ? AND ts = ?",
            (channel, ts),
        ).fetchone()
        return str(row["status"]) if row is not None else None

    def mark(
        self,
        channel: str,
        ts: str,
        code: str,
        status: str,
        reply_ts: str | None = None,
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"Unknown reply status: {status!r}")
        now = time.time()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO replies (
                    channel, ts, code, status, reply_ts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel, ts) DO UPDATE SET
                    code = excluded.code,
                    status = excluded.status,
                    reply_ts = excluded.reply_ts,
                    updated_at = excluded.updated_at
                WHERE replies.status NOT IN ('sent', 'manual')
                """,
                (channel, ts, code, status, reply_ts, now, now),
            )

    def get_pending(self, channel: str) -> list[dict[str, str | None]]:
        rows = self._connection.execute(
            """
            SELECT ts, code, status, reply_ts FROM replies
            WHERE channel = ? AND status IN ('pending', 'uncertain', 'retryable')
            ORDER BY created_at, ts
            """,
            (channel,),
        ).fetchall()
        return [dict(row) for row in rows]

    def cleanup(self, now: float, retention_days: int = 30) -> None:
        if retention_days < 0:
            raise ValueError("retention_days must not be negative")
        cutoff = now - retention_days * 24 * 60 * 60
        with self._connection:
            self._connection.execute(
                """
                DELETE FROM replies
                WHERE status IN ('sent', 'manual', 'expired') AND updated_at < ?
                """,
                (cutoff,),
            )

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
