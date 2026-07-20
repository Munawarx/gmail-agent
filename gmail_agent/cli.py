"""CLI entry point for the Gmail agent.

Usage:
  uv run python -m gmail_agent.cli run            # one pass (resume from checkpoint)
  uv run python -m gmail_agent.cli run --dry-run  # analyze, don't trash
  uv run python -m gmail_agent.cli run --max 20   # cap emails this pass
  uv run python -m gmail_agent.cli status         # show checkpoint + counts
  uv run python -m gmail_agent.cli auth           # perform OAuth (writes token.json)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config, agent
from .store import Store


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="gmail_agent", description="Intelligent Gmail manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="process inbox from checkpoint")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--max", type=int, default=0)

    sub.add_parser("status", help="show checkpoint + log counts")
    sub.add_parser("auth", help="perform OAuth flow")

    args = p.parse_args(argv)

    if args.cmd == "auth":
        s = config.get_settings()
        if s.get("mode") == "imap":
            print("[auth] IMAP mode active — no OAuth needed. "
                  "Set GMAIL_USER_EMAIL + GMAIL_APP_PASSWORD in .env.")
            return 0
        c = agent.gmail.GmailClient(s["credentials_json"], s["token_json"], s["user_email"] or "me")
        c.authenticate()
        print("[auth] token saved to", s["token_json"])
        return 0

    if args.cmd == "status":
        s = config.get_settings()
        st = Store(s["db_path"])
        cp = st.get_checkpoint()
        print("checkpoint:", cp)
        print("errored (retry next run):", st.error_ids()[:20])
        st.close()
        return 0

    if args.cmd == "run":
        agent.run(dry_run=args.dry_run, max_emails=args.max)
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
