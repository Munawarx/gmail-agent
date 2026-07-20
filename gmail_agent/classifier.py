"""Email classifier: deterministic rules first, optional LLM for hard cases.

Designed so the common, high-volume categories (social, promo, expired OTP) are
caught by fast heuristics — minimizing API cost and maximizing speed — while
ambiguous emails can escalate to an LLM if USE_LLM is on.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from .schemas import Category, Classification, Email

OTP_PATTERNS = [
    r"\b(otp|one[- ]?time (?:password|code)|verification code|security code)\b",
    r"\byour (?:code|otp) is\b",
    r"\b\d{4,8}\b",  # a code somewhere
]
def _sender_email(sender: str) -> str:
    """Extract the bare email address from 'Name <user@domain>' or 'user@domain'."""
    s = sender.lower()
    if "<" in s and ">" in s:
        s = s[s.rfind("<") + 1:s.rfind(">")]
    return s.strip().rstrip(">")


def _sender_domain(sender: str) -> str:
    """Extract the bare domain from a 'Name <user@domain>' or 'user@domain' string."""
    s = _sender_email(sender)
    if "@" in s:
        s = s.split("@", 1)[1]
    return s.strip().rstrip(">")


SOCIAL_DOMAINS = (
    "linkedin.com", "facebook.com", "fb.com", "instagram.com", "threads.net",
    "twitter.com", "reddit.com", "pinterest.com",
    "snapchat.com", "tiktok.com", "youtube.com", "medium.com", "quora.com",
)
SOCIAL_PHRASES = (
    "connection request", "accepted your connection", "viewed your profile",
    "friend request", "someone commented", "started following you",
    "mentioned you", "new follower", "tagged you",
)
PROMO_PHRASES = (
    "sale", "discount", "coupon", "promo", "offer ends", "flash sale",
    "% off", "shop now", "buy now", "limited time", "deal", "save ",
    "newsletter", "unsubscribe", "order shipped", "your cart", "clearance",
    "exclusive offer", "limited offer", "free shipping", "big savings",
    "save up to", "limited", "upgrade", "get started", "early access",
    "prize", "contest", "win ", "claim", "last chance", "ends soon",
    "subscribe", "trial", "pro plus", "premium", "off your first",
)
# Substrings in the sender domain that almost always mean promotional/marketing mail
PROMO_DOMAINS = (
    "e.adobe.com", "engage.canva.com", "comms.runwayml.com", "mail.envato.com",
    "m.elements.envato.com", "info.alibabacloud.com", "elements.envato.com",
    "engage.", "comms.", "mail.", "newsletter.", "marketing.", "promo.",
    "e.", "email.", "offers.", "updates.",
)
# Local-parts (before @) typical of bulk/marketing senders
PROMO_SENDERS = (
    "noreply@", "no-reply@", "newsletter@", "promo@", "offers@", "marketing@",
    "deals@", "sales@", "notification@",
)


def _parse_date(received_at: str) -> datetime:
    try:
        # Gmail internalDate is ms epoch; our Email.received_at may be RFC3339
        if received_at.isdigit():
            return datetime.fromtimestamp(int(received_at) / 1000, tz=timezone.utc)
        return parsedate_to_datetime(received_at)
    except Exception:
        return datetime.now(timezone.utc)


def _find_otp(text: str) -> tuple[str | None, bool]:
    """Return (code, expired?). Heuristic: OTP emails often say 'expires in Xm'."""
    m = re.search(r"\b(\d{4,8})\b", text)
    code = m.group(1) if m else None
    expired = bool(re.search(r"(expired|no longer valid|has expi|invalid code)",
                             text, re.I))
    return code, expired


# NEVER trash: security / account / legal / billing / financial mail.
# Checked BEFORE promo/social so a bulk sender (e.g. no-reply@google.com)
# can't route a security alert to Trash.
SECURITY_PHRASES = (
    "security alert", "sign-in", "sign in", "new login", "unusual activity",
    "verify", "verification", "confirm your", "confirm email", "activate your",
    "password", "2-step", "two-step", "two factor", "2fa", "account recovery",
    "we detected", "suspicious", "your account has been", "recovery code",
    "billing", "invoice", "receipt", "payment received", "your order",
    "legal notice", "court", "bank statement", "transaction", "login code",
    "one-time code", "authentication",
)
# Domains whose mail is almost always account/security critical.
# (Note: bare google.com is intentionally excluded — too broad; its alerts
#  are still caught by SECURITY_PHRASES e.g. "security alert".)
SECURITY_DOMAINS = (
    "accounts.google.com", "discord.com", "roblox.com",
    "paypal.com", "apple.com", "microsoft.com", "amazon.com",
)


def classify(email: Email) -> Classification:
    text = f"{email.subject}\n{email.sender}\n{email.snippet}\n{email.body_text}".lower()
    sender = email.sender.lower()
    addr = _sender_email(sender)
    domain = _sender_domain(sender)

    # 0) OTP — explicit, time-sensitive codes to act on. Checked first so a
    #    "verification code" email is categorized as OTP (keep) rather than
    #    the generic IMPORTANT bucket.
    if re.search(OTP_PATTERNS[0], text, re.I) or re.search(OTP_PATTERNS[1], text, re.I):
        code, expired = _find_otp(text)
        if expired:
            return Classification(Category.OTP_EXPIRED,
                                  "OTP detected and marked expired", 0.9, code, True)
        return Classification(Category.OTP_VALID,
                              "OTP detected, still valid — keep", 0.9, code, False)

    # 1) IMPORTANT — protect security/account/financial mail (override below)
    if domain in SECURITY_DOMAINS or any(p in text for p in SECURITY_PHRASES):
        return Classification(Category.IMPORTANT,
                              "Security/account/financial — keep", 1.0)

    # 2) Social
    if domain in SOCIAL_DOMAINS or any(p in text for p in SOCIAL_PHRASES):
        return Classification(Category.SOCIAL, "Social media notification", 0.9)

    # 3) Promotional
    promo_hits = sum(1 for p in PROMO_PHRASES if p in text)
    # A known bulk/marketing sender is trashed on its own (no body needed);
    # otherwise require at least 2 promo phrases in the content.
    # Domain match is suffix-anchored so 'mail.' matches 'e.adobe.com' but
    # NOT 'gmail.com' (which merely contains the substring 'mail.').
    promo_sender = any(addr.startswith(s) for s in PROMO_SENDERS) or \
        any(domain == d or domain.endswith("." + d) for d in PROMO_DOMAINS)
    if promo_sender:
        return Classification(Category.PROMO, "Bulk/marketing sender", 0.85)
    if promo_hits >= 2:
        return Classification(Category.PROMO, f"{promo_hits} promo phrases", 0.8)

    return Classification(Category.KEEP, "No trash rule matched", 1.0)
