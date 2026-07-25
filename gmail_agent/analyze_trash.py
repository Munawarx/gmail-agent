"""analyze_trash.py — Read and deeply analyze all emails in Gmail Trash.

Connects via IMAP, reads every email in [Gmail]/Trash, extracts:
  - Sender name, email, domain
  - Subject, body, HTML structure
  - Links, tracking URLs
  - Marketing language, intent, category
  - Attachments info
  - Metadata

Outputs a detailed analysis JSON for the learning engine.
"""
import imaplib
import email
import json
import re
import os
from email.header import decode_header as _decode_header_raw
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent


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


def _extract_email_addr(sender):
    """Extract bare email from 'Name <user@domain>'."""
    s = sender.lower()
    if "<" in s and ">" in s:
        s = s[s.rfind("<") + 1:s.rfind(">")]
    return s.strip().rstrip(">")


def _extract_domain(sender):
    addr = _extract_email_addr(sender)
    if "@" in addr:
        return addr.split("@", 1)[1]
    return ""


def _extract_sender_name(sender):
    """Extract display name from 'Name <user@domain>'."""
    if "<" in sender:
        return sender[:sender.rfind("<")].strip().strip('"')
    return ""


def _extract_text(msg):
    """Extract plain text body from email message."""
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
        try:
            return str(msg.get_payload())
        except Exception:
            return ""


def _extract_html(msg):
    """Extract HTML body if present."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    return ""
    return ""


def _extract_links(html):
    """Extract all URLs from HTML body."""
    if not html:
        return []
    urls = re.findall(r'href=["\']([^"\']+)["\']', html, re.I)
    return list(set(urls))


def _detect_tracking_urls(links):
    """Detect tracking/marketing URLs."""
    tracking_indicators = [
        "track", "click", "redirect", "pixel", "open.rate",
        "utm_", "mailchimp", "sendgrid", "amazonses", "campaign",
        "engage", "ct.", "elink", "r20", "newsletter",
    ]
    tracking = []
    for url in links:
        lower = url.lower()
        if any(t in lower for t in tracking_indicators):
            tracking.append(url)
    return tracking


def _detect_attachments(msg):
    """Detect attachment count and types."""
    attachments = []
    if msg.is_multipart():
        for part in msg.walk():
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp.lower():
                fname = part.get_filename() or "unknown"
                ctype = part.get_content_type()
                attachments.append({"filename": fname, "type": ctype})
    return attachments


def _analyze_marketing_language(text, subject, html):
    """Detect marketing/promotional language patterns."""
    combined = f"{subject} {text}".lower()
    marketing_signals = {
        "promo_words": [],
        "urgency_words": [],
        "cta_words": [],
        "personalization": [],
    }
    promo_keywords = [
        "sale", "discount", "offer", "deal", "coupon", "promo", "% off",
        "save", "limited time", "exclusive", "members only", "reward",
        "bonus", "cashback", "free", "giveaway", "contest",
    ]
    urgency_keywords = [
        "hurry", "ends soon", "last chance", "today only", "expires",
        "don't miss", "act now", "final hours", "closing", "deadline",
    ]
    cta_keywords = [
        "shop now", "buy now", "subscribe", "sign up", "get started",
        "learn more", "click here", "download", "upgrade", "join",
        "claim", "redeem", "order now", "book now",
    ]
    personalization_keywords = [
        "hi ", "dear ", "your ", "you ", "for you", "exclusive",
        "personalized", "recommended", "based on your",
    ]
    for kw in promo_keywords:
        if kw in combined:
            marketing_signals["promo_words"].append(kw)
    for kw in urgency_keywords:
        if kw in combined:
            marketing_signals["urgency_words"].append(kw)
    for kw in cta_keywords:
        if kw in combined:
            marketing_signals["cta_words"].append(kw)
    for kw in personalization_keywords:
        if kw in combined:
            marketing_signals["personalization"].append(kw)
    return marketing_signals


def _detect_intent(subject, text, sender, domain):
    """Classify the email's intent."""
    combined = f"{subject} {text}".lower()
    if any(w in combined for w in ["unsubscribe", "newsletter", "digest", "weekly", "monthly update"]):
        return "newsletter"
    if any(w in combined for w in ["sale", "discount", "% off", "deal", "coupon", "offer"]):
        return "promotion"
    if any(w in combined for w in ["connection request", "new message", "someone viewed", "job"]):
        return "social_notification"
    if any(w in combined for w in ["order", "shipped", "delivered", "invoice", "receipt"]):
        return "transactional"
    if any(w in combined for w in ["verify", "code", "otp", "authentication", "login"]):
        return "security"
    if any(w in combined for w in ["welcome", "get started", "onboarding", "activate"]):
        return "onboarding"
    if any(w in combined for w in ["ai", "tool", "platform", "solution", "demo", "pricing"]):
        return "product_marketing"
    if any(w in combined for w in ["recruit", "opportunity", "position", "role", "candidate"]):
        return "recruitment"
    return "marketing_general"


def _detect_html_structure(html):
    """Analyze HTML template structure."""
    if not html:
        return {"has_html": False, "complexity": "none", "img_count": 0, "has_unsubscribe": False}
    img_count = len(re.findall(r'<img\s', html, re.I))
    table_count = len(re.findall(r'<table', html, re.I))
    has_unsubscribe = "unsubscribe" in html.lower()
    has_header_banner = bool(re.search(r'<img[^>]*header|banner|hero', html, re.I))
    if table_count > 5 or img_count > 10:
        complexity = "high"
    elif table_count > 2 or img_count > 3:
        complexity = "medium"
    else:
        complexity = "low"
    return {
        "has_html": True,
        "complexity": complexity,
        "img_count": img_count,
        "table_count": table_count,
        "has_unsubscribe": has_unsubscribe,
        "has_header_banner": has_header_banner,
    }


def analyze_trash():
    """Main: connect, read all trash emails, return analysis."""
    env = _load_env()
    user_email = env.get("GMAIL_USER_EMAIL", "")
    app_password = env.get("GMAIL_APP_PASSWORD", "")
    imap_host = env.get("GMAIL_IMAP_HOST", "imap.gmail.com")

    if not user_email or not app_password:
        print("[ERROR] GMAIL_USER_EMAIL or GMAIL_APP_PASSWORD not set in .env")
        return []

    print(f"[analyze_trash] Connecting to {imap_host} as {user_email}...")
    conn = imaplib.IMAP4_SSL(imap_host)
    conn.login(user_email, app_password)

    # Select Trash folder
    status, data = conn.select("[Gmail]/Trash")
    if status != "OK":
        print(f"[ERROR] Cannot select Trash: {data}")
        conn.logout()
        return []

    count = int(data[0])
    print(f"[analyze_trash] Found {count} emails in Trash")

    # Fetch all UIDs
    status, data = conn.search(None, "ALL")
    if status != "OK" or not data or not data[0]:
        print("[analyze_trash] No emails in Trash")
        conn.logout()
        return []

    uids = data[0].split()
    print(f"[analyze_trash] Analyzing {len(uids)} emails...")

    analyses = []
    for uid_bytes in uids:
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
            html = _extract_html(msg)
            links = _extract_links(html)
            tracking = _detect_tracking_urls(links)
            attachments = _detect_attachments(msg)
            marketing = _analyze_marketing_language(body, subject, html)
            intent = _detect_intent(subject, body, sender, _extract_domain(sender))
            html_struct = _detect_html_structure(html)

            addr = _extract_email_addr(sender)
            domain = _extract_domain(sender)
            name = _extract_sender_name(sender)

            analysis = {
                "uid": uid,
                "subject": subject,
                "sender_name": name,
                "sender_email": addr,
                "sender_domain": domain,
                "date": date_hdr,
                "body_preview": body[:500] if body else "",
                "body_length": len(body),
                "html_structure": html_struct,
                "links": links[:10],
                "link_count": len(links),
                "tracking_urls": tracking[:5],
                "tracking_url_count": len(tracking),
                "has_tracking": len(tracking) > 0,
                "attachments": attachments,
                "attachment_count": len(attachments),
                "marketing_language": marketing,
                "intent": intent,
                "analyzed_at": datetime.now().isoformat(),
            }
            analyses.append(analysis)
            print(f"  [{uid}] {sender[:40]} | {subject[:50]} | intent={intent}")

        except Exception as e:
            print(f"  [{uid}] ERROR: {e}")
            analyses.append({"uid": uid, "error": str(e)})

    conn.logout()
    print(f"\n[analyze_trash] Done. {len(analyses)} emails analyzed.")
    return analyses


if __name__ == "__main__":
    results = analyze_trash()
    out_path = ROOT / "trash_analysis.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"[analyze_trash] Saved to {out_path}")
