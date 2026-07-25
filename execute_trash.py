"""Execute the trash plan: move junk emails to Gmail Trash.
All trashed emails are recoverable for 30 days from Gmail Trash.
"""
import os, sys, json, imaplib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

env_path = Path(__file__).parent / ".env"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

USER = os.environ["GMAIL_USER_EMAIL"]
PASS = os.environ["GMAIL_APP_PASSWORD"]

# Load scan results to get sender info
results = json.loads(Path("scan_results.json").read_text())

# === REFINED SECURITY GUARD ===
# Only TRUE security senders — not Instagram digests
SECURITY_SENDERS = {
    "no-reply@accounts.google.com",
    "security@mail.instagram.com",   # only the security address, NOT posts-recap/follow-suggestions
    "noreply@discord.com",
    "no_reply@mail-service.wondershare.com",
    "no-reply@elevenlabs.io",
    "do-not-reply@account.envato.com",
    "notification@email.meta.com",
    "noreply@email.openai.com",      # privacy policy updates
    "support@salesforce.com",
    "noreply@transactional.n8n.io",  # verification
    "hey@mail.n8n.cloud",            # password reset
    "noreply@vheer.com",             # account password
    "registration@facebookmail.com",  # confirmation code
    "notice@email.anthropic.com",
}
SECURITY_KEYWORDS = (
    "security alert", "new login", "recovery email", "app password",
    "password reset", "verify your email", "verify email address",
    "verification code", "confirm your email", "two-factor",
    "two-factor authentication", "accounts centre",
)

def is_security(sender, subject):
    s = sender.lower()
    addr = s
    if "<" in s and ">" in s:
        addr = s[s.rfind("<")+1:s.rfind(">")].strip()
    subj = subject.lower()
    if addr in SECURITY_SENDERS:
        return True
    for kw in SECURITY_KEYWORDS:
        if kw in subj:
            return True
    return False

# === BUILD TRASH LIST ===
JUNK_DOMAINS = {
    "email.vidiq.com",      # vidIQ milestones
    "kive.ai",              # Kive marketing
    "florafauna.ai",        # FLORA marketing
    "zendrop.com",          # Zendrop Academy
    "email.heygen.com",     # HeyGen marketing
    "learn.heygen.com",     # HeyGen events
    "mail.adobe.com",       # Adobe marketing
    "m.elements.envato.com",# Envato marketing
    "engage.canva.com",     # Canva marketing
    "email.openai.com",     # OpenAI/ChatGPT marketing
    "announce.fiverr.com",  # Fiverr marketing
    "email.epidemicsound.com", # Epidemic Sound
    "tryexponent.com",      # Exponent
    "blog.reve.com",        # Reve contest
    "substack.com",         # Finzar newsletter
    "creator.patreon.com",  # Finzar patreon
    "gentube.app",          # GenTube welcome
    "roboneo.com",          # RoboNeo welcome
    "akool.io",             # Akool welcome
    "openrouter.ai",        # OpenRouter marketing
    "email.anthropic.com",  # Anthropic privacy (duplicate info)
}
JUNK_FULL_SENDERS = {
    "googlecommunityteam-noreply@google.com",
    "businessprofile-noreply@google.com",   # business profile welcome/marketing
    "googleplay-noreply@google.com",
    "google-maps-noreply@google.com",
    "google-gemini-noreply@google.com",
    "googleaistudio-noreply@google.com",
    "CloudPlatform-noreply@google.com",
    "noreply@youtube.com",
    "agentforcesalesagent@salesforce.com",
    "system-sg@notice.alibabacloud.com",     # Alibaba welcome
    "newsletter@info.alibabacloud.com",      # Alibaba newsletter
    "hello@info.n8n.io",                    # n8n onboarding/marketing
    "onboarding@info.n8n.io",
    "hey@mail.n8n.cloud",                   # workflow error (not password reset)
    "posts-recap@mail.instagram.com",        # IG digests
    "follow-suggestions@mail.instagram.com", # IG follow suggestions
    "team@kive.ai",
}
# n8n workflow error → trash (UID 98). But password reset → keep (UID 96).
# We handle mail.n8n.cloud by subject: "password reset" stays, rest trashed.

trash_list = []
keep_list = []

for e in results:
    uid = e["uid"]
    sender = e["sender"]
    subject = e["subject"]
    
    sender_lower = sender.lower()
    addr = sender_lower
    if "<" in sender_lower and ">" in sender_lower:
        addr = sender_lower[sender_lower.rfind("<")+1:sender_lower.rfind(">")].strip()
    domain = addr.split("@")[-1] if "@" in addr else addr
    subj_lower = subject.lower()
    
    # SECURITY GUARD
    if is_security(sender, subject):
        keep_list.append((uid, sender, subject, "SECURITY"))
        continue
    
    # n8n special: keep password reset, trash workflow error + marketing
    if "mail.n8n.cloud" in addr:
        if "password reset" in subj_lower:
            keep_list.append((uid, sender, subject, "n8n password reset"))
            continue
        else:
            trash_list.append((uid, sender, subject, "n8n non-security"))
            continue
    
    # Check exact sender match
    if addr in JUNK_FULL_SENDERS:
        trash_list.append((uid, sender, subject, f"junk sender ({addr})"))
        continue
    
    # Check junk domains
    if domain in JUNK_DOMAINS:
        trash_list.append((uid, sender, subject, f"junk domain ({domain})"))
        continue
    
    # Alibaba Cloud — all marketing
    if "alibabacloud" in domain:
        trash_list.append((uid, sender, subject, "Alibaba marketing"))
        continue
    
    # Check for "confirm your subscription" from substack
    if "confirm your subscription" in subj_lower:
        keep_list.append((uid, sender, subject, "subscription confirm"))
        continue
    
    # Default: keep
    keep_list.append((uid, sender, subject, "default keep"))

print("=" * 90)
print(f"FINAL TRASH PLAN")
print(f"  Total emails: {len(results)}")
print(f"  TO TRASH:     {len(trash_list)}")
print(f"  TO KEEP:      {len(keep_list)}")
print("=" * 90)

print(f"\nTRASH ({len(trash_list)} emails):")
for uid, sender, subject, reason in sorted(trash_list):
    print(f"  UID {uid:>3} | {reason:30s} | {sender[:40]:40s} | {subject[:45]}")

print(f"\nKEEP ({len(keep_list)} emails):")
for uid, sender, subject, reason in sorted(keep_list):
    print(f"  UID {uid:>3} | {reason:30s} | {sender[:40]:40s} | {subject[:45]}")

# === EXECUTE ===
print(f"\n{'=' * 90}")
print(f"CONNECTING TO GMAIL AND TRASHING {len(trash_list)} EMAILS...")
print(f"{'=' * 90}")

conn = imaplib.IMAP4_SSL("imap.gmail.com")
conn.login(USER, PASS)

trashed = 0
errors = 0
for uid, sender, subject, reason in sorted(trash_list):
    try:
        conn.select("INBOX")
        # COPY to Trash, then mark deleted
        conn.copy(str(uid), "[Gmail]/Trash")
        conn.store(str(uid), "+FLAGS", "\\Deleted")
        trashed += 1
        print(f"  [OK] UID {uid} -> Trash | {subject[:50]}")
    except Exception as ex:
        errors += 1
        print(f"  [ERR] UID {uid}: {ex}")

conn.expunge()
conn.logout()

print(f"\n{'=' * 90}")
print(f"DONE! Trashed: {trashed} | Errors: {errors} | Kept: {len(keep_list)}")
print(f"All trashed emails are in Gmail Trash, recoverable for 30 days.")
print(f"{'=' * 90}")
