"""Tests for the P5 multi-account triage system.

Tests:
  1. Multi-account config loading
  2. Rules table CRUD + user-approved protection
  3. Checkpoints table save/resume
  4. Message-ID dedup
  5. Protected categories guardrail
  6. Confidence → action mapping
  7. End-to-end triage on mock data
"""
import pytest
import tempfile
import os
from pathlib import Path
from gmail_agent.p5_store import P5Store, Checkpoint, RunSummary
from gmail_agent.p5_guardrail import check_protection, classify_confidence
from gmail_agent.multiaccount import Account, P5Config, load_p5_config


@pytest.fixture
def store():
    """Fresh P5 store with temp database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    s = P5Store(db_path)
    yield s
    s.close()
    os.unlink(db_path)


class TestRulesTable:
    """Test the rules table — the core memory store."""

    def test_add_and_get_rule(self, store):
        store.upsert_rule("*", "spam@marketing.com", "sender", "trash", "user", 1.0)
        rule = store.get_rule("*", "spam@marketing.com")
        assert rule is not None
        assert rule.action == "trash"
        assert rule.approved_by == "user"

    def test_get_rule_account_specific_overrides_global(self, store):
        store.upsert_rule("*", "test@example.com", "sender", "trash", "learned", 0.9)
        store.upsert_rule("user@gmail.com", "test@example.com", "sender", "keep", "user", 1.0)
        rule = store.get_rule("user@gmail.com", "test@example.com")
        assert rule.action == "keep"

    def test_user_rule_never_overwritten(self, store):
        store.upsert_rule("*", "important@bank.com", "sender", "keep", "user", 1.0)
        # Attempt to overwrite with learned rule
        written = store.upsert_rule("*", "important@bank.com", "sender", "trash", "learned", 0.95)
        assert written is False
        rule = store.get_rule("*", "important@bank.com")
        assert rule.action == "keep"  # Still keep!

    def test_user_rule_can_be_overwritten_with_flag(self, store):
        store.upsert_rule("*", "test@example.com", "sender", "keep", "user", 1.0)
        written = store.upsert_rule("*", "test@example.com", "sender", "trash", "user", 1.0,
                                     allow_overwrite_user=True)
        assert written is True

    def test_delete_rule(self, store):
        store.upsert_rule("*", "spam@marketing.com", "sender", "trash", "learned", 0.9)
        assert store.delete_rule("*", "spam@marketing.com")
        assert store.get_rule("*", "spam@marketing.com") is None

    def test_domain_rule(self, store):
        store.upsert_rule("*", "marketing.com", "domain", "trash", "learned", 0.85)
        rule = store.get_rule("*", "marketing.com")
        assert rule is not None
        assert rule.pattern_type == "domain"


class TestCheckpoints:
    """Test checkpoint resume system."""

    def test_save_and_get_checkpoint(self, store):
        cp = Checkpoint(
            account="test@gmail.com", label="INBOX",
            last_message_id="abc123", last_uid="42",
            processed_count=10, trashed_count=5,
            stop_reason="batch_checkpoint",
        )
        store.save_checkpoint(cp)
        loaded = store.get_checkpoint("test@gmail.com", "INBOX")
        assert loaded is not None
        assert loaded.last_message_id == "abc123"
        assert loaded.processed_count == 10
        assert loaded.trashed_count == 5
        assert loaded.stop_reason == "batch_checkpoint"

    def test_get_unfinished_checkpoint(self, store):
        # Save completed checkpoint
        store.save_checkpoint(Checkpoint(
            account="a@gmail.com", label="INBOX",
            stop_reason="completed", processed_count=20,
        ))
        # Save unfinished checkpoint
        store.save_checkpoint(Checkpoint(
            account="a@gmail.com", label="[Gmail]/Important",
            last_message_id="xyz", stop_reason="cap_reached",
            processed_count=5,
        ))
        unfinished = store.get_unfinished_checkpoint("a@gmail.com")
        assert unfinished is not None
        assert unfinished.label == "[Gmail]/Important"

    def test_checkpoint_update_on_save(self, store):
        store.save_checkpoint(Checkpoint(
            account="a@gmail.com", label="INBOX",
            processed_count=5, stop_reason="batch_checkpoint",
        ))
        store.save_checkpoint(Checkpoint(
            account="a@gmail.com", label="INBOX",
            processed_count=10, stop_reason="completed",
        ))
        cp = store.get_checkpoint("a@gmail.com", "INBOX")
        assert cp.processed_count == 10
        assert cp.stop_reason == "completed"


class TestMessageIdDedup:
    """Test message-ID dedup — never process same email twice."""

    def test_mark_and_check_processed(self, store):
        assert not store.is_processed("a@gmail.com", "msg123")
        store.mark_processed("a@gmail.com", "msg123", "INBOX", "trash")
        assert store.is_processed("a@gmail.com", "msg123")

    def test_dedup_strips_angle_brackets(self, store):
        store.mark_processed("a@gmail.com", "<msg456>", "INBOX", "keep")
        assert store.is_processed("a@gmail.com", "<msg456>")
        assert store.is_processed("a@gmail.com", "msg456")

    def test_different_accounts_not_deduped(self, store):
        store.mark_processed("a@gmail.com", "msg789", "INBOX", "trash")
        assert not store.is_processed("b@gmail.com", "msg789")

    def test_empty_message_id_not_processed(self, store):
        assert not store.is_processed("a@gmail.com", "")
        store.mark_processed("a@gmail.com", "", "INBOX")
        assert not store.is_processed("a@gmail.com", "")


class TestProtectedCategories:
    """Test the P5 guardrail — never trash important categories."""

    def test_banking_email_protected(self):
        result = check_protection(
            sender="alerts@chase.com",
            subject="Your account statement",
            body="Account number ending in 1234",
        )
        assert result.is_protected
        assert "financial" in result.category

    def test_paypal_protected(self):
        result = check_protection(
            sender="service@paypal.com",
            subject="You've received a payment",
        )
        assert result.is_protected

    def test_security_alert_protected(self):
        result = check_protection(
            sender="noreply@accounts.google.com",
            subject="Security alert: New sign-in",
        )
        assert result.is_protected

    def test_password_reset_protected(self):
        result = check_protection(
            sender="noreply@example.com",
            subject="Password reset request",
        )
        assert result.is_protected

    def test_invoice_protected(self):
        result = check_protection(
            sender="billing@shop.com",
            subject="Invoice #12345",
        )
        assert result.is_protected

    def test_receipt_protected(self):
        result = check_protection(
            sender="orders@amazon.com",
            subject="Your receipt",
        )
        assert result.is_protected

    def test_travel_protected(self):
        result = check_protection(
            sender="noreply@united.com",
            subject="Flight confirmation",
        )
        assert result.is_protected

    def test_personal_correspondence_re_thread_protected(self):
        result = check_protection(
            sender="friend@example.com",
            subject="Re: Dinner plans",
        )
        assert result.is_protected

    def test_marketing_email_not_protected(self):
        result = check_protection(
            sender="newsletter@marketing.com",
            subject="Check out our sale!",
            body="Big discounts this week",
        )
        assert not result.is_protected

    def test_random_promo_not_protected(self):
        result = check_protection(
            sender="promo@somecompany.com",
            subject="You reached 1000 views!",
        )
        assert not result.is_protected


class TestConfidenceMapping:
    """Test confidence → action mapping with P5 thresholds."""

    def test_high_confidence_trash(self):
        assert classify_confidence(0.95, 0.80, 0.55) == "trash"

    def test_threshold_exact_trash(self):
        assert classify_confidence(0.80, 0.80, 0.55) == "trash"

    def test_medium_confidence_flag(self):
        assert classify_confidence(0.65, 0.80, 0.55) == "flag"

    def test_flag_threshold_exact(self):
        assert classify_confidence(0.55, 0.80, 0.55) == "flag"

    def test_low_confidence_keep(self):
        assert classify_confidence(0.30, 0.80, 0.55) == "keep"

    def test_custom_thresholds(self):
        assert classify_confidence(0.90, 0.95, 0.70) == "flag"


class TestMultiAccountConfig:
    """Test multi-account configuration."""

    def test_account_creation(self):
        a = Account(email="test@gmail.com", app_password="abcdefghijklmnop")
        assert a.email == "test@gmail.com"
        assert a.safe_name == "test_at_gmail_com"
        assert a.is_primary

    def test_account_validation(self):
        with pytest.raises(ValueError):
            Account(email="", app_password="")

    def test_p5config_defaults(self):
        config = P5Config()
        assert config.delete_cap == 50
        assert config.confidence_trash == 0.80
        assert config.confidence_flag == 0.55
        assert config.batch_size == 25


class TestRunSummaries:
    """Test run summary persistence."""

    def test_save_and_get_summary(self, store):
        s = RunSummary(
            run_id="test123",
            account="test@gmail.com",
            labels_scanned="INBOX,[Gmail]/All Mail",
            emails_analyzed=10,
            emails_trashed=5,
            emails_retained=3,
            emails_flagged=2,
            new_rules_learned=2,
            errors=0,
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:05:00Z",
        )
        store.save_summary(s)
        loaded = store.get_last_summary("test@gmail.com")
        assert loaded is not None
        assert loaded.run_id == "test123"
        assert loaded.emails_analyzed == 10
        assert loaded.emails_trashed == 5

    def test_latest_summary_retrieved(self, store):
        store.save_summary(RunSummary(
            run_id="old", account="a@gmail.com", started_at="2026-01-01",
            completed_at="2026-01-01T01:00:00",
        ))
        store.save_summary(RunSummary(
            run_id="new", account="a@gmail.com", started_at="2026-01-02",
            completed_at="2026-01-02T01:00:00",
        ))
        latest = store.get_last_summary("a@gmail.com")
        assert latest.run_id == "new"
