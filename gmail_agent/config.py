"""Config + safe .env loader. Credentials are never printed.

Auth modes:
  GMAIL_MODE=oauth (default) -> uses Gmail API + OAuth (credentials.json/token.json)
  GMAIL_MODE=imap          -> uses IMAP + an App Password (no Google Cloud project)

Required .env keys (oauth mode):
  GMAIL_USER_EMAIL      -> the mailbox being managed (for logging only)
  GMAIL_TOKEN_JSON      -> path to OAuth token.json (or credentials.json)
  GMAIL_CREDENTIALS_JSON-> path to OAuth credentials.json (first-run)

Required .env keys (imap mode):
  GMAIL_USER_EMAIL      -> your full Gmail address
  GMAIL_APP_PASSWORD    -> 16-char App Password (generate after enabling 2-Step Verification)
  GMAIL_IMAP_HOST       -> imap.gmail.com (default if unset)

Optional:
  OPENAI_API_KEY        -> optional; enables LLM classification tier
  LOG_DIR               -> where run logs go (default: ./logs)
  DB_PATH               -> SQLite file (default: ./state/gmail_agent.db)
  DRY_RUN               -> true/false (default false) — analyze but don't trash
  USE_LLM               -> true/false (default false) — use LLM for hard cases
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
DEFAULT_DB = ROOT / "state" / "gmail_agent.db"
DEFAULT_LOGDIR = ROOT / "logs"


def load_env(path: Path = ENV_PATH) -> dict:
    data: dict[str, str] = {}
    p = Path(path)
    if p.exists():
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip().strip('"').strip("'")
    return data


def get_settings() -> dict:
    env = load_env()
    return {
        "user_email": env.get("GMAIL_USER_EMAIL", ""),
        "mode": env.get("GMAIL_MODE", "oauth").lower(),
        "token_json": Path(env.get("GMAIL_TOKEN_JSON", str(ROOT / "token.json"))),
        "credentials_json": Path(env.get("GMAIL_CREDENTIALS_JSON", str(ROOT / "credentials.json"))),
        "app_password": env.get("GMAIL_APP_PASSWORD", ""),
        "imap_host": env.get("GMAIL_IMAP_HOST", "imap.gmail.com"),
        "openai_api_key": env.get("OPENAI_API_KEY", ""),
        "db_path": Path(env.get("DB_PATH", str(DEFAULT_DB))),
        "log_dir": Path(env.get("LOG_DIR", str(DEFAULT_LOGDIR))),
        "dry_run": str(env.get("DRY_RUN", "false")).lower() == "true",
        "use_llm": str(env.get("USE_LLM", "false")).lower() == "true",
        "max_emails": int(env.get("MAX_EMAILS", "0") or 0),  # 0 = unlimited
    }


def has_credentials() -> bool:
    env = load_env()
    mode = env.get("GMAIL_MODE", "oauth").lower()
    if mode == "imap":
        return bool(env.get("GMAIL_APP_PASSWORD"))
    return bool(env.get("GMAIL_TOKEN_JSON") or (ROOT / "token.json").exists())
