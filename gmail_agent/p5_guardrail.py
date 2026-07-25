"""P5 Protected Categories — Hard guardrail against trashing important emails.

Per P5.md spec: Never classify as Trash if the email plausibly falls into:
  - Banking or financial notices
  - Government or tax communications
  - Security/auth/password-reset alerts
  - Medical communications
  - Job-related email
  - Legal notices
  - Purchase receipts
  - Travel confirmations
  - Personal correspondence (back-and-forth threads)

When uncertain → "Flag for manual review", never "Trash".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class ProtectionResult:
    is_protected: bool
    category: str = ""
    reason: str = ""


# ═══ Protected sender patterns ═══
# These are checked against the full sender string (email + display name)

PROTECTED_DOMAINS = {
    # Banking / Financial
    "bank", "chase.com", "wellsfargo.com", "bankofamerica.com", "citi.com",
    "capitalone.com", "americanexpress.com", "discover.com",
    "paypal.com", "venmo.com", "zelle.com", "wise.com", "revolut.com",
    "stripe.com", "square.com", "intuit.com", "quickbooks.com",
    "irs.gov", "tax", "treasury", "socialsecurity.gov", "ssa.gov",
    # Government
    "gov", "uscis.gov", "dmv", "va.gov", "medicare.gov", "irs.gov",
    # Security
    "accounts.google.com", "security.google.com", "iforgot", "password-reset",
    "2fa", "auth", "login-alert", "security-alert", "verify",
    # Medical
    "health", "medical", "doctor", "hospital", "clinic", "pharmacy",
    "cvs.com", "walgreens.com", "epic", "mychart",
    # Legal
    "legal", "lawfirm", "attorney", "court", "judiciary",
    # Travel
    "airline", "united.com", "delta.com", "americanair", "southwest.com",
    "expedia.com", "booking.com", "airbnb.com", "uber.com", "lyft.com",
    "hotels.com", "marriott", "hilton.com",
}

PROTECTED_SENDER_LOCALPARTS = {
    "security", "no-reply-security", "alerts", "fraud", "billing",
    "invoice", "receipts", "receipt", "payment", "statements",
    "noreply-billing", "accounts", "verify", "verification",
}

PROTECTED_KEYWORDS_SUBJECT = [
    # Financial
    r"\b(invoice|receipt|payment|statement|transaction|deposit|withdrawal)\b",
    r"\b(transfer|funds|balance|refund|charge|billing)\b",
    r"\b(tax|w-?2|1099|taxes|deduction)\b",
    # Security
    r"\b(security|alert|verify|verification|password|reset|login)\b",
    r"\b(auth|2fa|two-factor|code|otp|passcode)\b",
    r"\b(new (device|location|browser)|suspicious activity)\b",
    # Account
    r"\b(account|profile|settings|confirm|confirmation)\b",
    r"\b(welcome to|sign.?up|activation|activate)\b",
    # Travel
    r"\b(flight|boarding|reservation|booking|itinerary|check.?in)\b",
    r"\b(hotel|rental|car|trip|travel)\b",
    # Legal/Official
    r"\b(legal|notice|subpoena|court|hearing|compliance)\b",
    r"\b(terms|privacy|policy|agreement|contract)\b",
    # Medical
    r"\b(appointment|prescription|lab|test results|doctor|medical)\b",
    # Job
    r"\b(interview|job|offer|employment|resume|application)\b",
    # Personal correspondence (re: / fwd: threads)
    r"^(re|fwd|fw):",
]

PROTECTED_KEYWORDS_BODY = [
    r"\b(invoice|receipt|order #|order number|tracking number)\b",
    r"\b(account number|routing number|last 4 digits)\b",
    r"\b(your (password|code|otp|verification code))\b",
    r"\b(do not share|do not reply to this email if you did not)\b",
    r"\b(confirmation code|booking reference|reservation number)\b",
]


def check_protection(sender: str, subject: str, body: str = "") -> ProtectionResult:
    """Check if an email falls into a protected category.

    Returns ProtectionResult(is_protected=True, category=..., reason=...) if protected.
    """
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    body_lower = body.lower() if body else ""

    # Extract domain from sender
    domain = ""
    if "@" in sender_lower:
        parts = sender_lower.split("@")
        if len(parts) >= 2:
            domain = parts[-1].split(">")[0].strip().strip('"').strip("'")

    # Extract local part
    local_part = ""
    if "@" in sender_lower:
        local_part = sender_lower.split("@")[0].strip().strip('"').strip("'")

    # ── Check 1: Protected domain ──
    for pd in PROTECTED_DOMAINS:
        if pd in domain or pd in sender_lower:
            return ProtectionResult(
                is_protected=True,
                category="financial_gov_security",
                reason=f"Protected domain match: '{pd}' in sender domain",
            )

    # ── Check 2: Protected sender local-part ──
    for lp in PROTECTED_SENDER_LOCALPARTS:
        if lp in local_part:
            return ProtectionResult(
                is_protected=True,
                category="security_billing",
                reason=f"Protected sender type: '{lp}' in local part",
            )

    # ── Check 3: Protected subject keywords ──
    for pattern in PROTECTED_KEYWORDS_SUBJECT:
        if re.search(pattern, subject_lower, re.IGNORECASE):
            return ProtectionResult(
                is_protected=True,
                category="protected_subject",
                reason=f"Protected keyword in subject: matched /{pattern}/",
            )

    # ── Check 4: Protected body keywords (only if body provided) ──
    if body_lower:
        for pattern in PROTECTED_KEYWORDS_BODY:
            if re.search(pattern, body_lower[:2000], re.IGNORECASE):
                return ProtectionResult(
                    is_protected=True,
                    category="protected_body",
                    reason=f"Protected keyword in body: matched /{pattern}/",
                )

    return ProtectionResult(is_protected=False)


def classify_confidence(
    confidence: float,
    confidence_trash: float = 0.80,
    confidence_flag: float = 0.55,
) -> str:
    """Map confidence score to action using P5 thresholds.

    Returns 'trash', 'flag', or 'keep'.
    """
    if confidence >= confidence_trash:
        return "trash"
    elif confidence >= confidence_flag:
        return "flag"
    else:
        return "keep"
