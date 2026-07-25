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

LLM escalation tier (optional; USE_LLM=true to enable):
  Default model is HY3 (tencent/hy3 via OpenRouter). Falls back to a free
  model automatically if HY3 is unavailable.
  USE_LLM               -> true/false (default false) — use LLM for borderline emails
  LLM_API_KEY           -> OpenRouter API key. If unset, we look for
                           OPENROUTER_API_KEY in the Hermes gateway .env
                           (%LOCALAPPDATA%/hermes/.env).
  LLM_MODEL             -> primary model (default tencent/hy3 = HY3)
  LLM_FALLBACK_MODEL    -> fallback model (default google/gemma-4-26b-a4b-it:free)
  LLM_BASE_URL          -> OpenAI-compatible endpoint (default OpenRouter)
  LLM_TIMEOUT           -> per-request seconds (default 25)

Other optional:
  LOG_DIR               -> where run logs go (default: ./logs)
  DB_PATH               -> SQLite file (default: ./state/gmail_agent.db)
  DRY_RUN               -> true/false (default false) — analyze but don't trash
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
DEFAULT_DB = ROOT / "state" / "gmail_agent.db"
DEFAULT_LOGDIR = ROOT / "logs"

# Hermes gateway .env holds OPENROUTER_API_KEY (shared so the Gmail agent
# reuses the same key that Hermes uses for HY3/other models).
_HERMES_ENV = Path(
    os.environ.get("LOCALAPPDATA", "%LOCALAPPDATA%")
) / "hermes" / ".env"

# HY3-first defaults for the LLM escalation tier.
DEFAULT_LLM_MODEL = "tencent/hy3"
DEFAULT_LLM_FALLBACK = "google/gemma-4-26b-a4b-it:free"
DEFAULT_LLM_BASE_URL = "https://openrouter.ai/api/v1"


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


def _read_key_from_file(path: str, key: str) -> str:
    """Best-effort read of a single KEY=VALUE line from a .env file."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == key:
                    return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def get_settings() -> dict:
    env = load_env()
    # Reuse the Hermes gateway key if the project .env doesn't set its own.
    llm_api_key = env.get("LLM_API_KEY", "")
    if not llm_api_key and _HERMES_ENV.exists():
        llm_api_key = _read_key_from_file(str(_HERMES_ENV), "OPENROUTER_API_KEY")
    return {
        "user_email": env.get("GMAIL_USER_EMAIL", ""),
        "mode": env.get("GMAIL_MODE", "oauth").lower(),
        "token_json": Path(env.get("GMAIL_TOKEN_JSON", str(ROOT / "token.json"))),
        "credentials_json": Path(env.get("GMAIL_CREDENTIALS_JSON", str(ROOT / "credentials.json"))),
        "app_password": env.get("GMAIL_APP_PASSWORD", ""),
        "imap_host": env.get("GMAIL_IMAP_HOST", "imap.gmail.com"),
        "openai_api_key": env.get("OPENAI_API_KEY", ""),
        "llm_api_key": llm_api_key,
        "llm_model": env.get("LLM_MODEL", DEFAULT_LLM_MODEL),
        "llm_fallback_model": env.get("LLM_FALLBACK_MODEL", DEFAULT_LLM_FALLBACK),
        "llm_base_url": env.get("LLM_BASE_URL", DEFAULT_LLM_BASE_URL),
        "llm_timeout": int(env.get("LLM_TIMEOUT", "25") or 25),
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
