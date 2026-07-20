"""Data schemas for the Gmail agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Category(str, Enum):
    KEEP = "keep"                      # nothing to do
    OTP_EXPIRED = "otp_expired"        # OTP present and expired -> trash
    OTP_VALID = "otp_valid"            # OTP present, still valid -> keep
    SOCIAL = "social"                  # social notification -> trash
    PROMO = "promo"                    # promotional -> trash
    IMPORTANT = "important"            # keep (not trashable)


@dataclass
class Email:
    id: str
    thread_id: str
    subject: str
    sender: str
    received_at: str                   # RFC3339 / internalDate iso
    snippet: str = ""
    body_text: str = ""
    labels: list[str] = field(default_factory=list)

    def short(self) -> str:
        return f"<{self.id}> from={self.sender!r} subj={self.subject!r}"


@dataclass
class Classification:
    category: Category
    reason: str
    confidence: float = 1.0
    otp_code: Optional[str] = None
    otp_expired: Optional[bool] = None


@dataclass
class LogEntry:
    email_id: str
    subject: str
    sender: str
    received_at: str
    category: str
    action: str
    status: str                        # "processed" | "skipped" | "error" | "retry"
    error: str = ""
