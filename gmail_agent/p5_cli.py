"""P5 CLI — Multi-account Gmail triage commands.

Usage:
  python -m gmail_agent.p5_cli run [--dry-run] [--max-emails N]
  python -m gmail_agent.p5_cli rules [--account EMAIL]
  python -m gmail_agent.p5_cli rule-add SENDER ACTION [--account EMAIL]
  python -m gmail_agent.p5_cli rule-del SENDER
  python -m gmail_agent.p5_cli checkpoints [--account EMAIL]
  python -m gmail_agent.p5_cli report [--account EMAIL]
  python -m gmail_agent.p5_cli accounts
  python -m gmail_agent.p5_cli config
"""
from __future__ import annotations

import sys
import argparse
from pathlib import Path

from .multiaccount import load_p5_config, load_accounts, P5Config
from .p5_store import P5Store
from .config import DEFAULT_DB


def cmd_run(args):
    """Run triage on all accounts."""
    from .p5_engine import P5TriageEngine

    config = load_p5_config()
    if not config.accounts:
        print("❌ No accounts configured. Add to .env:")
        print("  ACCOUNT_1_EMAIL=you@gmail.com")
        print("  ACCOUNT_1_APP_PASSWORD=your-16-char-app-password")
        return 1

    dry_run = args.dry_run or config.dry_run
    engine = P5TriageEngine(config, DEFAULT_DB)

    try:
        if args.account:
            # Run specific account
            acct = next(
                (a for a in config.accounts if a.email == args.account), None
            )
            if not acct:
                print(f"❌ Account not found: {args.account}")
                return 1
            engine.triage_account(acct, dry_run=dry_run, max_emails=args.max_emails)
        else:
            engine.run_all(dry_run=dry_run, max_emails=args.max_emails)
    finally:
        engine.close()
    return 0


def cmd_rules(args):
    """List all rules."""
    store = P5Store(DEFAULT_DB)
    account = args.account or "*"
    rules = store.get_all_rules(account)

    if not rules:
        print("No rules yet.")
        return 0

    print(f"\n{'Pattern':<40} {'Type':<10} {'Action':<8} {'Approved':<15} {'Conf':>6}")
    print("-" * 85)
    for r in rules:
        print(f"{r.pattern:<40} {r.pattern_type:<10} {r.action:<8} {r.approved_by:<15} {r.confidence:>6.2f}")
    print(f"\nTotal: {len(rules)} rules")
    store.close()
    return 0


def cmd_rule_add(args):
    """Add or update a rule."""
    store = P5Store(DEFAULT_DB)
    account = args.account or "*"
    action = args.action.lower()

    if action not in ("trash", "keep", "flag"):
        print(f"❌ Invalid action '{action}'. Use: trash, keep, flag")
        return 1

    written = store.upsert_rule(
        account=account,
        pattern=args.sender.lower(),
        pattern_type="sender",
        action=action,
        approved_by="user",
        confidence=1.0,
    )

    if written:
        print(f"✅ Rule added: {args.sender} → {action} (account={account})")
    else:
        print(f"⚠️  Blocked — existing user-approved rule for {args.sender}")
    store.close()
    return 0


def cmd_rule_del(args):
    """Delete a rule."""
    store = P5Store(DEFAULT_DB)
    account = args.account or "*"
    deleted = store.delete_rule(account, args.sender)
    if deleted:
        print(f"🗑️  Rule deleted: {args.sender}")
    else:
        print(f"❌ No rule found for {args.sender}")
    store.close()
    return 0


def cmd_checkpoints(args):
    """Show checkpoints for an account."""
    store = P5Store(DEFAULT_DB)
    account = args.account or ""
    if not account:
        config = load_p5_config()
        if config.primary:
            account = config.primary.email
        else:
            print("No account specified.")
            return 1

    cps = store.get_all_checkpoints(account)
    if not cps:
        print(f"No checkpoints for {account}")
        return 0

    print(f"\nCheckpoints for {account}:")
    print(f"{'Label':<25} {'Last Msg-ID':<20} {'Proc':>5} {'Trash':>5} {'Keep':>5} {'Flag':>5} {'Stop':<15}")
    print("-" * 100)
    for cp in cps:
        print(f"{cp.label:<25} {cp.last_message_id[:18]:<20} {cp.processed_count:>5} "
              f"{cp.trashed_count:>5} {cp.kept_count:>5} {cp.flagged_count:>5} {cp.stop_reason:<15}")
    store.close()
    return 0


def cmd_report(args):
    """Show last run report for an account."""
    store = P5Store(DEFAULT_DB)
    account = args.account or ""
    if not account:
        config = load_p5_config()
        if config.primary:
            account = config.primary.email
        else:
            print("No account specified.")
            return 1

    summary = store.get_last_summary(account)
    if not summary:
        print(f"No run reports for {account}")
        return 0

    print(f"\n{'='*60}")
    print(f"  LAST RUN REPORT: {account}")
    print(f"{'='*60}")
    print(f"  Run ID:        {summary.run_id}")
    print(f"  Labels:        {summary.labels_scanned}")
    print(f"  Analyzed:      {summary.emails_analyzed}")
    print(f"  Trashed:       {summary.emails_trashed}")
    print(f"  Retained:      {summary.emails_retained}")
    print(f"  Flagged:       {summary.emails_flagged}")
    print(f"  New Rules:     {summary.new_rules_learned}")
    print(f"  Errors:        {summary.errors}")
    print(f"  Checkpoint:    {summary.checkpoint_label} / {summary.checkpoint_message_id[:20]}")
    if summary.remaining:
        print(f"  Remaining:     {summary.remaining}")
    print(f"  Started:       {summary.started_at}")
    print(f"  Completed:     {summary.completed_at}")
    print(f"{'='*60}")
    store.close()
    return 0


def cmd_accounts(args):
    """List configured accounts."""
    accounts = load_accounts()
    if not accounts:
        print("No accounts configured.")
        return 1

    print(f"\nConfigured accounts ({len(accounts)}):")
    for i, a in enumerate(accounts, 1):
        prim = " (PRIMARY)" if a.is_primary else ""
        print(f"  {i}. {a.email}{prim}")
    return 0


def cmd_config(args):
    """Show P5 config."""
    config = load_p5_config()
    print(f"\nP5 Configuration:")
    print(f"  Accounts:        {len(config.accounts)}")
    print(f"  Delete cap:      {config.delete_cap}")
    print(f"  Confidence trash: {config.confidence_trash}")
    print(f"  Confidence flag:  {config.confidence_flag}")
    print(f"  Batch size:      {config.batch_size}")
    print(f"  Dry run:         {config.dry_run}")
    print(f"  Use LLM:         {config.use_llm}")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="P5 Multi-Account Gmail Triage Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    # run
    p_run = sub.add_parser("run", help="Run triage on all accounts")
    p_run.add_argument("--dry-run", action="store_true", help="Analyze without deleting")
    p_run.add_argument("--max-emails", type=int, default=0, help="Max emails per run")
    p_run.add_argument("--account", type=str, default="", help="Specific account only")

    # rules
    p_rules = sub.add_parser("rules", help="List all rules")
    p_rules.add_argument("--account", type=str, default="")

    # rule-add
    p_add = sub.add_parser("rule-add", help="Add/update a rule")
    p_add.add_argument("sender", help="Sender email or domain")
    p_add.add_argument("action", choices=["trash", "keep", "flag"], help="Action")
    p_add.add_argument("--account", type=str, default="*")

    # rule-del
    p_del = sub.add_parser("rule-del", help="Delete a rule")
    p_del.add_argument("sender", help="Sender email or domain")
    p_del.add_argument("--account", type=str, default="*")

    # checkpoints
    p_cp = sub.add_parser("checkpoints", help="Show checkpoints")
    p_cp.add_argument("--account", type=str, default="")

    # report
    p_rep = sub.add_parser("report", help="Show last run report")
    p_rep.add_argument("--account", type=str, default="")

    # accounts
    sub.add_parser("accounts", help="List configured accounts")

    # config
    sub.add_parser("config", help="Show P5 config")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "run": cmd_run,
        "rules": cmd_rules,
        "rule-add": cmd_rule_add,
        "rule-del": cmd_rule_del,
        "checkpoints": cmd_checkpoints,
        "report": cmd_report,
        "accounts": cmd_accounts,
        "config": cmd_config,
    }

    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
