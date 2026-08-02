"""Tests for the Gmail agent. No network/Gmail needed — GmailClient is mocked.

Run: cd gmail_agent && uv run pytest -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_agent import agent, classifier
from gmail_agent.schemas import Category, Email
from gmail_agent.store import Store


def _email(eid, subject, sender, snippet="", body="", received="1700000000000"):
    return Email(id=eid, thread_id=eid, subject=subject, sender=sender,
                 received_at=received, snippet=snippet, body_text=body)


# ---------- classifier ----------
def test_otp_expired():
    e = _email("1", "Your OTP is 123456", "security@bank.com",
               body="Your code is 123456. This code has expired.")
    c = classifier.classify(e)
    assert c.category == Category.OTP_EXPIRED
    assert c.otp_expired is True


def test_otp_valid():
    e = _email("2", "Your verification code is 998877", "no-reply@google.com",
               body="Your code is 998877. Valid for 10 minutes.")
    c = classifier.classify(e)
    assert c.category == Category.OTP_VALID


def test_social_linkedin_kept_for_user():
    # P26 KEEP list protects Work/Interviews; LinkedIn is professional/career
    # mail and must NOT be trashed for the account holder. With the account
    # email injected, linkedin.com is treated as professional -> KEEP.
    e = _email("3", "You have a new connection request", "notifications@linkedin.com",
               body="John sent you a connection request")
    e._account_email = "munawar304@gmail.com"
    assert classifier.classify(e).category == Category.KEEP


def test_self_sent_hvac_kept():
    # User's own HVAC-tagged mail (e.g. a CV) must be kept despite HVAC rule.
    e = _email("99", "MD Munawar Ali (CV)", "munawar304@gmail.com", body="my hvac resume")
    e._account_email = "munawar304@gmail.com"
    assert classifier.classify(e).category == Category.KEEP


def test_thirdparty_hvac_trash():
    # Third-party HVAC marketing (non-professional sender) is trashed.
    e = _email("98", "HVAC technician needed", "spam@hvacdeals.net", body="hvac job")
    e._account_email = "munawar304@gmail.com"
    assert classifier.classify(e).category == Category.PROMO


def test_promo():
    e = _email("4", "50% OFF flash sale — shop now!", "deals@store.com",
               body="Limited time offer, free shipping, big savings")
    assert classifier.classify(e).category == Category.PROMO


def test_promo_named_sender_kept_before_fix():
    # Regression: promo mail from a "Name <mail@e.adobe.com>" style sender
    # must be caught, not silently kept.
    e = _email("6", "Save 50% on your first year of a Firefly Pro Plus plan",
               '"Adobe Firefly" <mail@e.adobe.com>',
               body="Try Firefly Pro Plus — 50% off your first year")
    assert classifier.classify(e).category == Category.PROMO


def test_keep_personal():
    e = _email("5", "Dinner plans Friday?", "friend@gmail.com",
               body="Hey want to grab dinner?")
    assert classifier.classify(e).category == Category.KEEP


# ---------- store ----------
def test_checkpoint_roundtrip(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    assert s.get_checkpoint() is None
    s.save_checkpoint("msg9", "2026-07-17T00:00:00+00:00")
    assert s.get_checkpoint() == ("msg9", "2026-07-17T00:00:00+00:00")
    s.save_checkpoint("msg10", "2026-07-18T00:00:00+00:00")
    assert s.get_checkpoint()[0] == "msg10"  # updated, not duplicated
    s.close()


def test_already_processed(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    from gmail_agent.schemas import LogEntry
    s.log(LogEntry("x", "s", "f", "t", "promo", "trash", "processed"))
    assert s.already_processed("x") is True
    assert s.already_processed("y") is False
    s.close()


# ---------- agent loop (mocked gmail) ----------
class FakeGmail:
    def __init__(self, messages):
        # messages: list of dicts in chronological (oldest-first) order
        self._all = messages
        self.trashed = []

    def list_ids_chronological(self, page_token=None, max_results=100):
        ids = [m["id"] for m in self._all]
        return ids, None

    def get_message(self, mid):
        return next(m for m in self._all if m["id"] == mid)

    def trash(self, mid):
        self.trashed.append(mid)


def _raw(eid, subject, sender, body=""):
    return {"id": eid, "threadId": eid, "internalDate": "1700000000000",
            "snippet": "", "labelIds": [],
            "payload": {"headers": [{"name": "Subject", "value": subject},
                                    {"name": "From", "value": sender}],
                        "parts": [{"mimeType": "text/plain",
                                   "body": {"data": __import__("base64").urlsafe_b64encode(body.encode()).decode()}}]}}


def test_agent_resumes_from_checkpoint(tmp_path):
    msgs = [_raw("m1", "Your OTP is 1111", "security@bank.com", "code 1111 expired"),
            _raw("m2", "50% off sale", "deals@x.com", "shop now big savings"),
            _raw("m3", "hi", "friend@gmail.com", "dinner?")]
    fake = FakeGmail(msgs)
    store = Store(tmp_path / "db.sqlite")

    # first run: should process m1, m2 (trash), m3 (keep). Checkpoint = m3.
    agent.run(dry_run=False, gmail_client=fake, store=store)
    assert store.get_checkpoint()[0] == "m3"
    assert set(fake.trashed) == {"m1", "m2"}
    store.close()


def test_agent_no_rescan_after_checkpoint(tmp_path):
    msgs = [_raw("m1", "Your OTP is 2222", "security@bank.com", "expired code"),
            _raw("m2", "promo", "deals@x.com", "sale save")]
    fake = FakeGmail(msgs)
    store = Store(tmp_path / "db.sqlite")
    agent.run(dry_run=False, gmail_client=fake, store=store)  # processes both
    # second run: checkpoint at m2 -> nothing new to process
    fake2 = FakeGmail(msgs)
    summary = agent.run(dry_run=False, gmail_client=fake2, store=store)
    assert summary["processed"] == 0
    assert fake2.trashed == []
    store.close()


def test_agent_dry_run_does_not_trash(tmp_path):
    msgs = [_raw("m1", "Your OTP is 3333", "security@bank.com", "expired")]
    fake = FakeGmail(msgs)
    store = Store(tmp_path / "db.sqlite")
    summary = agent.run(dry_run=True, gmail_client=fake, store=store)
    assert summary["dry_run"] is True
    assert fake.trashed == []  # never called
    assert store.get_checkpoint()[0] == "m1"  # checkpoint still advances
    store.close()


def test_agent_error_continues(tmp_path):
    msgs = [_raw("m1", "Your OTP is 4444", "security@bank.com", "expired"),
            _raw("m2", "promo", "deals@x.com", "sale")]
    fake = FakeGmail(msgs)
    def boom(mid):
        raise RuntimeError("simulated API failure")
    fake.get_message = boom
    store = Store(tmp_path / "db.sqlite")
    summary = agent.run(dry_run=False, gmail_client=fake, store=store)
    assert summary["errors"] == 2
    assert summary["processed"] == 0  # both failed, checkpoint did not advance past them
    store.close()
