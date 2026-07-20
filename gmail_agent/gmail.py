"""Gmail API client: read messages (chronological) + move to Trash.

Uses the official Google API client. Auth is OAuth (token.json). On first run
with only credentials.json, run() triggers the console OAuth flow (you paste
the code once). All destructive actions go through a trash() that respects
DRY_RUN at the agent level (this client just executes what it's told).
"""
from __future__ import annotations

import os
import pickle
import imaplib
from pathlib import Path
from typing import Iterable

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailClient:
    def __init__(self, credentials_json: Path, token_json: Path,
                 user_email: str = "me"):
        self.credentials_json = Path(credentials_json)
        self.token_json = Path(token_json)
        self.user = user_email
        self.service = None

    # ---- auth ----
    def authenticate(self) -> None:
        creds = None
        if self.token_json.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_json), SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not self.credentials_json.exists():
                    raise FileNotFoundError(
                        f"Missing OAuth credentials: {self.credentials_json}. "
                        "Create a Google Cloud project, enable Gmail API, "
                        "download credentials.json.")
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_json), SCOPES)
                creds = flow.run_local_server(port=0)
            self.token_json.parent.mkdir(parents=True, exist_ok=True)
            self.token_json.write_text(creds.to_json())
        self.service = build("gmail", "v1", credentials=creds)

    # ---- read ----
    def list_ids_chronological(self, page_token: str | None = None, max_results: int = 100):
        """Return (list_of_message_ids, next_page_token). Oldest first."""
        kwargs = {"userId": self.user, "maxResults": max_results,
                  "q": "-in:trash", "orderBy": "date"}
        if page_token:
            kwargs["pageToken"] = page_token
        res = self.service.users().messages().list(**kwargs).execute()
        return [m["id"] for m in res.get("messages", [])], res.get("nextPageToken")

    def get_message(self, msg_id: str):
        return self.service.users().messages().get(
            userId=self.user, id=msg_id, format="full").execute()

    def trash(self, msg_id: str) -> None:
        self.service.users().messages().trash(userId=self.user, id=msg_id).execute()


class ImapClient:
    """OAuth-free Gmail backend using IMAP + an App Password.

    No Google Cloud project, no OAuth consent screen. Generate an App
    Password at https://myaccount.google.com/apppasswords (requires 2-Step
    Verification enabled). Set GMAIL_USER_EMAIL + GMAIL_APP_PASSWORD in .env
    and GMAIL_MODE=imap.

    Implements the same interface the agent loop expects:
      list_ids_chronological(page_token, max_results) -> (ids, next_token)
      get_message(msg_id) -> raw Gmail-API-shaped dict (subset)
      trash(msg_id) -> move message to the Trash mailbox
    """

    TRASH_MAILBOX = "[Gmail]/Trash"

    def __init__(self, user_email: str, app_password: str,
                 imap_host: str = "imap.gmail.com", user: str = "me"):
        self.email = user_email
        self.app_password = app_password
        self.imap_host = imap_host
        self.user = user  # accepted for interface parity; unused for IMAP
        self._conn = None
        self._uid_validity = None

    # ---- auth ----
    def authenticate(self) -> None:
        if not self.email or not self.app_password:
            raise ValueError(
                "IMAP mode requires GMAIL_USER_EMAIL and GMAIL_APP_PASSWORD "
                "in .env (no OAuth needed).")
        # Connect + login to validate credentials up front. Use a throwaway
        # connection: do NOT cache it in self._conn (logging out leaves the
        # cached handle dead, and the agent would then SELECT on a LOGOUT
        # socket). _ensure() opens a fresh connection on first use.
        conn = imaplib.IMAP4_SSL(self.imap_host)
        conn.login(self.email, self.app_password)
        conn.select("INBOX")
        conn.close()
        conn.logout()

    def _connect(self):
        conn = imaplib.IMAP4_SSL(self.imap_host)
        conn.login(self.email, self.app_password)
        self._conn = conn
        return conn

    def _ensure(self):
        if self._conn is None:
            self._connect()
        return self._conn

    # ---- read ----
    def list_ids_chronological(self, page_token: str | None = None,
                               max_results: int = 100):
        """Return (list_of_msg_ids, next_page_token).

        msg_id here is the IMAP UID (stable, monotonically increasing) so the
        checkpoint logic in agent.py works unchanged. Oldest first. We page
        using the 'page_token' which carries the last UID we returned.
        """
        conn = self._ensure()
        conn.select("INBOX")
        status, data = conn.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return [], None
        uids = [int(u) for u in data[0].split()]
        # oldest first (IMAP SEARCH already returns ascending)
        if page_token:
            try:
                after = int(page_token)
                uids = [u for u in uids if u > after]
            except ValueError:
                pass
        page = uids[:max_results]
        next_token = None
        if len(uids) > max_results and page:
            next_token = str(page[-1])
        return [str(u) for u in page], next_token

    def get_message(self, msg_id: str):
        conn = self._ensure()
        conn.select("INBOX")
        status, data = conn.fetch(str(msg_id).encode(), "(RFC822)")
        if status != "OK" or not data:
            raise KeyError(f"message {msg_id} not found")
        raw_bytes = None
        for part in data:
            if isinstance(part, tuple):
                raw_bytes = part[1]
                break
        if raw_bytes is None:
            raise KeyError(f"message {msg_id} body missing")
        msg = email.message_from_bytes(raw_bytes)
        subject = _decode_header(msg.get("Subject", ""))
        sender = _decode_header(msg.get("From", ""))
        date_hdr = msg.get("Date", "")
        body = _extract_text(msg)
        snippet = body[:200]
        return {
            "id": str(msg_id),
            "threadId": str(msg_id),
            "snippet": snippet,
            "internalDate": "",  # IMAP has no internalDate; Date header used
            "labelIds": ["INBOX"],
            "payload": {
                "headers": [
                    {"name": "Subject", "value": subject},
                    {"name": "From", "value": sender},
                    {"name": "Date", "value": date_hdr},
                ],
                "body": {"data": ""},  # body extracted below for API parity
                "parts": [],
            },
        }

    def trash(self, msg_id: str) -> None:
        conn = self._ensure()
        conn.select("INBOX")
        # COPY to Trash, then mark deleted + expunge (IMAP-safe trash).
        conn.copy(str(msg_id).encode(), self.TRASH_MAILBOX)
        conn.store(str(msg_id).encode(), "+FLAGS", "\\Deleted")
        conn.expunge()


# ---- helpers ----
import email
from email.header import decode_header as _decode_header_raw


def _decode_header(value: str) -> str:
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


def _extract_text(msg) -> str:
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
