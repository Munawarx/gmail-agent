"""scan_tabs.py — Analyze Promotions, Updates, and Social Gmail tabs.

Connects via IMAP, reads emails from each Gmail category tab,
runs them through the learned-pattern scoring engine, and reports:
  - What would be AUTO-TRASHED (high confidence)
  - What would be FLAGGED for review (medium confidence)
  - What would be KEPT (low confidence / protected)
"""
import imaplib
import email
import json
import sys
from email.header import decode_header as _decode_header_raw
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gmail_agent.schemas import Email
from gmail_agent.learning import score_email, classify_learned, _is_protected, load_patterns
from gmail_agent.classifier import classify_with_learning

TAB_LABELS = {
    "promotions": "CATEGORY_PROMOTIONS",
    "updates": "CATEGORY_UPDATES",
    "social": "CATEGORY_SOCIAL",
}


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
    parts = _decode_header_raw(value)
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


def scan_tab(conn, tab_name, label, max_emails=50):
    """Scan one Gmail tab and classify all emails."""
    print(f"\n{'='*60}")
    print(f"  SCANNING: {tab_name.upper()} ({label})")
    print(f"{'='*60}")

    # Select INBOX first (IMAP requires a mailbox selected before search)
    conn.select("INBOX", readonly=True)
    # Search using Gmail's raw search (category:promotions etc.)
    status, data = conn.search(None, "X-GM-RAW", f'"category:{tab_name}"')
    if status != "OK" or not data or not data[0]:
        print(f"  No emails found in {tab_name}")
        return []

    uids = data[0].split()
    print(f"  Found {len(uids)} emails (scanning up to {max_emails})")
    print()

    results = []
    auto_trash = []
    flagged = []
    kept = []

    for uid_bytes in uids[:max_emails]:
        uid = uid_bytes.decode()
        try:
            status, msg_data = conn.fetch(uid_bytes, "(RFC822)")
            if status != "OK" or not msg_data:
                continue
            raw_bytes = None
            for part in msg_data:
                if isinstance(part, tuple):
                    raw_bytes = part[1]
                    break
            if raw_bytes is None:
                continue

            msg = email.message_from_bytes(raw_bytes)
            subject = _decode_header(msg.get("Subject", ""))
            sender = _decode_header(msg.get("From", ""))
            date_hdr = msg.get("Date", "")
            body = _extract_text(msg)

            email_obj = Email(
                id=uid, thread_id=uid, subject=subject, sender=sender,
                received_at=date_hdr, snippet=body[:200] if body else "",
                body_text=body, labels=[label],
            )

            # Classify using the full learning pipeline
            cls, action = classify_with_learning(email_obj, settings={"use_llm": False})
            conf = cls.confidence

            entry = {
                "uid": uid,
                "tab": tab_name,
                "subject": subject,
                "sender": sender,
                "date": date_hdr,
                "category": cls.category.value,
                "action": action,
                "confidence": round(conf, 2),
                "reason": cls.reason,
            }
            results.append(entry)

            if action == "trash":
                auto_trash.append(entry)
            elif action == "flag":
                flagged.append(entry)
            else:
                kept.append(entry)

        except Exception as e:
            print(f"  [{uid}] ERROR: {e}")

    # Print results
    print(f"  ┌─ AUTO-TRASH ({len(auto_trash)} emails) ─ high confidence")
    print(f"  │")
    for e in auto_trash:
        print(f"  │ 🔴 [{e['uid']:>4}] conf={e['confidence']:.2f}  {e['sender'][:35]:<35}  {e['subject'][:40]}")
    if not auto_trash:
        print(f"  │ (none)")

    print(f"  │")
    print(f"  ├─ FLAG FOR REVIEW ({len(flagged)} emails) ─ medium confidence")
    print(f"  │")
    for e in flagged:
        print(f"  │ 🟡 [{e['uid']:>4}] conf={e['confidence']:.2f}  {e['sender'][:35]:<35}  {e['subject'][:40]}")
    if not flagged:
        print(f"  │ (none)")

    print(f"  │")
    print(f"  └─ KEEP ({len(kept)} emails) ─ low confidence / protected")
    print(f"    ")
    for e in kept:
        print(f"    🟢 [{e['uid']:>4}] conf={e['confidence']:.2f}  {e['sender'][:35]:<35}  {e['subject'][:40]}")
    if not kept:
        print(f"    (none)")

    print(f"\n  SUMMARY: {len(auto_trash)} trash | {len(flagged)} flag | {len(kept)} keep")
    return results


def main():
    env = _load_env()
    user_email = env.get("GMAIL_USER_EMAIL", "")
    app_password = env.get("GMAIL_APP_PASSWORD", "")
    imap_host = env.get("GMAIL_IMAP_HOST", "imap.gmail.com")

    if not user_email or not app_password:
        print("[ERROR] GMAIL_USER_EMAIL or GMAIL_APP_PASSWORD not set in .env")
        return

    print(f"[scan_tabs] Connecting to {imap_host} as {user_email}...")
    conn = imaplib.IMAP4_SSL(imap_host)
    conn.login(user_email, app_password)

    all_results = []
    for tab_name, label in TAB_LABELS.items():
        results = scan_tab(conn, tab_name, label, max_emails=50)
        all_results.extend(results)

    conn.logout()

    # Save full results
    out_path = ROOT / "tab_analysis.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, indent=2, ensure_ascii=False)

    # Grand summary
    total = len(all_results)
    trash_count = sum(1 for r in all_results if r["action"] == "trash")
    flag_count = sum(1 for r in all_results if r["action"] == "flag")
    keep_count = sum(1 for r in all_results if r["action"] == "keep")

    print(f"\n{'='*60}")
    print(f"  GRAND SUMMARY — ALL 3 TABS")
    print(f"{'='*60}")
    print(f"  Total emails scanned: {total}")
    print(f"  🔴 Auto-trash:  {trash_count}")
    print(f"  🟡 Flag review: {flag_count}")
    print(f"  🟢 Keep:        {keep_count}")
    print(f"{'='*60}")
    print(f"\n  Results saved to: {out_path}")


if __name__ == "__main__":
    main()
