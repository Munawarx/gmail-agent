"""The agent processing loop.

Key behaviors (per spec):
  * Resume from the saved checkpoint (last processed email id + timestamp).
  * Process emails chronologically, one by one, never skip, never duplicate.
  * Save checkpoint after each email so a crash mid-run is safe.
  * On error: log, skip only that email, continue; retry errors next run.
  * DRY_RUN: analyze + log but never call trash().
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from . import config, classifier, gmail
from .schemas import Category, Classification, Email, LogEntry
from .store import Store


def _email_from_gmail(msg: dict) -> Email:
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    parts = msg.get("payload", {}).get("parts", [])
    body = ""
    snippet = msg.get("snippet", "")

    def _walk(ps):
        out = ""
        for p in ps:
            if p.get("mimeType") == "text/plain" and p.get("body", {}).get("data"):
                import base64
                out += base64.urlsafe_b64decode(p["body"]["data"]).decode("utf-8", "replace")
            elif p.get("parts"):
                out += _walk(p["parts"])
        return out

    if parts:
        body = _walk(parts)
    elif msg.get("payload", {}).get("body", {}).get("data"):
        import base64
        body = base64.urlsafe_b64decode(msg["payload"]["body"]["data"]).decode("utf-8", "replace")

    internal = msg.get("internalDate", "")
    return Email(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        subject=headers.get("Subject", ""),
        sender=headers.get("From", ""),
        received_at=internal or headers.get("Date", ""),
        snippet=snippet,
        body_text=body,
        labels=msg.get("labelIds", []),
    )


def run(dry_run: bool = False, max_emails: int = 0,
        gmail_client: Optional[gmail.GmailClient] = None,
        store: Optional[Store] = None) -> dict:
    """Execute one pass. Returns a summary dict. If gmail_client/store are None,
    they are created from config (real mode)."""
    settings = config.get_settings()
    dry_run = dry_run or settings["dry_run"]
    max_emails = max_emails or settings["max_emails"]
    own_store = store is None
    own_client = gmail_client is None

    store = store or Store(settings["db_path"])
    if gmail_client is None:
        if settings.get("mode") == "imap":
            client = gmail.ImapClient(
                settings["user_email"], settings["app_password"],
                settings.get("imap_host", "imap.gmail.com"))
        else:
            client = gmail.GmailClient(
                settings["credentials_json"], settings["token_json"],
                settings["user_email"] or "me")
    else:
        client = gmail_client
    if own_client:
        client.authenticate()

    checkpoint = store.get_checkpoint()
    last_id = checkpoint[0] if checkpoint else None
    last_ts = checkpoint[1] if checkpoint else None

    print(f"[agent] checkpoint: id={last_id} ts={last_ts} dry_run={dry_run}")
    print(f"[agent] fetching messages from Gmail (oldest first)...")

    processed = 0
    trashed = 0
    errors = 0
    skipped = 0

    page_token = None
    # Gmail list returns newest-first by default; we requested orderBy=date
    # (oldest first). We iterate pages until we pass the checkpoint.
    seen_checkpoint = last_id is None  # if no checkpoint, process everything

    try:
        while True:
            ids, page_token = client.list_ids_chronological(page_token)
            if not ids:
                break
            for mid in ids:
                if not seen_checkpoint:
                    if mid == last_id:
                        seen_checkpoint = True
                    # skip until we reach the checkpoint message
                    continue
                if max_emails and processed >= max_emails:
                    break

                try:
                    raw = client.get_message(mid)
                except Exception as e:  # noqa: BLE001
                    store.log(LogEntry(mid, "", "", "", "unknown",
                                        "none", "error", str(e)))
                    errors += 1
                    continue

                email = _email_from_gmail(raw)
                if store.already_processed(mid):
                    skipped += 1
                    continue

                try:
                    cls: Classification = classifier.classify_with_learning(email, settings)
                    if isinstance(cls, tuple):
                        cls, action = cls
                    else:
                        action = "none"
                except Exception as e:  # noqa: BLE001
                    store.log(LogEntry(mid, email.subject, email.sender,
                                        email.received_at, "unknown", "none",
                                        "error", str(e)))
                    errors += 1
                    continue

                # Determine action based on classification + learning
                if action == "none":
                    if cls.category in (Category.OTP_EXPIRED, Category.SOCIAL, Category.PROMO):
                        action = "trash"
                    elif cls.category in (Category.KEEP, Category.OTP_VALID, Category.IMPORTANT):
                        action = "keep"

                # Execute action
                if action == "trash":
                    if not dry_run:
                        try:
                            client.trash(mid)
                            trashed += 1
                        except Exception as e:  # noqa: BLE001
                            store.log(LogEntry(mid, email.subject, email.sender,
                                                email.received_at, cls.category.value,
                                                "trash", "error", str(e)))
                            errors += 1
                            # Retry contract: do NOT advance the checkpoint past a
                            # failed email, so the next run re-evaluates it. The
                            # failed row stays status='error' (already_processed()
                            # ignores 'error'), guaranteeing a retry instead of a
                            # silent permanent skip.
                            continue

                elif action == "flag":
                    # Medium confidence — log for review but don't trash
                    print(f"  ⚠️ FLAGGED for review: {email.short()} (conf={cls.confidence:.2f})")

                store.log(LogEntry(mid, email.subject, email.sender, email.received_at,
                                    cls.category.value, action, "processed"))
                store.save_checkpoint(mid, email.received_at)
                processed += 1
                print(f"[agent] {email.short()} -> {cls.category.value}/{action} (conf={cls.confidence:.2f})")
            if not page_token:
                break
            if max_emails and processed >= max_emails:
                break
    finally:
        if own_store:
            store.close()

    summary = {"processed": processed, "trashed": trashed, "errors": errors,
               "skipped": skipped, "dry_run": dry_run,
               "finished_at": datetime.now(timezone.utc).isoformat()}
    print(f"[agent] DONE {summary}")
    return summary
