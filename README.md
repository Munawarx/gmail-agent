# Intelligent Gmail Management Agent

An autonomous agent that scans your inbox **from a saved checkpoint**, classifies
each email (OTP / social / promotional / keep), and moves trashable mail to Trash
— resuming exactly where it left off on the next scheduled run. No re-scanning the
whole inbox; no duplicate processing.

## Quick start (two auth modes)

### Mode A — IMAP + App Password (NO Google Cloud project, recommended)
1. Enable **2-Step Verification** on your Google account.
2. Create an **App Password**: https://myaccount.google.com/apppasswords
   (a 16-character code, e.g. `abcd efgh ijkl mnop` — enter without spaces).
3. Copy `.env.example` -> `.env` and set:
     GMAIL_MODE=imap
     GMAIL_USER_EMAIL=you@gmail.com
     GMAIL_APP_PASSWORD=xxxxxxxxxxxxcccc
4. `uv run python -m gmail_agent.cli run --dry-run`  (preview, no trashing)
5. `uv run python -m gmail_agent.cli run`  (live — moves trash)

### Mode B — OAuth (original; needs a Google Cloud project)
1. Create a Google Cloud project, enable **Gmail API**, create **OAuth Desktop
   credentials**, download as `credentials.json` into this folder.
2. Copy `.env.example` -> `.env`, set `GMAIL_USER_EMAIL`, `GMAIL_MODE=oauth`.
3. `uv run python -m gmail_agent.cli auth`  (opens browser, saves token.json)
4. `uv run python -m gmail_agent.cli run --dry-run`  (preview, no trashing)
5. `uv run python -m gmail_agent.cli run`  (live — moves trash)

6. Schedule it (cron / Task Scheduler) to run daily.

## Commands
- `run`            one pass, resumes from checkpoint
- `run --dry-run` analyze only
- `run --max N`    cap emails this pass
- `status`         show checkpoint + errored ids
- `auth`           perform OAuth (no-op in IMAP mode; prints a hint)

## Classification rules (see docs/ARCHITECTURE.md)
- OTP expired -> Trash
- Social notifications (LinkedIn/FB/IG/X/Threads/...) -> Trash
- Promotional (sales/coupons/newsletters/...) -> Trash
- Everything else -> Keep

## Safety
- `DRY_RUN=true` analyzes but never trashes.
- Checkpoint saved after every email -> crash-safe, resumable.
- Failed emails logged + retried next run; never blocks the queue.

## Tests
`uv run pytest -q`  (no Gmail needed — client is mocked)
