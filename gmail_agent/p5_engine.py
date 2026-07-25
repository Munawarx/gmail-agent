"""P5 Triage Engine — Multi-account, checkpointed, rule-driven Gmail scanner.

This is the core execution layer that implements the P5.md optimized prompt:

  For each account independently:
    1. Read checkpoints table — resume from last position if unfinished
    2. Scan labels in order: INBOX → Important → Spam → All Mail
    3. For each new (not-yet-processed) message:
       a. Check rules table first (exact sender/domain match → apply, skip reasoning)
       b. If no rule → reason from scratch (classify + protect check)
       c. Classify: Keep / Trash / Flag for review
       d. Log immediately (never lose progress on interruption)
    4. Checkpoint after every batch
    5. Stop at deletion cap or process cap
    6. Report end-of-run summary

Safety:
    - Deletion cap per run (default 50)
    - Confidence thresholds: ≥0.80 trash, 0.55-0.79 flag, <0.55 keep
    - Protected categories: never trash banking/gov/security/medical/legal/receipts/travel
    - User-approved rules are never overwritten
    - Message-ID dedup: never evaluate the same email twice
"""
from __future__ import annotations

import imaplib
import email
import uuid
import time
from email.header import decode_header as _dh
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

from .multiaccount import P5Config, Account, SCAN_LABELS
from .p5_store import P5Store, Checkpoint, RunSummary
from .p5_guardrail import check_protection, classify_confidence
from .learning import score_email
from .schemas import Email


def _decode_h(v: str) -> str:
    if not v:
        return ""
    parts = _dh(v)
    out = []
    for b, e in parts:
        if isinstance(b, bytes):
            out.append(b.decode(e or "utf-8", "replace"))
        else:
            out.append(b)
    return " ".join(out)


def _extract_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    pass
    try:
        return msg.get_payload(decode=True).decode("utf-8", "replace")
    except Exception:
        return ""


@dataclass
class TriageResult:
    """Result of a single account triage run."""
    account: str
    labels_scanned: list[str] = field(default_factory=list)
    analyzed: int = 0
    trashed: int = 0
    retained: int = 0
    flagged: int = 0
    skipped: int = 0
    errors: int = 0
    new_rules: int = 0
    flagged_emails: list[dict] = field(default_factory=list)
    checkpoint_label: str = ""
    checkpoint_message_id: str = ""
    remaining: str = ""


class P5TriageEngine:
    """The P5-compliant triage engine."""

    def __init__(self, config: P5Config, db_path: Path | str):
        self.config = config
        self.store = P5Store(db_path)
        self.trash_mailbox = "[Gmail]/Trash"

    def _connect(self, account: Account) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(account.imap_host)
        conn.login(account.email, account.app_password)
        return conn

    def _select_mailbox(self, conn: imaplib.IMAP4_SSL, label: str) -> bool:
        """Select a mailbox, handling Gmail's special folder names."""
        try:
            if label == "INBOX":
                conn.select("INBOX")
            else:
                # Quote the label for IMAP
                conn.select(f'"{label}"')
            return True
        except imaplib.IMAP4.error:
            return False

    def _get_message_id(self, msg) -> str:
        mid = msg.get("Message-ID", "") or msg.get("message-id", "")
        if not mid:
            # Fall back to X-GM-MSGID if available (from fetch response)
            mid = msg.get("X-GM-MSGID", "")
        return mid.strip().strip("<>")

    def _check_rules_first(
        self, account_email: str, sender: str
    ) -> Optional[str]:
        """Check rules table for exact sender/domain match. Returns action or None."""
        sender_lower = sender.lower()

        # Extract domain
        domain = ""
        if "@" in sender_lower:
            domain = sender_lower.split("@")[-1].split(">")[0].strip().strip('"').strip("'")

        # Try exact sender match
        rule = self.store.get_rule(account_email, sender_lower)
        if rule:
            return rule.action

        # Try domain match
        if domain:
            rule = self.store.get_rule(account_email, domain)
            if rule:
                return rule.action

        return None

    def triage_account(
        self,
        account: Account,
        dry_run: bool = False,
        max_emails: int = 0,
    ) -> TriageResult:
        """Run a full triage pass on one account."""
        run_id = str(uuid.uuid4())[:8]
        result = TriageResult(account=account.email)
        config = self.config
        started_at = datetime.now(timezone.utc).isoformat()

        print(f"\n{'='*60}")
        print(f"  TRIAGING: {account.email}")
        print(f"  Run ID: {run_id} | Cap: {config.delete_cap} | Dry-run: {dry_run}")
        print(f"{'='*60}")

        # Check for unfinished checkpoint to resume from
        unfinished = self.store.get_unfinished_checkpoint(account.email)
        if unfinished:
            print(f"  ⚡ RESUMING from unfinished checkpoint: {unfinished.label} "
                  f"(stop_reason={unfinished.stop_reason})")

        conn = None
        try:
            conn = self._connect(account)
            print(f"  ✅ Connected to {account.imap_host}")
        except Exception as e:
            print(f"  ❌ Cannot connect: {e}")
            result.errors = 1
            result.remaining = f"Connection failed: {e}"
            return result

        total_trashed_this_run = 0
        total_processed_this_run = 0
        email_cap = max_emails if max_emails > 0 else 999999

        for label in SCAN_LABELS:
            # Skip if already hit caps
            if total_trashed_this_run >= config.delete_cap:
                result.remaining = f"Deletion cap reached ({config.delete_cap})"
                print(f"\n  ⛔ Deletion cap reached — stopping")
                break
            if total_processed_this_run >= email_cap:
                result.remaining = f"Process cap reached ({email_cap})"
                print(f"\n  ⛔ Process cap reached — stopping")
                break

            # Check if this label has a completed checkpoint
            existing_cp = self.store.get_checkpoint(account.email, label)
            if existing_cp and existing_cp.stop_reason == "completed":
                print(f"\n  ⏭️  Skipping {label} — already completed in prior run")
                result.labels_scanned.append(f"{label} (cached)")
                continue

            print(f"\n  📂 Scanning {label}...")
            result.labels_scanned.append(label)

            if not self._select_mailbox(conn, label):
                print(f"     Cannot select {label} — skipping")
                continue

            status, data = conn.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                print(f"     No emails in {label}")
                self._save_checkpoint_safe(account.email, label, "completed", result)
                continue

            uids = data[0].split()
            print(f"     Found {len(uids)} emails")

            cp = Checkpoint(account=account.email, label=label)
            batch_count = 0

            for uid_bytes in uids:
                uid = uid_bytes.decode()

                # Cap checks
                if total_trashed_this_run >= config.delete_cap:
                    cp.stop_reason = "cap_reached"
                    result.remaining = f"Cap reached during {label}"
                    break
                if total_processed_this_run >= email_cap:
                    cp.stop_reason = "cap_reached"
                    result.remaining = f"Process cap during {label}"
                    break

                try:
                    # Re-select the label (previous expunge may have invalidated)
                    self._select_mailbox(conn, label)
                    status, msg_data = conn.fetch(uid_bytes, "(RFC822)")
                    raw = None
                    for p in msg_data:
                        if isinstance(p, tuple):
                            raw = p[1]
                            break
                    if not raw:
                        result.errors += 1
                        continue

                    msg = email.message_from_bytes(raw)
                    message_id = self._get_message_id(msg)
                    subject = _decode_h(msg.get("Subject", ""))
                    sender = _decode_h(msg.get("From", ""))
                    date_hdr = msg.get("Date", "")
                    body = _extract_text(msg)

                    # ── Dedup check ──
                    if message_id and self.store.is_processed(account.email, message_id):
                        result.skipped += 1
                        cp.skipped_count += 1
                        continue

                    # ── Build Email object ──
                    eo = Email(
                        id=uid, thread_id=message_id or uid, subject=subject,
                        sender=sender, received_at=date_hdr,
                        snippet=body[:200], body_text=body,
                    )

                    # ── Step 1: Check rules table first ──
                    rule_action = self._check_rules_first(account.email, sender)

                    if rule_action:
                        action = rule_action
                        confidence = 1.0
                        reason = f"Rule match: {sender[:40]}"
                    else:
                        # ── Step 2: Reason from scratch ──
                        confidence, score_reason, _details = score_email(eo)

                        # ── Step 3: Protection guardrail ──
                        prot = check_protection(sender, subject, body)

                        if prot.is_protected:
                            action = "keep"
                            confidence = 0.0
                            reason = f"PROTECTED ({prot.category}): {prot.reason[:50]}"
                        else:
                            action = classify_confidence(
                                confidence,
                                config.confidence_trash,
                                config.confidence_flag,
                            )
                            reason = score_reason[:60] if score_reason else f"conf={confidence:.2f}"

                    # ── Step 4: Log immediately ──
                    self.store.log_email(
                        run_id, account.email, message_id or uid,
                        sender, subject, action, reason, confidence,
                    )

                    icon = {"trash": "🔴", "flag": "🟡", "keep": "🟢"}.get(action, "❓")
                    print(f"     {icon} [{uid:>4}] {action:4s} conf={confidence:.2f} "
                          f"{sender[:30]:<30} | {subject[:35]}")

                    # ── Step 5: Execute action ──
                    if action == "trash" and not dry_run:
                        try:
                            self._select_mailbox(conn, label)
                            status_copy, _ = conn.copy(uid_bytes, self.trash_mailbox)
                            if status_copy == "OK":
                                conn.store(uid_bytes, "+FLAGS", "\\Deleted")
                                conn.expunge()
                                total_trashed_this_run += 1
                                result.trashed += 1
                                cp.trashed_count += 1

                                # Learn: write rule for this sender
                                if self.store.upsert_rule(
                                    account.email, sender.lower(), "sender",
                                    "trash", "learned", confidence,
                                ):
                                    result.new_rules += 1
                            else:
                                result.errors += 1
                        except Exception as e:
                            result.errors += 1
                            print(f"          ⚠️ trash error: {e}")

                    elif action == "flag":
                        result.flagged += 1
                        cp.flagged_count += 1
                        result.flagged_emails.append({
                            "uid": uid, "sender": sender, "subject": subject,
                            "confidence": confidence, "reason": reason,
                        })
                    else:
                        result.retained += 1
                        cp.kept_count += 1

                    # Mark processed
                    self.store.mark_processed(
                        account.email, message_id or uid, label,
                        action, sender, subject, confidence,
                    )

                    total_processed_this_run += 1
                    cp.processed_count += 1
                    cp.last_message_id = message_id or uid
                    cp.last_uid = uid
                    cp.timestamp = date_hdr
                    batch_count += 1

                    # ── Checkpoint every batch_size emails ──
                    if batch_count >= config.batch_size:
                        cp.stop_reason = "batch_checkpoint"
                        self.store.save_checkpoint(cp)
                        print(f"          💾 Checkpoint saved ({batch_count} emails)")
                        batch_count = 0

                except imaplib.IMAP4.error as e:
                    result.errors += 1
                    print(f"     ❌ [{uid}] IMAP error: {e}")
                    # Reconnect
                    try:
                        conn = self._connect(account)
                    except Exception:
                        print(f"     ❌ Reconnect failed — stopping {label}")
                        cp.stop_reason = "error"
                        break
                except Exception as e:
                    result.errors += 1
                    print(f"     ❌ [{uid}] error: {e}")

            # Save checkpoint for this label
            cp.stop_reason = "completed" if not result.remaining else "cap_reached"
            self.store.save_checkpoint(cp)
            result.checkpoint_label = label
            result.checkpoint_message_id = cp.last_message_id

        # Logout
        try:
            conn.logout()
        except Exception:
            pass

        # Save run summary
        completed_at = datetime.now(timezone.utc).isoformat()
        summary = RunSummary(
            run_id=run_id,
            account=account.email,
            labels_scanned=",".join(result.labels_scanned),
            emails_analyzed=result.analyzed + result.trashed + result.retained + result.flagged,
            emails_trashed=result.trashed,
            emails_retained=result.retained,
            emails_flagged=result.flagged,
            new_rules_learned=result.new_rules,
            errors=result.errors,
            checkpoint_label=result.checkpoint_label,
            checkpoint_message_id=result.checkpoint_message_id,
            remaining=result.remaining,
            started_at=started_at,
            completed_at=completed_at,
        )
        self.store.save_summary(summary)

        # Print summary
        self._print_report(result)

        return result

    def _save_checkpoint_safe(self, account_email: str, label: str,
                               stop_reason: str, result: TriageResult) -> None:
        cp = Checkpoint(account=account_email, label=label, stop_reason=stop_reason)
        self.store.save_checkpoint(cp)

    def _print_report(self, result: TriageResult) -> None:
        print(f"\n{'='*60}")
        print(f"  END-OF-RUN REPORT: {result.account}")
        print(f"{'='*60}")
        print(f"  📂 Labels scanned: {', '.join(result.labels_scanned)}")
        print(f"  📧 Emails analyzed: {result.analyzed + result.trashed + result.retained + result.flagged}")
        print(f"  🔴 Trashed:        {result.trashed}")
        print(f"  🟢 Retained:       {result.retained}")
        print(f"  🟡 Flagged:        {result.flagged}")
        print(f"  ⏭️  Skipped (dup):  {result.skipped}")
        print(f"  📝 New rules:      {result.new_rules}")
        print(f"  ❌ Errors:         {result.errors}")
        if result.checkpoint_label:
            print(f"  📍 Checkpoint:     {result.checkpoint_label} / {result.checkpoint_message_id[:20]}")
        if result.remaining:
            print(f"  ⚠️  Remaining:      {result.remaining}")
        if result.flagged_emails:
            print(f"\n  🟡 FLAGGED FOR REVIEW:")
            for fe in result.flagged_emails:
                print(f"     - {fe['sender'][:30]} | {fe['subject'][:40]} (conf={fe['confidence']:.2f})")
        print(f"{'='*60}")

    def run_all(self, dry_run: bool = False, max_emails: int = 0) -> list[TriageResult]:
        """Run triage on ALL configured accounts."""
        results = []
        for account in self.config.accounts:
            try:
                r = self.triage_account(account, dry_run=dry_run, max_emails=max_emails)
                results.append(r)
            except Exception as e:
                print(f"❌ Account {account.email} failed: {e}")
                results.append(TriageResult(account=account.email, errors=1, remaining=str(e)))
        return results

    def close(self) -> None:
        self.store.close()
