"""SQLite store: checkpoint + processing logs.

Single source of truth for resume. Thread-safe enough for one scheduled run at
a time (the agent is meant to run as a single process per mailbox).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .schemas import Category, LogEntry


SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_email_id TEXT NOT NULL,
    last_timestamp TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logs (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT NOT NULL,
    subject TEXT,
    sender TEXT,
    received_at TEXT,
    category TEXT,
    action TEXT,
    status TEXT,
    error TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_email ON logs(email_id);
"""


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- checkpoint ----
    def get_checkpoint(self) -> Optional[tuple[str, str]]:
        cur = self.conn.execute(
            "SELECT last_email_id, last_timestamp FROM checkpoint WHERE id=1"
        )
        row = cur.fetchone()
        return (row[0], row[1]) if row else None

    def save_checkpoint(self, email_id: str, timestamp: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO checkpoint (id, last_email_id, last_timestamp, updated_at)
               VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 last_email_id=excluded.last_email_id,
                 last_timestamp=excluded.last_timestamp,
                 updated_at=excluded.updated_at""",
            (email_id, timestamp, now),
        )
        self.conn.commit()

    # ---- logs ----
    def log(self, e: LogEntry) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO logs
               (email_id, subject, sender, received_at, category, action, status, error, ts)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (e.email_id, e.subject, e.sender, e.received_at, e.category,
             e.action, e.status, e.error, ts),
        )
        self.conn.commit()

    def already_processed(self, email_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM logs WHERE email_id=? AND status IN ('processed','error') LIMIT 1",
            (email_id,),
        )
        return cur.fetchone() is not None

    def error_ids(self) -> list[str]:
        cur = self.conn.execute(
            "SELECT DISTINCT email_id FROM logs WHERE status='error'"
        )
        return [r[0] for r in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()
