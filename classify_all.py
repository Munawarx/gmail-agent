"""Classify all scanned emails using the agent's own classifier.
Outputs a full report + recommendation per email.
"""
import os, sys, json
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gmail_agent.schemas import Email, Category
from gmail_agent import classifier

results = json.loads(Path("scan_results.json").read_text())

cats = {}
details = []

for e in results:
    email_obj = Email(
        id=str(e["uid"]),
        thread_id=str(e["uid"]),
        subject=e["subject"],
        sender=e["sender"],
        received_at=e["date"],
        snippet=e["body_preview"][:200],
        body_text=e["body_preview"],
        labels=["INBOX"],
    )
    cls = classifier.classify(email_obj)
    cat = cls.category.value
    cats[cat] = cats.get(cat, 0) + 1
    action = "TRASH" if cat in ("otp_expired", "social", "promo") else "KEEP"
    details.append({
        "uid": e["uid"],
        "date": e["date"],
        "sender": e["sender"][:60],
        "subject": e["subject"][:70],
        "category": cat,
        "action": action,
        "reason": cls.reason,
        "confidence": cls.confidence,
    })

print("=" * 90)
print(f"TOTAL EMAILS ANALYZED: {len(details)}")
print("=" * 90)
print("\nCATEGORY BREAKDOWN:")
for cat, count in sorted(cats.items(), key=lambda x: -x[1]):
    action = "TRASH" if cat in ("otp_expired", "social", "promo") else "KEEP"
    print(f"  {cat:20s}  {count:4d}  -> {action}")
print()

trash_count = sum(1 for d in details if d["action"] == "TRASH")
keep_count = sum(1 for d in details if d["action"] == "KEEP")
print(f"TOTAL TO TRASH: {trash_count}")
print(f"TOTAL TO KEEP:  {keep_count}")

print("\n" + "=" * 90)
print("EMAILS TO TRASH:")
print("=" * 90)
for d in details:
    if d["action"] == "TRASH":
        print(f"  UID {d['uid']:>3} | {d['category']:15s} | {d['date'][:25]:25s} | {d['sender'][:35]:35s} | {d['subject'][:50]}")

print("\n" + "=" * 90)
print("EMAILS TO KEEP (IMPORTANT/OTP/OTHER):")
print("=" * 90)
for d in details:
    if d["action"] == "KEEP":
        print(f"  UID {d['uid']:>3} | {d['category']:15s} | {d['date'][:25]:25s} | {d['sender'][:35]:35s} | {d['subject'][:50]}")

# Save classification results
Path("classification_results.json").write_text(json.dumps(details, indent=2, ensure_ascii=False))
print(f"\nDetailed results saved to classification_results.json")
