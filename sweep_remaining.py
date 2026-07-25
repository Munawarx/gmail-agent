"""Efficient batch sweeper for the rest of the inbox.

Why this exists: the main agent processes one at a time and the IMAP
trash+expunge per message is slow over a long connection. This script:

  1. Lists all INBOX UIDs fresh
  2. Filters out ones already logged as 'processed' (skips ~200)
  3. For each remaining: fetch, classify, trash-if-safe, log
  4. Uses a single connection, reconnects only on socket death
  5. Resumable: just re-run to continue
"""
from __future__ import annotations

import sys
import os
import imaplib
import email
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gmail_agent import config
from gmail_agent.gmail import _decode_header, _extract_text
from gmail_agent.classifier import classify
from gmail_agent.schemas import Email, Category, LogEntry
from gmail_agent.store import Store


def already_logged_processed(store: Store) -> set[str]:
    """Read the logs and return all email_ids that have status='processed'."""
    import sqlite3
    con = sqlite3.connect(store.db_path)
    cur = con.execute("SELECT email_id FROM logs WHERE status='processed'")
    ids = {r[0] for r in cur.fetchall()}
    con.close()
    return ids


def reconnect(s, M=None):
    if M:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass
    M = imaplib.IMAP4_SSL(s["imap_host"])
    M.login(s["user_email"], s["app_password"])
    M.select("INBOX")
    return M


def main():
    s = config.get_settings()
    store = Store(s["db_path"])
    skip = already_logged_processed(store)
    print(f"already_processed: {len(skip)} — skipping those")

    M = None
    M = reconnect(s, M)
    typ, data = M.search(None, "ALL")
    ids = [int(x) for x in data[0].split()]
    print(f"INBOX total: {len(ids)} UIDs, range {min(ids)}..{max(ids)}")

    processed = 0
    trashed = 0
    kept = 0
    errors = 0
    skipped = 0
    t0 = time.time()
    last_5 = []  # rolling log for visibility

    for i, mid in enumerate(ids):
        mid_s = str(mid)
        if mid_s in skip:
            skipped += 1
            continue
        try:
            typ, d = M.fetch(mid_s.encode(), "(RFC822)")
            if typ != "OK" or not d:
                errors += 1
                M = reconnect(s, M)
                continue
            raw = None
            for p in d:
                if isinstance(p, tuple) and len(p) >= 2 and p[1]:
                    raw = p[1]
                    break
            if not raw:
                errors += 1
                M = reconnect(s, M)
                continue
            msg = email.message_from_bytes(raw)
            subject = _decode_header(msg.get("Subject", ""))
            sender = _decode_header(msg.get("From", ""))
            body = _extract_text(msg)
            e = Email(
                id=mid_s, thread_id=mid_s, subject=subject, sender=sender,
                received_at=msg.get("Date", ""), snippet=body[:200],
                body_text=body, labels=["INBOX"],
            )
            cls = classify(e)
            action = "none"
            if cls.category in (Category.OTP_EXPIRED, Category.SOCIAL, Category.PROMO):
                try:
                    M.copy(mid_s.encode(), "[Gmail]/Trash")
                    M.store(mid_s.encode(), "+FLAGS", "\\Deleted")
                    M.expunge()
                    action = "trash"
                    trashed += 1
                except Exception as exc:
                    errors += 1
                    store.log(LogEntry(mid_s, subject, sender, msg.get("Date", ""),
                                       cls.category.value, "trash", "error", str(exc)))
                    M = reconnect(s, M)
                    continue
            else:
                kept += 1
            store.log(LogEntry(mid_s, subject, sender, msg.get("Date", ""),
                               cls.category.value, action, "processed"))
            store.save_checkpoint(mid_s, msg.get("Date", ""))
            processed += 1
            last_5.append(f"  UID {mid}: {cls.category.value:12s} {action:5s} | {sender[:30]!r} | {subject[:45]!r}")
            if len(last_5) > 5:
                last_5.pop(0)
            if processed % 20 == 0:
                elapsed = time.time() - t0
                rate = processed / elapsed if elapsed > 0 else 0
                print(f"[{processed:3d} done, {elapsed:5.1f}s, {rate:4.1f}/s] last 5:")
                for line in last_5:
                    print(line)
        except Exception as exc:
            errors += 1
            print(f"  UID {mid}: ERR {exc!r}")
            M = reconnect(s, M)

    try:
        M.close()
        M.logout()
    except Exception:
        pass
    store.close()
    elapsed = time.time() - t0
    print(f"\n=== SWEEP COMPLETE ===")
    print(f"  processed: {processed}  trashed: {trashed}  kept: {kept}  errors: {errors}  skipped: {skipped}")
    print(f"  elapsed:   {elapsed:.1f}s  rate: {processed/elapsed if elapsed>0 else 0:.1f}/s")
    print(f"  finished:  {datetime.now().isoformat()}")


if __name__ == "__main__":
    main()
