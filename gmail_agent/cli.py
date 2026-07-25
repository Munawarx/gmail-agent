"""CLI entry point for the Gmail agent.

Usage:
  uv run python -m gmail_agent.cli run            # one pass (resume from checkpoint)
  uv run python -m gmail_agent.cli run --dry-run  # analyze, don't trash
  uv run python -m gmail_agent.cli run --max 20   # cap emails this pass
  uv run python -m gmail_agent.cli status         # show checkpoint + counts
  uv run python -m gmail_agent.cli auth           # perform OAuth (writes token.json)
  uv run python -m gmail_agent.cli analyze-trash  # deep-analyze Trash folder
  uv run python -m gmail_agent.cli learn          # re-analyze trash + rebuild patterns
  uv run python -m gmail_agent.cli patterns       # show learned pattern summary
  uv run python -m gmail_agent.cli feedback <action> <email_id> <sender> <subject>  # record feedback
  uv run python -m gmail_agent.cli apply-feedback  # process feedback → update patterns
"""
from __future__ import annotations

import argparse
import json
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

    sub.add_parser("analyze-trash", help="deep-analyze emails in Gmail Trash")
    sub.add_parser("learn", help="re-analyze trash + rebuild learned patterns")
    sub.add_parser("patterns", help="show learned pattern knowledge base summary")

    fb = sub.add_parser("feedback", help="record a user action for learning")
    fb.add_argument("action", choices=["delete", "restore", "archive", "spam", "keep", "move"])
    fb.add_argument("email_id")
    fb.add_argument("sender")
    fb.add_argument("subject")
    fb.add_argument("--category", default="")
    fb.add_argument("--confidence", type=float, default=0.0)

    sub.add_parser("apply-feedback", help="process accumulated feedback → update patterns")

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

    if args.cmd == "analyze-trash":
        from .analyze_trash import analyze_trash
        results = analyze_trash()
        out_path = Path(__file__).resolve().parent.parent / "trash_analysis.json"
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n[analyze-trash] {len(results)} emails analyzed. Saved to {out_path}")
        return 0

    if args.cmd == "learn":
        print("[learn] Step 1: Analyzing Gmail Trash...")
        from .analyze_trash import analyze_trash
        results = analyze_trash()
        if not results:
            print("[learn] No emails in Trash — nothing to learn from.")
            return 0
        print(f"[learn] Analyzed {len(results)} trashed emails.")

        # Extract patterns from the analysis
        domains = {}
        senders = {}
        intents = {}
        for e in results:
            if "error" in e:
                continue
            dom = e.get("sender_domain", "")
            snd = e.get("sender_email", "")
            intent = e.get("intent", "")
            if dom:
                domains[dom] = domains.get(dom, 0) + 1
            if snd:
                senders[snd] = senders.get(snd, 0) + 1
            intents[intent] = intents.get(intent, 0) + 1

        print(f"\n[learn] Top domains: {sorted(domains.items(), key=lambda x:-x[1])[:5]}")
        print(f"[learn] Top senders: {sorted(senders.items(), key=lambda x:-x[1])[:5]}")
        print(f"[learn] Intent distribution: {intents}")

        # Load existing patterns and merge new senders
        from .learning import load_patterns, PATTERNS_PATH
        patterns = load_patterns()
        trashable = patterns.setdefault("sender_patterns", {}).setdefault("trashable_senders", {})
        expired_txn = patterns.setdefault("sender_patterns", {}).setdefault("expired_transactional", {})

        new_count = 0
        marketing_intents = {"newsletter", "onboarding", "product_marketing", "promotion", "marketing_general"}
        for addr, freq in senders.items():
            if not addr or "@" not in addr:
                continue
            # Check intent of this sender's emails
            sender_emails = [e for e in results if e.get("sender_email") == addr]
            sender_intent = sender_emails[0].get("intent", "") if sender_emails else ""

            if addr not in trashable and addr not in expired_txn:
                if sender_intent in marketing_intents and freq >= 1:
                    trashable[addr] = {
                        "frequency": freq,
                        "reason": f"Learned from trash: {sender_intent}",
                        "confidence": min(0.85 + 0.03 * (freq - 1), 0.95),
                    }
                    new_count += 1
                elif sender_intent in ("security", "transactional"):
                    expired_txn[addr] = {
                        "frequency": freq,
                        "reason": f"Expired transactional: {sender_intent}",
                        "confidence": min(0.80 + 0.03 * (freq - 1), 0.90),
                    }
                    new_count += 1

        patterns["last_learned"] = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        patterns["total_emails_learned"] = len(results)

        with open(PATTERNS_PATH, "w", encoding="utf-8") as f:
            json.dump(patterns, f, indent=2, ensure_ascii=False)

        print(f"\n[learn] Added {new_count} new sender patterns.")
        print(f"[learn] Total trashable senders: {len(trashable)}")
        print(f"[learn] Total expired transactional: {len(expired_txn)}")
        return 0

    if args.cmd == "patterns":
        from .learning import get_pattern_summary
        summary = get_pattern_summary()
        print("=" * 50)
        print("  GMAIL AGENT — LEARNED PATTERN SUMMARY")
        print("=" * 50)
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print("=" * 50)
        return 0

    if args.cmd == "feedback":
        from .learning import record_feedback
        entry = record_feedback(
            action=args.action,
            email_id=args.email_id,
            sender=args.sender,
            subject=args.subject,
            category=args.category,
            confidence=args.confidence,
        )
        print(f"[feedback] Recorded: {entry['action']} for {entry['sender_email']}")
        print("[feedback] Run 'apply-feedback' to update patterns.")
        return 0

    if args.cmd == "apply-feedback":
        from .learning import apply_feedback_to_patterns
        result = apply_feedback_to_patterns()
        print(f"[apply-feedback] Result: {result}")
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
