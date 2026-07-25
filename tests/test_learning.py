"""Tests for the self-learning Gmail Agent (P4 spec).

Tests:
  1. Learning engine pattern scoring
  2. Semantic similarity matching
  3. Confidence thresholds (auto-trash / flag / keep)
  4. Protected email safety (never trash OTPs, banking, security)
  5. Feedback loop (delete/restore/keep → pattern updates)
  6. Classifier integration with learning
  7. Pattern knowledge base loading
"""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure we can import the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_agent.schemas import Category, Classification, Email
from gmail_agent.learning import (
    score_email, classify_learned, _is_protected, _phrase_match,
    _domain_similarity, _string_similarity, load_patterns,
    record_feedback, apply_feedback_to_patterns, load_feedback,
    PATTERNS_PATH, FEEDBACK_DB_PATH,
)
from gmail_agent.classifier import classify_with_learning


# ── Test helpers ────────────────────────────────────────────────────

def make_email(sender="test@example.com", subject="Test", body="Hello",
               snippet="", received_at=""):
    return Email(
        id="test123", thread_id="t1", subject=subject,
        sender=sender, received_at=received_at,
        snippet=snippet or body[:100], body_text=body,
    )


# ── 1. Pattern scoring ──────────────────────────────────────────────

class TestPatternScoring:
    def test_known_trashable_sender(self):
        """Vidiq newsletters should score high (user trashed 4 of them)."""
        email = make_email(
            sender="Rob Wilson <support@email.vidiq.com>",
            subject="You reached 10,000 views",
            body="Your channel performance update...",
        )
        conf, reason, details = score_email(email)
        assert conf >= 0.80, f"Expected ≥0.80 for vidiq, got {conf}"
        assert "trashable" in reason.lower() or "sender" in reason.lower()

    def test_unknown_sender_low_score(self):
        """Random new sender should score low."""
        email = make_email(
            sender="friend@gmail.com",
            subject="Lunch tomorrow?",
            body="Hey, want to grab lunch?",
        )
        conf, reason, details = score_email(email)
        assert conf < 0.55, f"Expected <0.55 for random sender, got {conf}"

    def test_marketing_newsletter_pattern(self):
        """Alibaba Cloud newsletter should match domain/semantic patterns."""
        email = make_email(
            sender="Alibaba Cloud <newsletter@info.alibabacloud.com>",
            subject="Activate your welcome gift and start exploring",
            body="Start your journey with Alibaba Cloud...",
        )
        conf, reason, details = score_email(email)
        assert conf >= 0.55, f"Expected ≥0.55 for Alibaba newsletter, got {conf}: {reason}"


# ── 2. Semantic similarity ──────────────────────────────────────────

class TestSemanticSimilarity:
    def test_exact_phrase_match(self):
        matched, score, phrases = _phrase_match(
            "You reached 8,500 views on your channel",
            ["you reached", "views", "your channel"],
        )
        assert matched
        assert score == 1.0

    def test_fuzzy_phrase_match(self):
        matched, score, phrases = _phrase_match(
            "Your performance stats are up",
            ["your performance", "your stats"],
        )
        assert matched
        assert score >= 0.6

    def test_no_match(self):
        matched, score, phrases = _phrase_match(
            "Hello world",
            ["verification code", "security alert"],
        )
        assert not matched

    def test_domain_exact_match(self):
        assert _domain_similarity("email.vidiq.com", "email.vidiq.com") == 1.0

    def test_domain_parent_match(self):
        assert _domain_similarity("mail.vidiq.com", "email.vidiq.com") >= 0.85

    def test_domain_no_match(self):
        assert _domain_similarity("gmail.com", "vidiq.com") < 0.3


# ── 3. Confidence thresholds ────────────────────────────────────────

class TestConfidenceThresholds:
    def test_high_confidence_auto_trash(self):
        """High confidence sender → auto-trash."""
        email = make_email(
            sender="<support@email.vidiq.com>",
            subject="You reached 9,000 views",
            body="Performance update",
        )
        cls, action = classify_learned(email)
        assert action == "trash", f"Expected trash, got {action}"
        assert cls.confidence >= 0.80

    def test_low_confidence_keep(self):
        """Random email → keep."""
        email = make_email(
            sender="mom@gmail.com",
            subject="Happy birthday!",
            body="Love you!",
        )
        cls, action = classify_learned(email)
        assert action == "keep", f"Expected keep, got {action}"


# ── 4. Protected emails (NEVER TRASH) ───────────────────────────────

class TestProtectedEmails:
    def test_active_otp_protected(self):
        """Active OTP (<1hr) must NEVER be trashed."""
        import time
        ts_ms = str(int(time.time() * 1000))  # now
        email = make_email(
            sender="TikTok <register@account.tiktok.com>",
            subject="563241 is your code to log in to TikTok",
            body="Your verification code is 563241",
            received_at=ts_ms,
        )
        protected, reason = _is_protected(email)
        assert protected, f"Active OTP should be protected: {reason}"

    def test_banking_protected(self):
        """Banking emails must NEVER be trashed."""
        email = make_email(
            sender="PayPal <service@paypal.com>",
            subject="Payment received: $500",
            body="You received a payment",
        )
        protected, reason = _is_protected(email)
        assert protected, f"Banking should be protected: {reason}"

    def test_expired_otp_not_protected(self):
        """Expired OTP (>1hr old) is NOT protected — can be trashed."""
        import time
        ts_ms = str(int((time.time() - 7200) * 1000))  # 2 hours ago
        email = make_email(
            sender="TikTok <register@account.tiktok.com>",
            subject="563241 is your code",
            body="Your code is 563241. It expires in 5 minutes.",
            received_at=ts_ms,
        )
        protected, reason = _is_protected(email)
        assert not protected, "Expired OTP should NOT be protected"

    def test_fresh_security_alert_protected(self):
        """Fresh security alert (<10 min) must be protected."""
        import time
        ts_ms = str(int(time.time() * 1000))  # now
        email = make_email(
            sender="Google <no-reply@accounts.google.com>",
            subject="Security alert: new login",
            body="We detected a new sign-in on your account",
            received_at=ts_ms,
        )
        protected, reason = _is_protected(email)
        assert protected, f"Fresh security alert should be protected: {reason}"

    def test_invoice_protected(self):
        """Invoices must NEVER be trashed."""
        email = make_email(
            sender="Stripe <receipts@stripe.com>",
            subject="Invoice #12345 - Payment receipt",
            body="Thank you for your payment",
        )
        protected, reason = _is_protected(email)
        assert protected, f"Invoice should be protected: {reason}"


# ── 5. Feedback loop ────────────────────────────────────────────────

class TestFeedbackLoop:
    def test_record_and_load_feedback(self, tmp_path):
        """Record a feedback action and verify it's stored."""
        with patch("gmail_agent.learning.FEEDBACK_DB_PATH", tmp_path / "feedback.json"):
            record_feedback("delete", "msg1", "spam@promo.com", "Buy now!")
            feedback = load_feedback()
            assert len(feedback) == 1
            assert feedback[0]["action"] == "delete"
            assert feedback[0]["sender_email"] == "spam@promo.com"

    def test_apply_feedback_adds_pattern(self, tmp_path):
        """2+ deletes → sender added to trashable patterns."""
        # Copy real patterns to temp
        patterns_path = tmp_path / "learned_patterns.json"
        real_patterns = load_patterns()
        with open(patterns_path, "w") as f:
            json.dump(real_patterns, f)

        feedback_path = tmp_path / "feedback.json"

        with patch("gmail_agent.learning.PATTERNS_PATH", patterns_path), \
             patch("gmail_agent.learning.FEEDBACK_DB_PATH", feedback_path):
            # Record 2 deletes for same sender
            record_feedback("delete", "m1", "newsletters@newpromo.com", "Sale!")
            record_feedback("delete", "m2", "newsletters@newpromo.com", "Deal!")
            result = apply_feedback_to_patterns()

            patterns = load_patterns()
            trashable = patterns["sender_patterns"]["trashable_senders"]
            assert "newsletters@newpromo.com" in trashable, \
                "Sender with 2 deletes should be in trashable patterns"

    def test_restore_removes_false_positive(self, tmp_path):
        """Restore action → removes sender from trashable (false positive)."""
        patterns_path = tmp_path / "learned_patterns.json"
        real_patterns = load_patterns()
        # Add a test sender to trashable
        real_patterns.setdefault("sender_patterns", {}).setdefault("trashable_senders", {})
        real_patterns["sender_patterns"]["trashable_senders"]["test@falsepos.com"] = {
            "frequency": 1, "reason": "test", "confidence": 0.85
        }
        with open(patterns_path, "w") as f:
            json.dump(real_patterns, f)

        feedback_path = tmp_path / "feedback.json"

        with patch("gmail_agent.learning.PATTERNS_PATH", patterns_path), \
             patch("gmail_agent.learning.FEEDBACK_DB_PATH", feedback_path):
            record_feedback("restore", "m1", "test@falsepos.com", "Important!")
            apply_feedback_to_patterns()

            patterns = load_patterns()
            trashable = patterns["sender_patterns"]["trashable_senders"]
            assert "test@falsepos.com" not in trashable, \
                "Restored sender should be removed from trashable"


# ── 6. Classifier integration ───────────────────────────────────────

class TestClassifierIntegration:
    def test_classify_with_learning_trash(self):
        """Full pipeline: known trashable sender → trash action."""
        email = make_email(
            sender="Rob Wilson <support@email.vidiq.com>",
            subject="You reached 8,500 views",
            body="Your channel update",
        )
        cls, action = classify_with_learning(email, settings={"use_llm": False})
        assert action == "trash", f"Expected trash for vidiq, got {action}"

    def test_classify_with_learning_keep(self):
        """Full pipeline: personal email → keep."""
        email = make_email(
            sender="friend@gmail.com",
            subject="Movie tonight?",
            body="Want to see a movie?",
        )
        cls, action = classify_with_learning(email, settings={"use_llm": False})
        assert action == "keep", f"Expected keep for personal email, got {action}"

    def test_classify_with_learning_protected(self):
        """Full pipeline: OTP → protected, never trashed."""
        import time
        ts_ms = str(int(time.time() * 1000))
        email = make_email(
            sender="Discord <noreply@discord.com>",
            subject="Verify Email Address for Discord",
            body="Your verification code is 839204",
            received_at=ts_ms,
        )
        cls, action = classify_with_learning(email, settings={"use_llm": False})
        assert action == "keep", f"Active OTP should be kept, got {action}"
        assert cls.category == Category.OTP_VALID or cls.category == Category.IMPORTANT


# ── 7. Pattern knowledge base ───────────────────────────────────────

class TestPatternKnowledgeBase:
    def test_patterns_file_exists(self):
        assert PATTERNS_PATH.exists(), f"learned_patterns.json not found at {PATTERNS_PATH}"

    def test_patterns_have_senders(self):
        patterns = load_patterns()
        trashable = patterns.get("sender_patterns", {}).get("trashable_senders", {})
        assert len(trashable) > 0, "Should have learned trashable senders from trash analysis"

    def test_patterns_have_thresholds(self):
        patterns = load_patterns()
        thresholds = patterns.get("confidence_thresholds", {})
        assert "auto_trash" in thresholds
        assert "flag_for_review" in thresholds
        assert thresholds["auto_trash"] >= thresholds["flag_for_review"]

    def test_patterns_have_never_trash(self):
        patterns = load_patterns()
        never_trash = patterns.get("never_trash", {})
        assert "categories" in never_trash
        assert len(never_trash["categories"]) > 0
