"""Tests for the IMAP backend (GmailAgent.ImapClient).

No real network/Gmail needed — imaplib.IMAP4_SSL is monkeypatched with an
in-memory fake that returns RFC822 message bytes. Covers list/get/trash
shaping the agent expects, plus the App-Password auth guard.
"""
from __future__ import annotations

import sys
import imaplib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gmail_agent import gmail
from gmail_agent.schemas import Email


# ---------- fake IMAP server ----------
class FakeIMAP:
    """Minimal stand-in for imaplib.IMAP4_SSL returning canned data."""

    def __init__(self, host="", *a, **k):
        self.host = host
        self.logged_in = False
        self._uids = [10, 11, 12]  # ascending = oldest-first
        self._msgs = {
            10: _rfc822("Old OTP 1111", "security@bank.com", "code 1111 expired",
                        "Tue, 01 Jan 2026 00:00:00 +0000"),
            11: _rfc822("50% off sale", "deals@x.com", "shop now big savings",
                        "Wed, 02 Jan 2026 00:00:00 +0000"),
            12: _rfc822("hi", "friend@gmail.com", "dinner?",
                        "Thu, 03 Jan 2026 00:00:00 +0000"),
        }
        self.deleted = []
        # patch the module so ImapClient._connect() builds us
        imaplib.IMAP4_SSL = lambda *a, **k: self

    # auth
    def login(self, user, pw):
        assert user and pw, "login requires creds"
        self.logged_in = True
        return ("OK", [b"ok"])

    def select(self, mailbox="INBOX"):
        self.mailbox = mailbox
        return ("OK", [b"1"])

    def search(self, *a):
        return ("OK", [b" ".join(str(u).encode() for u in self._uids)])

    def fetch(self, uid_spec, parts):
        uid = int(uid_spec.split()[0])
        return ("OK", [(None, self._msgs[uid])])

    def copy(self, uid_spec, mailbox):
        self.copied = (int(uid_spec.split()[0]), mailbox)
        return ("OK", [b""])

    def store(self, uid_spec, flags, value):
        self.deleted.append(int(uid_spec.split()[0]))
        return ("OK", [b""])

    def expunge(self):
        return ("OK", [b""])

    def close(self):
        return ("OK", [b""])

    def logout(self):
        return ("OK", [b""])


def _rfc822(subject, sender, body, date):
    raw = (
        f"From: {sender}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date}\r\n"
        f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}"
    ).encode("utf-8")
    return raw


def _make_client():
    c = gmail.ImapClient("me@gmail.com", "abcdabcdabcdabcd")
    # force a fresh fake connection per call
    FakeIMAP()  # registers itself as imaplib.IMAP4_SSL
    c._connect()
    return c


# ---------- tests ----------
def test_imap_auth_guard_missing_creds():
    c = gmail.ImapClient("", "")
    try:
        c.authenticate()
        assert False, "should have raised"
    except ValueError as e:
        assert "GMAIL_APP_PASSWORD" in str(e)


def test_imap_list_chronological_oldest_first():
    c = _make_client()
    ids, token = c.list_ids_chronological()
    assert ids == ["10", "11", "12"], ids
    assert token is None


def test_imap_list_pagination_token():
    c = _make_client()
    ids, token = c.list_ids_chronological(max_results=2)
    assert ids == ["10", "11"], ids
    assert token == "11"  # last returned uid
    # next page skips <= token
    ids2, _ = c.list_ids_chronological(page_token=token, max_results=2)
    assert ids2 == ["12"], ids2


def test_imap_get_message_shapes_email():
    c = _make_client()
    raw = c.get_message("11")
    # agent._email_from_gmail reads headers via payload.headers
    assert raw["id"] == "11"
    headers = {h["name"]: h["value"] for h in raw["payload"]["headers"]}
    assert headers["Subject"] == "50% off sale"
    assert headers["From"] == "deals@x.com"
    assert raw["labelIds"] == ["INBOX"]


def test_imap_trash_copies_and_deletes():
    c = _make_client()
    c.trash("12")
    assert c._conn.copied == (12, "[Gmail]/Trash")
    assert 12 in c._conn.deleted
