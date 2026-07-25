"""P5 State Store — rules table + checkpoints table + message-ID dedup.

Per P5.md spec:
  1. `rules` — sender/domain/keyword → action (trash/keep) + who approved + date
  2. `checkpoints` — account, label, last processed message-id, timestamp, counts, stop reason
  3. `processed` — message-ID dedup (never reason about the same email twice)

This is the actual "memory" — the external state store the agent reads from
and writes to every run. Without this, there is no persistence between runs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA = """
-- Rules table: sender/domain → action (user-approved or learned)
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,          -- which account this rule applies to ('*' = all)
    pattern TEXT NOT NULL,          -- sender email or domain (e.g. 'noreply@adobe.com' or 'adobe.com')
    pattern_type TEXT NOT NULL,     -- 'sender' | 'domain' | 'keyword'
    action TEXT NOT NULL CHECK(action IN ('trash', 'keep', 'flag')),
    approved_by TEXT NOT NULL,      -- 'user' | 'learned' | 'system-default'
    confidence REAL DEFAULT 0.0,    -- confidence score when learned
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account, pattern)
);

-- Checkpoints table: per-account, per-label resume state
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    label TEXT NOT NULL,
    last_message_id TEXT,
    last_uid TEXT,
    timestamp TEXT NOT NULL,
    processed_count INTEGER DEFAULT 0,
    trashed_count INTEGER DEFAULT 0,
    skipped_count INTEGER DEFAULT 0,
    flagged_count INTEGER DEFAULT 0,
    kept_count INTEGER DEFAULT 0,
    stop_reason TEXT,               -- 'completed' | 'cap_reached' | 'quota_reached' | 'manual_stop' | 'error'
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account, label)
);

-- Processed message-IDs: dedup key (never evaluate the same email twice)
CREATE TABLE IF NOT EXISTS processed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL,
    message_id TEXT NOT NULL,
    label TEXT,                     -- where it was seen
    action TEXT,                    -- what was decided
    sender TEXT,
    subject TEXT,
    confidence REAL DEFAULT 0.0,
    processed_at TEXT NOT NULL,
    UNIQUE(account, message_id)
);

-- Run logs: per-email decision log (written immediately before moving on)
CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    account TEXT NOT NULL,
    message_id TEXT,
    sender TEXT,
    subject TEXT,
    action TEXT,
    reason TEXT,
    confidence REAL,
    ts TEXT NOT NULL
);

-- Run summaries: end-of-run report per account
CREATE TABLE IF NOT EXISTS run_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    account TEXT NOT NULL,
    labels_scanned TEXT,            -- comma-separated
    emails_analyzed INTEGER DEFAULT 0,
    emails_trashed INTEGER DEFAULT 0,
    emails_retained INTEGER DEFAULT 0,
    emails_flagged INTEGER DEFAULT 0,
    new_rules_learned INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    checkpoint_label TEXT,
    checkpoint_message_id TEXT,
    checkpoint_timestamp TEXT,
    remaining TEXT,                 -- what's left if run stopped early
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_rules_pattern ON rules(account, pattern);
CREATE INDEX IF NOT EXISTS idx_checkpoint_acct ON checkpoints(account, label);
CREATE INDEX IF NOT EXISTS idx_processed_msg ON processed(account, message_id);
CREATE INDEX IF NOT EXISTS idx_runlogs_run ON run_logs(run_id);
"""


@dataclass
class Rule:
    pattern: str
    pattern_type: str  # 'sender' | 'domain' | 'keyword'
    action: str        # 'trash' | 'keep' | 'flag'
    approved_by: str   # 'user' | 'learned' | 'system-default'
    confidence: float = 0.0
    created_at: str = ""
    updated_at: str = ""


@dataclass
class Checkpoint:
    account: str
    label: str
    last_message_id: str = ""
    last_uid: str = ""
    timestamp: str = ""
    processed_count: int = 0
    trashed_count: int = 0
    skipped_count: int = 0
    flagged_count: int = 0
    kept_count: int = 0
    stop_reason: str = ""


@dataclass
class RunSummary:
    run_id: str
    account: str
    labels_scanned: str = ""
    emails_analyzed: int = 0
    emails_trashed: int = 0
    emails_retained: int = 0
    emails_flagged: int = 0
    new_rules_learned: int = 0
    errors: int = 0
    checkpoint_label: str = ""
    checkpoint_message_id: str = ""
    checkpoint_timestamp: str = ""
    remaining: str = ""
    started_at: str = ""
    completed_at: str = ""


class P5Store:
    """P5-compliant state store with rules, checkpoints, and dedup."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # ═══════════════════════════════════════
    #  RULES TABLE
    # ═══════════════════════════════════════

    def get_rule(self, account: str, pattern: str) -> Optional[Rule]:
        """Look up exact sender/domain rule. Checks account-specific then global ('*')."""
        # Try account-specific first
        cur = self.conn.execute(
            "SELECT pattern, pattern_type, action, approved_by, confidence, created_at, updated_at "
            "FROM rules WHERE account=? AND pattern=?",
            (account, pattern.lower()),
        )
        row = cur.fetchone()
        if not row:
            # Try global
            cur = self.conn.execute(
                "SELECT pattern, pattern_type, action, approved_by, confidence, created_at, updated_at "
                "FROM rules WHERE account='*' AND pattern=?",
                (pattern.lower(),),
            )
            row = cur.fetchone()
        if not row:
            return None
        return Rule(
            pattern=row[0], pattern_type=row[1], action=row[2],
            approved_by=row[3], confidence=row[4],
            created_at=row[5], updated_at=row[6],
        )

    def get_all_rules(self, account: str = "*") -> list[Rule]:
        """Get all rules for an account (or all)."""
        cur = self.conn.execute(
            "SELECT pattern, pattern_type, action, approved_by, confidence, created_at, updated_at "
            "FROM rules WHERE account IN ('*', ?) ORDER BY approved_by DESC, pattern",
            (account,),
        )
        return [
            Rule(pattern=r[0], pattern_type=r[1], action=r[2], approved_by=r[3],
                 confidence=r[4], created_at=r[5], updated_at=r[6])
            for r in cur.fetchall()
        ]

    def upsert_rule(
        self, account: str, pattern: str, pattern_type: str,
        action: str, approved_by: str, confidence: float = 0.0,
        allow_overwrite_user: bool = False,
    ) -> bool:
        """Insert or update a rule.

        Never overwrites a user-approved rule unless allow_overwrite_user=True.
        Returns True if written, False if blocked by existing user rule.
        """
        now = self._now()
        pattern_lower = pattern.lower()

        # Check for existing user-approved rule
        existing = self.get_rule(account, pattern_lower)
        if existing and existing.approved_by == "user" and approved_by != "user" and not allow_overwrite_user:
            return False  # Never overwrite user rules

        self.conn.execute(
            """INSERT INTO rules (account, pattern, pattern_type, action, approved_by, confidence, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account, pattern) DO UPDATE SET
                 pattern_type=excluded.pattern_type,
                 action=excluded.action,
                 approved_by=excluded.approved_by,
                 confidence=excluded.confidence,
                 updated_at=excluded.updated_at""",
            (account, pattern_lower, pattern_type, action, approved_by, confidence, now, now),
        )
        self.conn.commit()
        return True

    def delete_rule(self, account: str, pattern: str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM rules WHERE account IN ('*', ?) AND pattern=?",
            (account, pattern.lower()),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ═══════════════════════════════════════
    #  CHECKPOINTS TABLE
    # ═══════════════════════════════════════

    def get_checkpoint(self, account: str, label: str) -> Optional[Checkpoint]:
        cur = self.conn.execute(
            "SELECT account, label, last_message_id, last_uid, timestamp, "
            "processed_count, trashed_count, skipped_count, flagged_count, kept_count, stop_reason "
            "FROM checkpoints WHERE account=? AND label=?",
            (account, label),
        )
        row = cur.fetchone()
        if not row:
            return None
        return Checkpoint(
            account=row[0], label=row[1], last_message_id=row[2], last_uid=row[3],
            timestamp=row[4], processed_count=row[5], trashed_count=row[6],
            skipped_count=row[7], flagged_count=row[8], kept_count=row[9], stop_reason=row[10],
        )

    def get_unfinished_checkpoint(self, account: str) -> Optional[Checkpoint]:
        """Find an unfinished checkpoint (stop_reason not 'completed') for this account."""
        cur = self.conn.execute(
            "SELECT account, label, last_message_id, last_uid, timestamp, "
            "processed_count, trashed_count, skipped_count, flagged_count, kept_count, stop_reason "
            "FROM checkpoints WHERE account=? AND stop_reason != 'completed' "
            "ORDER BY updated_at DESC LIMIT 1",
            (account,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return Checkpoint(
            account=row[0], label=row[1], last_message_id=row[2], last_uid=row[3],
            timestamp=row[4], processed_count=row[5], trashed_count=row[6],
            skipped_count=row[7], flagged_count=row[8], kept_count=row[9], stop_reason=row[10],
        )

    def save_checkpoint(self, cp: Checkpoint) -> None:
        now = self._now()
        self.conn.execute(
            """INSERT INTO checkpoints
               (account, label, last_message_id, last_uid, timestamp,
                processed_count, trashed_count, skipped_count, flagged_count, kept_count,
                stop_reason, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account, label) DO UPDATE SET
                 last_message_id=excluded.last_message_id,
                 last_uid=excluded.last_uid,
                 timestamp=excluded.timestamp,
                 processed_count=excluded.processed_count,
                 trashed_count=excluded.trashed_count,
                 skipped_count=excluded.skipped_count,
                 flagged_count=excluded.flagged_count,
                 kept_count=excluded.kept_count,
                 stop_reason=excluded.stop_reason,
                 updated_at=excluded.updated_at""",
            (cp.account, cp.label, cp.last_message_id, cp.last_uid, cp.timestamp,
             cp.processed_count, cp.trashed_count, cp.skipped_count,
             cp.flagged_count, cp.kept_count, cp.stop_reason, now, now),
        )
        self.conn.commit()

    def get_all_checkpoints(self, account: str) -> list[Checkpoint]:
        cur = self.conn.execute(
            "SELECT account, label, last_message_id, last_uid, timestamp, "
            "processed_count, trashed_count, skipped_count, flagged_count, kept_count, stop_reason "
            "FROM checkpoints WHERE account=? ORDER BY label",
            (account,),
        )
        return [
            Checkpoint(
                account=r[0], label=r[1], last_message_id=r[2], last_uid=r[3],
                timestamp=r[4], processed_count=r[5], trashed_count=r[6],
                skipped_count=r[7], flagged_count=r[8], kept_count=r[9], stop_reason=r[10],
            )
            for r in cur.fetchall()
        ]

    # ═══════════════════════════════════════
    #  PROCESSED TABLE (message-ID dedup)
    # ═══════════════════════════════════════

    def is_processed(self, account: str, message_id: str) -> bool:
        if not message_id:
            return False
        cur = self.conn.execute(
            "SELECT 1 FROM processed WHERE account=? AND message_id=? LIMIT 1",
            (account, message_id.strip().strip("<>")),
        )
        return cur.fetchone() is not None

    def mark_processed(
        self, account: str, message_id: str, label: str = "",
        action: str = "", sender: str = "", subject: str = "",
        confidence: float = 0.0,
    ) -> None:
        if not message_id:
            return
        now = self._now()
        mid = message_id.strip().strip("<>")
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO processed
                   (account, message_id, label, action, sender, subject, confidence, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (account, mid, label, action, sender, subject, confidence, now),
            )
            self.conn.commit()
        except sqlite3.IntegrityError:
            pass  # Already processed — fine

    # ═══════════════════════════════════════
    #  RUN LOGS
    # ═══════════════════════════════════════

    def log_email(
        self, run_id: str, account: str, message_id: str,
        sender: str, subject: str, action: str,
        reason: str = "", confidence: float = 0.0,
    ) -> None:
        """Log a per-email decision immediately (before moving to next email)."""
        now = self._now()
        self.conn.execute(
            """INSERT INTO run_logs
               (run_id, account, message_id, sender, subject, action, reason, confidence, ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, account, message_id, sender, subject, action, reason, confidence, now),
        )
        self.conn.commit()

    # ═══════════════════════════════════════
    #  RUN SUMMARIES (end-of-run report)
    # ═══════════════════════════════════════

    def save_summary(self, summary: RunSummary) -> None:
        self.conn.execute(
            """INSERT INTO run_summaries
               (run_id, account, labels_scanned, emails_analyzed, emails_trashed,
                emails_retained, emails_flagged, new_rules_learned, errors,
                checkpoint_label, checkpoint_message_id, checkpoint_timestamp,
                remaining, started_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (summary.run_id, summary.account, summary.labels_scanned,
             summary.emails_analyzed, summary.emails_trashed,
             summary.emails_retained, summary.emails_flagged,
             summary.new_rules_learned, summary.errors,
             summary.checkpoint_label, summary.checkpoint_message_id,
             summary.checkpoint_timestamp, summary.remaining,
             summary.started_at, summary.completed_at),
        )
        self.conn.commit()

    def get_last_summary(self, account: str) -> Optional[RunSummary]:
        cur = self.conn.execute(
            "SELECT run_id, account, labels_scanned, emails_analyzed, emails_trashed, "
            "emails_retained, emails_flagged, new_rules_learned, errors, "
            "checkpoint_label, checkpoint_message_id, checkpoint_timestamp, remaining, "
            "started_at, completed_at "
            "FROM run_summaries WHERE account=? ORDER BY completed_at DESC LIMIT 1",
            (account,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return RunSummary(
            run_id=row[0], account=row[1], labels_scanned=row[2],
            emails_analyzed=row[3], emails_trashed=row[4], emails_retained=row[5],
            emails_flagged=row[6], new_rules_learned=row[7], errors=row[8],
            checkpoint_label=row[9], checkpoint_message_id=row[10],
            checkpoint_timestamp=row[11], remaining=row[12],
            started_at=row[13], completed_at=row[14],
        )

    # ═══════════════════════════════════════
    #  UTILITIES
    # ═══════════════════════════════════════

    def close(self) -> None:
        self.conn.close()
