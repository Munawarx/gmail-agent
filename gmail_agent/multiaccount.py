"""Multi-account config for Gmail Agent (P5 spec).

Supports multiple Gmail accounts, each with its own IMAP credentials.
Reads from .env file with account-prefixed keys.

Accounts:
  ACCOUNT_1_EMAIL, ACCOUNT_1_APP_PASSWORD
  ACCOUNT_2_EMAIL, ACCOUNT_2_APP_PASSWORD
  ...

Or the legacy single-account keys (GMAIL_USER_EMAIL, GMAIL_APP_PASSWORD)
are auto-promoted to ACCOUNT_1.

Also sets the P5 safety parameters:
  DELETE_CAP       -> max trashed emails per run (default 50)
  CONFIDENCE_TRASH -> auto-trash threshold (default 0.80)
  CONFIDENCE_FLAG  -> flag-for-review threshold (default 0.55)
  BATCH_SIZE       -> emails per checkpoint write (default 25)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_env, ENV_PATH


# ═══ P5 Safety Parameters ═══
DEFAULT_DELETE_CAP = 50
DEFAULT_CONFIDENCE_TRASH = 0.80
DEFAULT_CONFIDENCE_FLAG = 0.55
DEFAULT_BATCH_SIZE = 25

# Scan order per P5 spec (IMAP-correct labels)
SCAN_LABELS = [
    "INBOX",
    "[Gmail]/Important",
    "[Gmail]/Spam",
    "[Gmail]/All Mail",
]

# Mailboxes to exclude from All Mail catch-all (already covered above)
DEDUP_SKIP_IN_ALLMAIL = {"[Gmail]/Sent Mail", "[Gmail]/Drafts", "[Gmail]/Trash"}


@dataclass
class Account:
    """A single Gmail account."""
    email: str
    app_password: str
    imap_host: str = "imap.gmail.com"
    is_primary: bool = True

    def __post_init__(self):
        if not self.email or not self.app_password:
            raise ValueError(f"Account missing email or password: {self.email}")

    @property
    def safe_name(self) -> str:
        """Filename-safe version of email for per-account state files."""
        return self.email.replace("@", "_at_").replace(".", "_")


@dataclass
class P5Config:
    """Configuration for the P5 multi-account triage agent."""
    accounts: list[Account] = field(default_factory=list)
    delete_cap: int = DEFAULT_DELETE_CAP
    confidence_trash: float = DEFAULT_CONFIDENCE_TRASH
    confidence_flag: float = DEFAULT_CONFIDENCE_FLAG
    batch_size: int = DEFAULT_BATCH_SIZE
    dry_run: bool = False
    use_llm: bool = False

    @property
    def primary(self) -> Account | None:
        return self.accounts[0] if self.accounts else None


def load_accounts() -> list[Account]:
    """Load all accounts from .env."""
    env = load_env(ENV_PATH)
    accounts: list[Account] = []

    # Check for multi-account format: ACCOUNT_1_EMAIL, ACCOUNT_2_EMAIL...
    i = 1
    while True:
        email_key = f"ACCOUNT_{i}_EMAIL"
        pass_key = f"ACCOUNT_{i}_APP_PASSWORD"
        email = env.get(email_key, "").strip()
        password = env.get(pass_key, "").strip()
        if not email and not password:
            break
        if email and password:
            accounts.append(Account(
                email=email,
                app_password=password,
                imap_host=env.get(f"ACCOUNT_{i}_IMAP_HOST", "imap.gmail.com"),
                is_primary=(i == 1),
            ))
        i += 1

    # Fallback: legacy single-account keys
    if not accounts:
        legacy_email = env.get("GMAIL_USER_EMAIL", "").strip()
        legacy_pass = env.get("GMAIL_APP_PASSWORD", "").strip()
        if legacy_email and legacy_pass:
            accounts.append(Account(
                email=legacy_email,
                app_password=legacy_pass,
                imap_host=env.get("GMAIL_IMAP_HOST", "imap.gmail.com"),
                is_primary=True,
            ))

    return accounts


def load_p5_config() -> P5Config:
    """Load full P5 config from .env."""
    env = load_env(ENV_PATH)
    accounts = load_accounts()

    return P5Config(
        accounts=accounts,
        delete_cap=int(env.get("DELETE_CAP", str(DEFAULT_DELETE_CAP))),
        confidence_trash=float(env.get("CONFIDENCE_TRASH", str(DEFAULT_CONFIDENCE_TRASH))),
        confidence_flag=float(env.get("CONFIDENCE_FLAG", str(DEFAULT_CONFIDENCE_FLAG))),
        batch_size=int(env.get("BATCH_SIZE", str(DEFAULT_BATCH_SIZE))),
        dry_run=str(env.get("DRY_RUN", "false")).lower() == "true",
        use_llm=str(env.get("USE_LLM", "false")).lower() == "true",
    )
