"""Scan Gmail [Gmail]/All Mail and clean promo/social/expired-OTP mail.

- Opens All Mail (every message except Trash/Spam/Drafts). 91 in this account.
- Reuses the SAME proven classifier + guardrail as the agent:
    trash  -> PROMO / SOCIAL / OTP_EXPIRED
    keep   -> IMPORTANT (security/account/banking/financial/legal) + everything else
- Deterministic rules only (no per-email LLM call) -> fast, cheap, offline-safe.
- RESILIENT: reconnects if Gmail drops the IMAP socket; resumes from the last
  processed UID (state saved to .scan_allmail_state.json) so a dropped run can
  simply be re-run to continue.
- Trashing happens in All Mail context (UIDs valid there). Recoverable 30 days.

Default: DRY-RUN (no mail moved). Pass --live to move matches to Trash.
"""
from __future__ import annotations
import argparse
import email
import imaplib
import json
import os
import re
import socket
import time
from email.header import decode_header as _dh
from pathlib import Path

from gmail_agent import config, classifier
from gmail_agent.schemas import Category

ALLMAIL = '"[Gmail]/All Mail"'
TRASH = '"[Gmail]/Trash"'
STATE_FILE = Path(__file__).resolve().parent / ".scan_allmail_state.json"


def _dec(v: str) -> str:
    if not v:
        return ""
    out = []
    for blob, enc in _dh(v):
        if isinstance(blob, bytes):
            out.append(blob.decode(enc or "utf-8", "replace"))
        else:
            out.append(blob)
    return " ".join(out)


def _text(msg) -> str:
    if msg.is_multipart():
        for p in msg.walk():
            if p.get_content_type() == "text/plain":
                try:
                    return p.get_payload(decode=True).decode("utf-8", "replace")
                except Exception:
                    return ""
    try:
        return msg.get_payload(decode=True).decode("utf-8", "replace")
    except Exception:
        return ""


def connect(s):
    conn = imaplib.IMAP4_SSL(s["imap_host"], 993)
    conn.login(s["user_email"], s["app_password"])
    return conn


def get_settings_for(env_path=None):
    """Load settings from a specific .env file (for multi-account runs)."""
    if env_path is None:
        return config.get_settings()
    # Build settings from the alternate env file directly.
    env = config.load_env(Path(env_path))
    llm_api_key = env.get("LLM_API_KEY", "")
    hermes_env = Path(os.environ.get("LOCALAPPDATA", "%LOCALAPPDATA%")) / "hermes" / ".env"
    if not llm_api_key and hermes_env.exists():
        llm_api_key = config._read_key_from_file(str(hermes_env), "OPENROUTER_API_KEY")
    return {
        "user_email": env.get("GMAIL_USER_EMAIL", ""),
        "mode": env.get("GMAIL_MODE", "imap").lower(),
        "app_password": env.get("GMAIL_APP_PASSWORD", ""),
        "imap_host": env.get("GMAIL_IMAP_HOST", "imap.gmail.com"),
        "use_llm": str(env.get("USE_LLM", "false")).lower() == "true",
        "llm_model": env.get("LLM_MODEL", config.DEFAULT_LLM_MODEL),
        "llm_fallback_model": env.get("LLM_FALLBACK_MODEL", config.DEFAULT_LLM_FALLBACK),
        "llm_base_url": env.get("LLM_BASE_URL", config.DEFAULT_LLM_BASE_URL),
        "llm_timeout": int(env.get("LLM_TIMEOUT", "25") or 25),
        "db_path": Path(env.get("DB_PATH", str(config.DEFAULT_DB))),
        "log_dir": Path(env.get("LOG_DIR", str(config.DEFAULT_LOGDIR))),
        "dry_run": str(env.get("DRY_RUN", "false")).lower() == "true",
        "max_emails": int(env.get("MAX_EMAILS", "0") or 0),
    }


def load_state(state_file) -> dict:
    if Path(state_file).exists():
        try:
            return json.loads(Path(state_file).read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": [], "trashed": []}


def save_state(st, state_file):
    Path(state_file).write_text(json.dumps(st, indent=2), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="Actually move matches to Trash")
    ap.add_argument("--reset", action="store_true", help="Clear resume state and start over")
    ap.add_argument("--env", default=None, help="Path to an alternate .env file (for a 2nd account)")
    ap.add_argument("--query", default=None, help="Gmail X-GM-RAW query, or comma-separated queries e.g. 'label:promotions' or 'from:instagram.com,from:tiktok.com'")
    args = ap.parse_args()
    dry = not args.live

    # Per-account state file so account #1 and #2 don't collide.
    if args.env:
        state_file = Path(args.env).resolve().parent / (".scan_allmail_state_" + Path(args.env).stem + ".json")
    else:
        state_file = STATE_FILE

    if args.reset and Path(state_file).exists():
        Path(state_file).unlink()

    s = get_settings_for(args.env)
    st = load_state(state_file)
    done = set(st.get("done", []))
    trashed = set(st.get("trashed", []))

    # One shared IMAP connection, reconnected on failure.
    conn = connect(s)
    conn.select(ALLMAIL, readonly=True)
    # Use X-GM-RAW query(s) if provided (fast, server-side filtering).
    # Support comma-separated queries -> union of matched UIDs.
    if args.query:
        queries = [q.strip() for q in args.query.split(",") if q.strip()]
        uids = []
        seen = set()
        for q in queries:
            _, data = conn.search(None, "X-GM-RAW", q)
            for u in data[0].split() if data and data[0] else []:
                if u.decode() not in seen:
                    seen.add(u.decode())
                    uids.append(u.decode())
    else:
        _, data = conn.search(None, "ALL")
        uids = [u.decode() for u in data[0].split()] if data and data[0] else []

    # Build list of remaining UIDs (skip already-done; preserve order)
    remaining = [u for u in uids if u not in done]
    print(f"[account: {s['user_email']}]  query={args.query or 'ALL'}  matched: {len(uids)} | already processed: {len(done)} | remaining: {len(remaining)}")
    if not remaining:
        print("Nothing left to process. Use --reset to run again from scratch.")
        conn.logout()
        return

    no_llm = dict(s)
    no_llm["use_llm"] = False
    rows = []
    to_trash = []

    for uid in remaining:
        # Fetch with resilient reconnect.
        raw = None
        msgid = None
        for attempt in range(6):
            try:
                _, data = conn.fetch(uid, "(BODY.PEEK[HEADER] X-GM-MSGID)")
                if data:
                    for part in data:
                        if isinstance(part, tuple):
                            hdr = part[0] if isinstance(part[0], bytes) else b""
                            payload = part[1] if len(part) > 1 else b""
                            m = re.search(rb"X-GM-MSGID\s+(\d+)", hdr)
                            if m:
                                try:
                                    msgid = int(m.group(1))
                                except Exception:
                                    msgid = None
                            if isinstance(payload, bytes) and b"\n" in payload and raw is None:
                                raw = payload
                if raw is not None and msgid is not None:
                    break
                # fallthrough: reconnect and retry
                raise imaplib.IMAP4.abort("empty")
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, socket.error, OSError) as e:
                print(f"  [reconnect] uid={uid} attempt {attempt+1}: {type(e).__name__}", flush=True)
                time.sleep(2)
                try:
                    conn.logout()
                except Exception:
                    pass
                try:
                    conn = connect(s)
                    conn.select(ALLMAIL, readonly=True)
                except Exception:
                    pass
        if raw is None or msgid is None:
            print(f"  [skip] uid={uid} could not be fetched after retries")
            continue

        msg = email.message_from_bytes(raw)
        subject = _dec(msg.get("Subject", ""))
        sender = _dec(msg.get("From", ""))
        body = _text(msg)
        em = type("E", (), {})()
        em.id = uid
        em.thread_id = uid
        em.subject = subject
        em.sender = sender
        em.received_at = msg.get("Date", "")
        em.snippet = body[:200]
        em.body_text = body
        em.labels = ["AllMail"]
        cls, action = classifier.classify_with_learning(em, no_llm)
        rows.append((uid, cls.category.value, action, cls.confidence, sender[:44], subject[:36]))
        done.add(uid)
        if action == "trash":
            to_trash.append(uid)
        # Persist progress frequently so a drop is cheap to resume.
        if len(done) % 10 == 0:
            save_state({"done": sorted(done, key=int), "trashed": sorted(trashed, key=int)}, state_file)
            print(f"  ...scanned {len(done)}/{len(uids)}", flush=True)

    save_state({"done": sorted(done, key=int), "trashed": sorted(trashed, key=int)}, state_file)

    # Report what was classified this run (+ cumulative not needed; show this run)
    print(f"{'UID':>5}  {'CATEGORY':<12}{'ACTION':<6}{'CONF':<5}  SENDER / SUBJECT")
    print("-" * 96)
    for uid, cat, act, conf, snd, subj in rows:
        print(f"{uid:>5}  {cat:<12}{act:<6}{conf:<5.2f}  {snd:<44} {subj}")
    would = sum(1 for r in rows if r[2] == "trash")
    keeps = sum(1 for r in rows if r[2] == "keep")
    flags = sum(1 for r in rows if r[2] == "flag")
    print(f"\n=== PREVIEW (dry={dry}) ===")
    print(f"  Emails evaluated this run: {len(rows)}")
    print(f"  Would TRASH : {would}")
    print(f"  Would KEEP  : {keeps}")
    print(f"  Would FLAG  : {flags}")
    print("  Security/banking/financial/account mail is NEVER trashed by the guardrail.")

    if dry:
        print("\nDRY-RUN: no mail moved. Re-run with --live to actually move matches to Trash.")
        conn.logout()
        return

    # LIVE: copy to Trash + mark deleted, in All Mail context. Resilient.
    conn.select(ALLMAIL)
    moved = 0
    live_trash = [u for u in to_trash if u not in trashed]
    print(f"\n[LIVE] moving {len(live_trash)} matched emails to Trash...")
    for uid in live_trash:
        ok = False
        for attempt in range(6):
            try:
                conn.copy(uid, TRASH)
                conn.store(uid, "+FLAGS", "\\Deleted")
                ok = True
                break
            except (imaplib.IMAP4.abort, imaplib.IMAP4.error, socket.error, OSError):
                time.sleep(2)
                try:
                    conn.logout()
                except Exception:
                    pass
                try:
                    conn = connect(s)
                    conn.select(ALLMAIL)
                except Exception:
                    pass
        if ok:
            moved += 1
            trashed.add(uid)
            save_state({"done": sorted(done, key=int), "trashed": sorted(trashed, key=int)}, state_file)
    # NOTE: intentionally NO conn.expunge() here. In Gmail IMAP, expunging
    # [Gmail]/All Mail permanently deletes the message everywhere — including
    # Trash — which would violate the "never permanently delete / keep
    # recoverable 30 days" rule. Copying to Trash is enough; the original also
    # remains in All Mail (zero data loss). Never expunge on All Mail.
    conn.logout()
    print(f"\nLIVE DONE: {moved} emails copied to Trash (recoverable for 30 days; originals retained in All Mail).")


if __name__ == "__main__":
    main()
