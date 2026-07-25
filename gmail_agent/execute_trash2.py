"""execute_trash2.py — Scan + Trash in one pass (UID-safe).

Scans Promotions + Updates tabs, classifies each email, and trashes
high-confidence matches immediately — all within a single IMAP session
so UIDs don't go stale between scan and execute.
"""
import imaplib
import email
import json
import sys
from email.header import decode_header as _dh
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gmail_agent.schemas import Email
from gmail_agent.classifier import classify_with_learning
from gmail_agent.learning import record_feedback

TRASH_MAILBOX = "[Gmail]/Trash"


def _load_env():
    env = {}
    p = ROOT / ".env"
    if p.exists():
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _decode_header(value):
    if not value:
        return ""
    parts = _dh(value)
    out = []
    for blob, enc in parts:
        if isinstance(blob, bytes):
            out.append(blob.decode(enc or "utf-8", "replace"))
        else:
            out.append(blob)
    return " ".join(out)


def _extract_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    return ""
    try:
        return msg.get_payload(decode=True).decode("utf-8", "replace")
    except Exception:
        return ""


def process_tab(conn, tab_name):
    """Scan + trash one tab in a single pass. Returns stats dict."""
    print(f"\n{'='*60}")
    print(f"  PROCESSING: {tab_name.upper()}")
    print(f"{'='*60}")

    conn.select("INBOX", readonly=True)
    status, data = conn.search(None, "X-GM-RAW", f'"category:{tab_name}"')
    if status != "OK" or not data or not data[0]:
        print(f"  No emails in {tab_name}")
        return {"trashed": 0, "protected": 0, "errors": 0, "kept": 0}

    uids = data[0].split()
    print(f"  Found {len(uids)} emails\n")

    trashed = 0
    protected = 0
    errors = 0
    kept = 0

    for uid_bytes in uids:
        uid = uid_bytes.decode()
        try:
            # Fetch in readonly mode
            conn.select("INBOX", readonly=True)
            status, msg_data = conn.fetch(uid_bytes, "(RFC822)")
            if status != "OK":
                errors += 1
                continue

            raw_bytes = None
            for part in msg_data:
                if isinstance(part, tuple):
                    raw_bytes = part[1]
                    break
            if raw_bytes is None:
                errors += 1
                continue

            msg = email.message_from_bytes(raw_bytes)
            subject = _decode_header(msg.get("Subject", ""))
            sender = _decode_header(msg.get("From", ""))
            date_hdr = msg.get("Date", "")
            body = _extract_text(msg)

            email_obj = Email(
                id=uid, thread_id=uid, subject=subject, sender=sender,
                received_at=date_hdr, snippet=body[:200], body_text=body,
            )

            # Classify
            cls, action = classify_with_learning(email_obj, settings={"use_llm": False})

            if action == "trash":
                # Switch to read-write mode and trash
                conn.select("INBOX")
                status_copy, _ = conn.copy(uid_bytes, TRASH_MAILBOX)
                if status_copy == "OK":
                    conn.store(uid_bytes, "+FLAGS", "\\Deleted")
                    conn.expunge()
                    trashed += 1
                    print(f"  🔴 [{uid:>4}] TRASHED  {sender[:35]:<35} | {subject[:40]}")
                    record_feedback("delete", uid, sender, subject,
                                    category=cls.category.value, confidence=cls.confidence)
                else:
                    errors += 1
                    print(f"  ❌ [{uid:>4}] COPY FAILED {sender[:35]}")
            else:
                kept += 1
                icon = "🟡" if action == "flag" else "🟢"
                print(f"  {icon} [{uid:>4}] KEEP     {sender[:35]:<35} | {subject[:40]}")

        except imaplib.IMAP4.error as e:
            # UID might have been invalidated — skip gracefully
            errors += 1
            print(f"  ⚠️ [{uid:>4}] IMAP error (likely already moved): {e}")
        except Exception as e:
            errors += 1
            print(f"  ❌ [{uid:>4}] ERROR: {e}")

    print(f"\n  {tab_name.upper()}: {trashed} trashed | {kept} kept | {errors} errors")
    return {"trashed": trashed, "protected": protected, "errors": errors, "kept": kept}


def main():
    env = _load_env()
    conn = imaplib.IMAP4_SSL(env.get("GMAIL_IMAP_HOST", "imap.gmail.com"))
    conn.login(env["GMAIL_USER_EMAIL"], env["GMAIL_APP_PASSWORD"])
    print(f"[execute] Connected as {env['GMAIL_USER_EMAIL']}")

    # Process each tab in UID-safe single pass
    all_stats = {"trashed": 0, "protected": 0, "errors": 0, "kept": 0}
    for tab in ["promotions", "updates", "social"]:
        stats = process_tab(conn, tab)
        for k in all_stats:
            all_stats[k] += stats[k]

    conn.logout()

    print(f"\n{'='*60}")
    print(f"  ✅ EXECUTION COMPLETE")
    print(f"{'='*60}")
    print(f"  🔴 Trashed:  {all_stats['trashed']}")
    print(f"  🟢 Kept:     {all_stats['kept']}")
    print(f"  ❌ Errors:   {all_stats['errors']}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
