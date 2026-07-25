"""execute_trash.py — Move high-confidence promotional emails to Trash.

Smart execution:
  1. Reads tab_analysis.json (scan results)
  2. Only trashes 'action=trash' emails (high confidence ≥0.80)
  3. NEVER touches 'keep' or 'flag' emails
  4. Double-checks each email is NOT protected before trashing
  5. Moves via IMAP COPY to [Gmail]/Trash + DELETE
  6. Records feedback for continuous learning
  7. Reports results
"""
import imaplib
import json
import sys
import time
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
    from email.header import decode_header as _dh
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


def main():
    # Load scan results
    analysis_path = ROOT / "tab_analysis.json"
    with open(analysis_path, "r", encoding="utf-8") as f:
        results = json.load(f)

    trash_targets = [r for r in results if r["action"] == "trash"]
    keep_targets = [r for r in results if r["action"] != "trash"]

    print(f"[execute] {len(trash_targets)} to trash, {len(keep_targets)} to keep")

    # Connect
    env = _load_env()
    conn = imaplib.IMAP4_SSL(env.get("GMAIL_IMAP_HOST", "imap.gmail.com"))
    conn.login(env["GMAIL_USER_EMAIL"], env["GMAIL_APP_PASSWORD"])
    print(f"[execute] Connected as {env['GMAIL_USER_EMAIL']}")

    trashed = 0
    skipped_protected = 0
    errors = 0

    for target in trash_targets:
        uid = target["uid"]
        sender = target["sender"]
        subject = target["subject"]
        tab = target["tab"]

        try:
            # Re-fetch the email to double-check it's not protected
            conn.select("INBOX", readonly=True)
            status, msg_data = conn.fetch(uid.encode(), "(RFC822)")
            if status != "OK":
                print(f"  ❌ [{uid}] Could not fetch — skipping")
                errors += 1
                continue

            import email as emailmod
            raw_bytes = None
            for part in msg_data:
                if isinstance(part, tuple):
                    raw_bytes = part[1]
                    break
            if raw_bytes is None:
                errors += 1
                continue

            msg = emailmod.message_from_bytes(raw_bytes)
            real_subject = _decode_header(msg.get("Subject", ""))
            real_sender = _decode_header(msg.get("From", ""))
            real_date = msg.get("Date", "")
            body = _extract_text(msg)

            email_obj = Email(
                id=uid, thread_id=uid, subject=real_subject, sender=real_sender,
                received_at=real_date, snippet=body[:200], body_text=body,
            )

            # FINAL safety check — re-classify with full pipeline
            cls, action = classify_with_learning(email_obj, settings={"use_llm": False})

            if action != "trash":
                print(f"  🛡️  [{uid}] PROTECTED on re-check ({action}) — skipping: {real_sender[:30]} | {real_subject[:30]}")
                skipped_protected += 1
                continue

            # Execute trash: COPY to [Gmail]/Trash then mark deleted
            conn.select("INBOX")
            status_copy, _ = conn.copy(uid.encode(), TRASH_MAILBOX)
            if status_copy == "OK":
                conn.store(uid.encode(), "+FLAGS", "\\Deleted")
                conn.expunge()
                trashed += 1
                print(f"  🔴 [{uid}] TRASHED  {real_sender[:35]:<35} | {real_subject[:40]}")
                # Record feedback for learning
                record_feedback("delete", uid, real_sender, real_subject,
                                category=cls.category.value, confidence=cls.confidence)
            else:
                print(f"  ❌ [{uid}] COPY failed — skipping")
                errors += 1

        except Exception as e:
            print(f"  ❌ [{uid}] ERROR: {e}")
            errors += 1

    conn.logout()

    # Summary
    print(f"\n{'='*60}")
    print(f"  EXECUTION COMPLETE")
    print(f"{'='*60}")
    print(f"  🔴 Trashed:            {trashed}")
    print(f"  🛡️  Protected (skipped): {skipped_protected}")
    print(f"  ❌ Errors:              {errors}")
    print(f"  🟢 Kept in inbox:      {len(keep_targets)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
