"""One-shot helper: retry the messages that errored on prior runs.

Why this exists: the main agent loop, on a single get_message() failure,
marks the message as errored and CONTINUES — but on Gmail IMAP a socket
error in the middle of a batch can poison the cached connection. The next
sweep hits the same error repeatedly. This script:

  1. Re-opens a fresh IMAP connection
  2. Fetches the specific errored message IDs
  3. Classifies them
  4. Trashes the safe ones (OTP_EXPIRED, SOCIAL, PROMO)
  5. Re-logs and clears them from the errored set

Idempotent and safe to re-run.
"""
from __future__ import annotations

import sys
import os
import imaplib
import email
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gmail_agent import config, classifier, gmail
from gmail_agent.schemas import Category, Classification, Email, LogEntry
from gmail_agent.store import Store
from gmail_agent.gmail import _decode_header, _extract_text


def retry_errored():
    s = config.get_settings()
    if s.get("mode") != "imap":
        print("ERROR: this retry helper only works in imap mode")
        return 1

    store = Store(s["db_path"])
    errs = store.error_ids()
    if not errs:
        print("No errored messages to retry. Done.")
        store.close()
        return 0

    # Fresh IMAP connection (not cached)
    M = imaplib.IMAP4_SSL(s["imap_host"])
    M.login(s["user_email"], s["app_password"])
    M.select("INBOX")

    trashed = 0
    kept = 0
    still_err = 0
    for mid in errs:
        try:
            status, data = M.fetch(mid.encode(), "(RFC822)")
            if status != "OK" or not data:
                print(f"  {mid}: FETCH status={status} — skipping")
                still_err += 1
                continue
            raw = None
            for part in data:
                if isinstance(part, tuple) and len(part) >= 2 and part[1]:
                    raw = part[1]
                    break
            if raw is None:
                print(f"  {mid}: no body in fetch response — skipping")
                still_err += 1
                continue

            msg = email.message_from_bytes(raw)
            subject = _decode_header(msg.get("Subject", ""))
            sender = _decode_header(msg.get("From", ""))
            date_hdr = msg.get("Date", "")
            body = _extract_text(msg)
            e = Email(
                id=mid, thread_id=mid, subject=subject, sender=sender,
                received_at=date_hdr, snippet=body[:200], body_text=body,
                labels=["INBOX"],
            )
            cls = classifier.classify(e)

            if cls.category in (Category.OTP_EXPIRED, Category.SOCIAL, Category.PROMO):
                try:
                    M.copy(mid.encode(), "[Gmail]/Trash")
                    M.store(mid.encode(), "+FLAGS", "\\Deleted")
                    M.expunge()
                    action = "trash"
                    trashed += 1
                    print(f"  {mid}: TRASHED ({cls.category.value}) sub={subject[:50]!r}")
                except Exception as exc:
                    action = "trash"
                    store.log(LogEntry(mid, subject, sender, date_hdr,
                                       cls.category.value, action, "error", str(exc)))
                    still_err += 1
                    continue
            else:
                action = "none"
                kept += 1
                print(f"  {mid}: KEPT ({cls.category.value}) sub={subject[:50]!r}")

            # Successful processing: log + clear from errored set by logging
            # a status=processed entry (overrides the prior error in views).
            store.log(LogEntry(mid, subject, sender, date_hdr,
                               cls.category.value, action, "processed"))
            store.save_checkpoint(mid, date_hdr)
        except Exception as exc:
            print(f"  {mid}: OUTER ERR {exc!r}")
            still_err += 1
            # Re-open connection if it died
            try:
                M.noop()
            except Exception:
                try:
                    M = imaplib.IMAP4_SSL(s["imap_host"])
                    M.login(s["user_email"], s["app_password"])
                    M.select("INBOX")
                except Exception:
                    pass

    M.close()
    M.logout()
    store.close()
    print(f"\nRETRY DONE: trashed={trashed} kept={kept} still_errored={still_err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(retry_errored())
