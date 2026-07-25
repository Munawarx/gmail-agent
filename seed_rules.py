"""Seed the P5 rules table with all patterns learned from prior Trash analysis.

These are the confirmed patterns from analyzing 24 trashed emails + 22 live
promotions/updates trashed in prior runs. Writes to the rules table with
approved_by='learned' so the agent can apply them immediately without
re-reasoning.
"""
from gmail_agent.p5_store import P5Store
from gmail_agent.config import DEFAULT_DB

# Rules learned from Trash analysis + live scans
LEARNED_RULES = [
    # ── Auto-trash: newsletters ──
    ("noreply@email.heygen.com", "sender", "trash", "Marketing newsletter"),
    ("mail@mail.adobe.com", "domain", "trash", "Adobe marketing"),
    ("engage.canva.com", "domain", "trash", "Canva marketing"),
    ("email.vidiq.com", "domain", "trash", "vidIQ newsletter (4x deleted)"),
    ("mail.instagram.com", "domain", "trash", "Instagram recap/suggestions"),
    ("elements.envato.com", "domain", "trash", "Envato newsletter"),
    ("notify@blog.reve.com", "sender", "trash", "Reve product blog"),
    ("finzar@creator.patreon.com", "sender", "trash", "Patreon/Finzar marketing"),
    ("team@kive.ai", "domain", "trash", "Kive marketing (2x deleted)"),
    ("info.alibabacloud.com", "domain", "trash", "Alibaba marketing"),
    ("mail.adobe.com", "domain", "trash", "Adobe marketing (3x deleted)"),

    # ── Auto-trash: onboarding (after 24h) ──
    ("onboarding@info.n8n.io", "sender", "trash", "n8n trial ended"),
    ("admin@akool.io", "sender", "trash", "Akool welcome (stale)"),
    ("hi@roboneo.com", "sender", "trash", "RoboNeo stale welcome"),
    ("zendrop.com", "domain", "trash", "Zendrop sales training"),

    # ── Keep: protected senders ──
    ("noreply@discord.com", "sender", "keep", "Discord mentions/alerts"),
    ("email.openai.com", "domain", "keep", "OpenAI/ChatGPT important"),
    ("support@salesforce.com", "sender", "keep", "Salesforce verification"),
    ("finzar@substack.com", "sender", "keep", "Substack subscription"),
    ("noreply@vheer.com", "sender", "keep", "Account credentials"),
    ("accounts.google.com", "domain", "keep", "Google security alerts"),
]

print("Seeding P5 rules table...")

store = P5Store(DEFAULT_DB)
count = 0

for pattern, ptype, action, reason in LEARNED_RULES:
    # Extract the actual pattern for matching
    if ptype == "domain":
        match_pattern = pattern
    else:
        match_pattern = pattern

    written = store.upsert_rule(
        account="*",  # Global rule
        pattern=match_pattern.lower(),
        pattern_type=ptype,
        action=action,
        approved_by="learned",
        confidence=0.90 if action == "trash" else 1.0,
    )
    if written:
        count += 1
        icon = "🔴" if action == "trash" else "🟢"
        print(f"  {icon} {match_pattern:<40} → {action}")

store.close()
print(f"\n✅ Seeded {count} rules (global, account='*')")
