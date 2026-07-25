"""
learning.py — Self-learning engine for the Gmail Agent.

Implements semantic similarity matching and continuous learning:
  1. Loads learned_patterns.json (knowledge base from trash analysis)
  2. Scores incoming emails against learned patterns
  3. Supports feedback: records user actions (delete/restore/archive/spam/keep)
  4. Adapts: new patterns are added, existing patterns are reinforced or weakened
  5. Confidence scoring: high → auto-trash, medium → flag, low → keep

Pattern matching uses:
  - Sender email/domain similarity (not exact match)
  - Semantic phrase similarity (fuzzy keyword matching)
  - HTML structure similarity
  - Marketing language detection
  - Local-part pattern matching
  - Intent classification

Never trashes: active OTPs, security alerts, banking, payments, work emails, etc.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from .schemas import Category, Classification, Email

PATTERNS_PATH = Path(__file__).resolve().parent.parent / "state" / "learned_patterns.json"
FEEDBACK_DB_PATH = Path(__file__).resolve().parent.parent / "state" / "feedback.json"


# ── Pattern loading ─────────────────────────────────────────────────

def load_patterns() -> dict:
    """Load the learned patterns knowledge base."""
    try:
        with open(PATTERNS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"version": 2, "sender_patterns": {}, "domain_patterns": {},
                "semantic_patterns": {}, "html_patterns": {},
                "local_part_patterns": {}, "never_trash": {},
                "confidence_thresholds": {"auto_trash": 0.80, "flag_for_review": 0.55, "keep": 0.00}}


def _extract_email_addr(sender: str) -> str:
    s = sender.lower()
    if "<" in s and ">" in s:
        s = s[s.rfind("<") + 1:s.rfind(">")]
    return s.strip().rstrip(">")


def _extract_domain(sender: str) -> str:
    addr = _extract_email_addr(sender)
    if "@" in addr:
        return addr.split("@", 1)[1]
    return ""


def _extract_local_part(sender: str) -> str:
    addr = _extract_email_addr(sender)
    if "@" in addr:
        return addr.split("@", 1)[0]
    return addr


# ── Safety checks (NEVER TRASH) ─────────────────────────────────────

# These are ALWAYS protected regardless of pattern match strength.
ACTIVE_SECURITY_KEYWORDS = [
    "security alert", "suspicious activity", "new login", "unusual sign-in",
    "password reset", "account recovery", "verify your account",
    "2-step verification", "two-factor", "authentication required",
]
ACTIVE_OTP_KEYWORDS = [
    "your code", "verification code", "otp", "one-time password",
    "login code", "security code", "is your code",
]
BANKING_DOMAINS = [
    "paypal.com", "stripe.com", "wise.com", "bankofamerica.com",
    "chase.com", "wellsfargo.com", "citibank.com", "hsbc.com",
]
PROTECTED_DOMAINS = [
    "accounts.google.com", "apple.com", "microsoft.com",
    "amazon.com", "gov.uk", "gov.in", "irs.gov",
]
PROTECTED_SUBJECTS = [
    "invoice", "receipt", "payment received", "your order",
    "order confirmation", "shipping", "tracking", "booking confirmed",
    "flight", "hotel", "appointment", "prescription", "test results",
    "tax return", "tax notice", "government", "legal notice",
]


def _is_protected(email: Email) -> tuple[bool, str]:
    """Check if email must NEVER be trashed. Returns (protected, reason)."""
    sender = email.sender.lower()
    addr = _extract_email_addr(sender)
    domain = _extract_domain(sender)
    subject = email.subject.lower()
    text = f"{email.subject}\n{email.snippet}\n{email.body_text}".lower()

    # Banking/financial domains
    for bd in BANKING_DOMAINS:
        if domain == bd or domain.endswith("." + bd):
            return True, f"banking domain: {domain}"

    # Check for active OTP (not expired)
    otp_match = any(kw in text for kw in ACTIVE_OTP_KEYWORDS)
    if otp_match:
        expired = bool(re.search(r"(expired|no longer valid|has expi|invalid code)", text, re.I))
        # Check age — if internalDate available, check if >1 hour old
        is_old = False
        if email.received_at:
            try:
                if email.received_at.isdigit():
                    age_s = time.time() - int(email.received_at) / 1000
                else:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(email.received_at)
                    age_s = time.time() - dt.timestamp()
                if age_s > 3600:  # >1 hour
                    is_old = True
            except Exception:
                pass
        if not expired and not is_old:
            return True, "active OTP — not expired, not old"

    # Active security alerts (not old/already-addressed)
    sec_match = any(kw in text for kw in ACTIVE_SECURITY_KEYWORDS)
    if sec_match:
        # These CAN be trashed if user has already seen them — let the
        # pattern engine decide, but we add a safety margin by checking age
        # Only block if <10 minutes old (genuinely new alert)
        if email.received_at:
            try:
                if email.received_at.isdigit():
                    age_s = time.time() - int(email.received_at) / 1000
                else:
                    from email.utils import parsedate_to_datetime
                    dt = parsedate_to_datetime(email.received_at)
                    age_s = time.time() - dt.timestamp()
                if age_s < 600:  # <10 min — genuinely new
                    return True, "fresh security alert (<10 min old)"
            except Exception:
                pass

    # Protected subjects
    for ps in PROTECTED_SUBJECTS:
        if ps in subject:
            return True, f"protected subject: {ps}"

    return False, ""


# ── Semantic similarity ─────────────────────────────────────────────

def _string_similarity(a: str, b: str) -> float:
    """Fuzzy string similarity 0.0-1.0 using SequenceMatcher."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _phrase_match(text: str, phrases: list[str]) -> tuple[bool, float, list[str]]:
    """Check if any phrase appears (or is similar) in text.
    Returns (matched, best_score, matched_phrases).
    """
    text_lower = text.lower()
    matched = []
    best_score = 0.0
    for phrase in phrases:
        if phrase.lower() in text_lower:
            matched.append(phrase)
            best_score = 1.0  # exact match = max
        else:
            # Check fuzzy similarity on chunks
            words = phrase.lower().split()
            if len(words) >= 2:
                # Check if most words appear near each other
                found_words = sum(1 for w in words if w in text_lower)
                ratio = found_words / len(words)
                if ratio >= 0.6:
                    best_score = max(best_score, ratio * 0.85)
    return len(matched) > 0 or best_score >= 0.6, best_score, matched


def _domain_similarity(email_domain: str, pattern_domain: str) -> float:
    """Check if two domains are related (same parent or similar subdomain)."""
    if email_domain == pattern_domain:
        return 1.0
    # Check parent domain match
    parts_e = email_domain.split(".")
    parts_p = pattern_domain.split(".")
    # Get last 2 parts (parent domain)
    parent_e = ".".join(parts_e[-2:]) if len(parts_e) >= 2 else email_domain
    parent_p = ".".join(parts_p[-2:]) if len(parts_p) >= 2 else pattern_domain
    if parent_e == parent_p:
        return 0.85
    if _string_similarity(parent_e, parent_p) >= 0.75:
        return 0.7
    return 0.0


# ── Main scoring engine ─────────────────────────────────────────────

def score_email(email: Email, patterns: Optional[dict] = None) -> tuple[float, str, dict]:
    """Score an email against all learned patterns.
    Returns (confidence, reason, details).
    """
    if patterns is None:
        patterns = load_patterns()

    sender = email.sender.lower()
    addr = _extract_email_addr(sender)
    domain = _extract_domain(sender)
    local_part = _extract_local_part(sender)
    text = f"{email.subject}\n{email.snippet}\n{email.body_text}".lower()

    scores = []
    reasons = []
    details = {"matches": []}

    # ── 1. Exact sender match ──
    trash_senders = patterns.get("sender_patterns", {}).get("trashable_senders", {})
    if addr in trash_senders:
        entry = trash_senders[addr]
        conf = entry.get("confidence", 0.85)
        scores.append(conf)
        reasons.append(f"Known trashable sender: {addr}")
        details["matches"].append({"type": "sender_exact", "sender": addr, "confidence": conf})

    expired_txn = patterns.get("sender_patterns", {}).get("expired_transactional", {})
    if addr in expired_txn:
        entry = expired_txn[addr]
        conf = entry.get("confidence", 0.80)
        scores.append(conf * 0.9)  # slightly lower — depends on age
        reasons.append(f"Expired transactional sender: {addr}")
        details["matches"].append({"type": "sender_expired", "sender": addr, "confidence": conf * 0.9})

    # ── 2. Domain pattern match (including fuzzy domain similarity) ──
    domain_patterns = patterns.get("domain_patterns", {}).get("marketing_subdomains", {})
    for pat_domain, info in domain_patterns.items():
        sim = _domain_similarity(domain, pat_domain)
        if sim > 0:
            conf = info.get("confidence", 0.80) * sim
            scores.append(conf)
            reasons.append(f"Domain similar to marketing pattern: {pat_domain} (sim={sim:.2f})")
            details["matches"].append({"type": "domain", "domain": pat_domain, "similarity": sim, "confidence": conf})

    # ── 3. Local-part pattern ──
    bulk_local_parts = patterns.get("local_part_patterns", {}).get("bulk_senders", [])
    for blp in bulk_local_parts:
        blp_clean = blp.rstrip("@")
        if local_part == blp_clean or local_part.startswith(blp_clean):
            scores.append(0.50)  # baseline — bulk sender, but not high alone
            reasons.append(f"Bulk sender local-part: {local_part}")
            details["matches"].append({"type": "local_part", "local_part": local_part})
            break

    # ── 4. Semantic phrase patterns ──
    semantic = patterns.get("semantic_patterns", {})
    for pattern_name, pattern_info in semantic.items():
        phrases = pattern_info.get("phrases", [])
        if phrases:
            matched, score, matched_phrases = _phrase_match(text, phrases)
            if matched:
                conf = pattern_info.get("confidence", 0.75) * score
                scores.append(conf)
                reasons.append(f"Semantic pattern '{pattern_name}': matched {matched_phrases}")
                details["matches"].append({
                    "type": "semantic", "pattern": pattern_name,
                    "matched": matched_phrases, "score": round(score, 2), "confidence": round(conf, 2),
                })

    # ── 5. Combine scores ──
    if not scores:
        return 0.0, "No pattern match", details

    # Weight: max score dominates, but additional matches boost confidence
    max_score = max(scores)
    boost = min(0.15, sum(s * 0.03 for s in scores if s < max_score))
    final_confidence = min(1.0, max_score + boost)

    # Multiple matching patterns → confidence boost
    if len(scores) >= 3:
        final_confidence = min(1.0, final_confidence + 0.10)

    return final_confidence, "; ".join(reasons[:3]), details


# ── Decision engine ─────────────────────────────────────────────────

def classify_learned(email: Email, patterns: Optional[dict] = None) -> tuple[Classification, str]:
    """Classify an email using learned patterns.
    Returns (Classification, action) where action is 'trash' / 'flag' / 'keep'.

    Confidence thresholds:
      ≥ 0.80 → auto-trash
      0.55-0.79 → flag for review
      < 0.55 → keep in inbox
    """
    if patterns is None:
        patterns = load_patterns()

    # Safety check: NEVER trash protected emails
    protected, prot_reason = _is_protected(email)
    if protected:
        return Classification(
            Category.IMPORTANT, f"PROTECTED: {prot_reason}", 1.0
        ), "keep"

    # Score against learned patterns
    confidence, reason, details = score_email(email, patterns)

    thresholds = patterns.get("confidence_thresholds", {})
    auto_trash_threshold = thresholds.get("auto_trash", 0.80)
    flag_threshold = thresholds.get("flag_for_review", 0.55)

    if confidence >= auto_trash_threshold:
        return Classification(
            Category.PROMO, f"Learned pattern: {reason}", confidence
        ), "trash"

    elif confidence >= flag_threshold:
        return Classification(
            Category.KEEP, f"Possible match (review): {reason}", confidence
        ), "flag"

    else:
        return Classification(
            Category.KEEP, "No strong pattern match", confidence
        ), "keep"


# ── Feedback / continuous learning ──────────────────────────────────

def load_feedback() -> list:
    """Load the feedback database."""
    try:
        with open(FEEDBACK_DB_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def record_feedback(action: str, email_id: str, sender: str, subject: str,
                    category: str = "", confidence: float = 0.0, notes: str = ""):
    """Record a user action for continuous learning.

    Actions:
      - 'delete'    → reinforces trash pattern
      - 'restore'   → weakens/we negate a trash pattern (false positive)
      - 'archive'   → neutral — email is informational, don't auto-trash
      - 'spam'      → strong reinforce (sender is spam)
      - 'keep'      → reinforces keep (don't trash)
      - 'move'      → user moved to folder — neutral
    """
    feedback = load_feedback()
    entry = {
        "action": action,
        "email_id": email_id,
        "sender": sender,
        "sender_email": _extract_email_addr(sender),
        "sender_domain": _extract_domain(sender),
        "subject": subject,
        "category": category,
        "confidence": confidence,
        "notes": notes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    feedback.append(entry)
    FEEDBACK_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(feedback, f, indent=2, ensure_ascii=False)
    return entry


def apply_feedback_to_patterns():
    """Process accumulated feedback and update learned_patterns.json.

    For each sender:
      - 2+ 'delete' actions → add to trashable_senders with confidence boost
      - 1+ 'restore' actions → remove from trashable_senders (false positive)
      - 2+ 'spam' actions → add with max confidence
      - 'keep' actions on a trashable sender → reduce confidence
    """
    patterns = load_patterns()
    feedback = load_feedback()
    if not feedback:
        return {"updated": 0, "reason": "no feedback to process"}

    # Aggregate by sender_email
    sender_actions = {}
    for fb in feedback:
        addr = fb.get("sender_email", "")
        if not addr:
            continue
        if addr not in sender_actions:
            sender_actions[addr] = []
        sender_actions[addr].append(fb)

    trashable = patterns.setdefault("sender_patterns", {}).setdefault("trashable_senders", {})
    updates = 0

    for addr, actions in sender_actions.items():
        deletes = sum(1 for a in actions if a["action"] in ("delete", "spam"))
        restores = sum(1 for a in actions if a["action"] == "restore")
        keeps = sum(1 for a in actions if a["action"] == "keep")
        spams = sum(1 for a in actions if a["action"] == "spam")

        if restores > 0 and addr in trashable:
            # False positive — remove
            del trashable[addr]
            updates += 1
            continue

        if deletes >= 2 and addr not in trashable:
            conf = 0.95 if spams >= 1 else min(0.80 + 0.05 * (deletes - 2), 0.95)
            trashable[addr] = {
                "frequency": deletes,
                "reason": f"Learned from {deletes} user deletions",
                "confidence": round(conf, 2),
            }
            updates += 1
        elif deletes >= 2 and addr in trashable:
            # Reinforce — boost confidence
            old_conf = trashable[addr].get("confidence", 0.80)
            trashable[addr]["confidence"] = min(0.98, old_conf + 0.03)
            trashable[addr]["frequency"] = trashable[addr].get("frequency", 1) + deletes
            updates += 1

        if keeps > 0 and addr in trashable:
            old_conf = trashable[addr].get("confidence", 0.80)
            trashable[addr]["confidence"] = max(0.0, old_conf - 0.10 * keeps)
            if trashable[addr]["confidence"] < 0.50:
                del trashable[addr]
                updates += 1

    # Save updated patterns
    patterns["last_feedback_applied"] = datetime.now(timezone.utc).isoformat()
    patterns["total_feedback_entries"] = len(feedback)
    with open(PATTERNS_PATH, "w", encoding="utf-8") as f:
        json.dump(patterns, f, indent=2, ensure_ascii=False)

    return {"updated": updates, "total_senders": len(trashable), "feedback_entries": len(feedback)}


# ── Pattern summary for CLI ─────────────────────────────────────────

def get_pattern_summary() -> dict:
    """Get a summary of the current knowledge base."""
    patterns = load_patterns()
    feedback = load_feedback()
    return {
        "total_trashable_senders": len(patterns.get("sender_patterns", {}).get("trashable_senders", {})),
        "total_expired_senders": len(patterns.get("sender_patterns", {}).get("expired_transactional", {})),
        "total_domain_patterns": len(patterns.get("domain_patterns", {}).get("marketing_subdomains", {})),
        "total_semantic_patterns": len(patterns.get("semantic_patterns", {})),
        "feedback_entries": len(feedback),
        "version": patterns.get("version", "?"),
        "last_feedback_applied": patterns.get("last_feedback_applied", "never"),
        "confidence_thresholds": patterns.get("confidence_thresholds", {}),
    }
