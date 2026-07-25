"""scan_all_mail.py — Scan All Mail, classify, and execute trash actions.

Selects [Gmail]/All Mail ONCE, fetches all emails in a batch, classifies,
then trashes in a second pass. No per-email re-select (kills IMAP connections).
"""
import imaplib
import email
import json
import sys
import time
from email.header import decode_header as _dh
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gmail_agent.schemas import Email
from gmail_agent.classifier import classify_with_learning
from gmail_agent.learning import _is_protected, record_feedback

TRASH = "[Gmail]/Trash"


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


def _decode_h(v):
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


def _extract_text(msg):
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


def _connect(env):
    conn = imaplib.IMAP4_SSL(env.get("GMAIL_IMAP_HOST", "imap.gmail.com"))
    conn.login(env["GMAIL_USER_EMAIL"], env["GMAIL_APP_PASSWORD"])
    return conn


def main():
    env = _load_env()

    # ═══ PHASE 1: SCAN (use Gmail search over INBOX) ═══
    print("[scan] Connecting...")
    conn = _connect(env)

    # Use INBOX + Gmail search to reach ALL emails (avoids selecting [Gmail]/All Mail which has space-quoting issues)
    conn.select("INBOX")
    status, data = conn.search(None, "X-GM-RAW", '"in:anywhere -in:trash -in:spam -in:drafts -in:sent"')
    uids = data[0].split() if data and data[0] else []
    total = len(uids)
    print(f"[scan] All Mail (via search): {total} emails")
    print("=" * 80)

    # Fetch all emails in this single selected session
    scanned = []
    trash_targets = []
    keep_count = 0

    for i, uid_bytes in enumerate(uids):
        uid = uid_bytes.decode()
        try:
            status, msg_data = conn.fetch(uid_bytes, "(RFC822)")
            raw = None
            for p in msg_data:
                if isinstance(p, tuple):
                    raw = p[1]
                    break
            if not raw:
                continue

            msg = email.message_from_bytes(raw)
            subject = _decode_h(msg.get("Subject", ""))
            sender = _decode_h(msg.get("From", ""))
            date = msg.get("Date", "")
            body = _extract_text(msg)

            eo = Email(
                id=uid, thread_id=uid, subject=subject, sender=sender,
                received_at=date, snippet=body[:200], body_text=body,
            )

            cls, action = classify_with_learning(eo, settings={"use_llm": False})
            prot, prot_reason = _is_protected(eo)

            if action == "trash" and not prot:
                icon = "🔴"
                trash_targets.append({
                    "uid": uid,
                    "sender": sender,
                    "subject": subject,
                    "conf": cls.confidence,
                    "category": cls.category.value,
                })
            elif action == "flag":
                icon = "🟡"
                keep_count += 1
            else:
                icon = "🟢"
                keep_count += 1

            print(f"  {icon} [{uid:>4}] conf={cls.confidence:.2f} {sender[:35]:<35} | {subject[:40]}")
            scanned.append(uid)

        except Exception as e:
            print(f"  ❌ [{uid}] error: {e}")

    conn.logout()
    print()
    print(f"SCAN COMPLETE: {len(trash_targets)} to trash | {keep_count} to keep out of {total}")
    print()

    if not trash_targets:
        print("Nothing to trash. Done!")
        return

    # ═══ PHASE 2: TRASH (fresh connection, one-by-one with re-connect on error) ═══
    print(f"[trash] Trashing {len(trash_targets)} emails...")
    trashed = 0
    errors = 0

    conn = _connect(env)
    conn.select("INBOX")

    for target in trash_targets:
        uid = target["uid"]
        uid_bytes = uid.encode()

        try:
            # COPY to Trash, then mark deleted
            status_copy, _ = conn.copy(uid_bytes, TRASH)
            if status_copy == "OK":
                conn.store(uid_bytes, "+FLAGS", "\\Deleted")
                conn.expunge()
                trashed += 1
                print(f"  🔴 TRASHED [{uid}] {target['sender'][:35]} | {target['subject'][:40]}")
                record_feedback(
                    "delete", uid, target["sender"], target["subject"],
                    category=target["category"], confidence=target["conf"],
                )
            else:
                errors += 1
                print(f"  ❌ [{uid}] COPY failed")
        except Exception as e:
            # Reconnect on socket errors
            print(f"  ⚠️ [{uid}] error: {e} — reconnecting...")
            errors += 1
            try:
                conn = _connect(env)
                conn.select("INBOX")
            except Exception:
                print("  ❌ Reconnect failed — stopping.")
                break

    try:
        conn.logout()
    except Exception:
        pass

    print()
    print("=" * 60)
    print("  EXECUTION COMPLETE")
    print("=" * 60)
    print(f"  Total scanned: {total}")
    print(f"  🔴 Trashed:    {trashed}")
    print(f"  🟢 Kept:       {keep_count}")
    print(f"  ❌ Errors:     {errors}")
    print("=" * 60)


if __name__ == "__main__":
    main()
