"""Fetch ALL emails from inbox, classify each, print full report.
Does NOT trash anything — analysis only (dry-run equivalent).
"""
import os, sys, json, imaplib, email
from email.header import decode_header
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env manually
env_path = Path(__file__).parent / ".env"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

USER = os.environ["GMAIL_USER_EMAIL"]
PASS = os.environ["GMAIL_APP_PASSWORD"]

def decode_hdr(value):
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for blob, enc in parts:
        if isinstance(blob, bytes):
            out.append(blob.decode(enc or "utf-8", "replace"))
        else:
            out.append(blob)
    return " ".join(out)

def extract_text(msg):
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", "replace")[:500]
                except:
                    return ""
    try:
        return msg.get_payload(decode=True).decode("utf-8", "replace")[:500]
    except:
        return ""

print(f"Connecting to Gmail IMAP as {USER}...")
conn = imaplib.IMAP4_SSL("imap.gmail.com")
conn.login(USER, PASS)
conn.select("INBOX")
status, data = conn.search(None, "ALL")
uids = [int(u) for u in data[0].split()]
print(f"Total emails in INBOX: {len(uids)}")
print("=" * 80)

emails_data = []
for uid in uids:
    status, raw = conn.fetch(str(uid).encode(), "(RFC822)")
    raw_bytes = None
    for part in raw:
        if isinstance(part, tuple):
            raw_bytes = part[1]
            break
    if not raw_bytes:
        continue
    msg = email.message_from_bytes(raw_bytes)
    subject = decode_hdr(msg.get("Subject", ""))
    sender = decode_hdr(msg.get("From", ""))
    date = msg.get("Date", "")
    body = extract_text(msg)
    emails_data.append({
        "uid": uid,
        "subject": subject,
        "sender": sender,
        "date": date,
        "body_preview": body[:200],
    })
    print(f"\n[UID {uid}] {date}")
    print(f"  FROM: {sender}")
    print(f"  SUBJ: {subject}")
    print(f"  BODY: {body[:150]}...")

conn.logout()

# Save to JSON for the classifier step
out_path = Path(__file__).parent / "scan_results.json"
out_path.write_text(json.dumps(emails_data, indent=2, ensure_ascii=False))
print(f"\n{'=' * 80}")
print(f"Saved {len(emails_data)} emails to {out_path}")
