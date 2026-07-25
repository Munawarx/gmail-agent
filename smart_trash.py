"""Smart second-pass: trash obvious junk that the basic classifier missed.
SECURITY GUARD: NEVER trash anything from security domains or containing security phrases.
"""
import os, sys, json, imaplib, email
from email.header import decode_header
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env
env_path = Path(__file__).parent / ".env"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

USER = os.environ["GMAIL_USER_EMAIL"]
PASS = os.environ["GMAIL_APP_PASSWORD"]

results = json.loads(Path("scan_results.json").read_text())

# === SECURITY GUARD — these NEVER get trashed ===
SECURITY_DOMAINS = {
    "accounts.google.com", "discord.com", "roblox.com", "paypal.com",
    "apple.com", "microsoft.com", "amazon.com", "mail.instagram.com",
    "security@mail.instagram.com", "elevenlabs.io",
}
SECURITY_PHRASES = (
    "security alert", "sign-in", "sign in", "new login", "unusual activity",
    "verify", "verification", "confirm your", "confirm email", "activate your",
    "password", "2-step", "two-step", "two factor", "2fa", "account recovery",
    "we detected", "suspicious", "your account has been", "recovery code",
    "billing", "invoice", "receipt", "payment received", "your order",
    "login code", "one-time code", "authentication",
    "app password", "two-factor", "accounts centre",
)

def is_security(sender, subject, body):
    s = sender.lower()
    text = f"{subject} {body}".lower()
    # Extract bare email
    addr = s
    if "<" in s and ">" in s:
        addr = s[s.rfind("<")+1:s.rfind(">")]
    domain = addr.split("@")[-1] if "@" in addr else ""
    for d in SECURITY_DOMAINS:
        if domain == d or domain.endswith("." + d) or addr == d:
            return True
    if any(p in text for p in SECURITY_PHRASES):
        return True
    return False

# === AGGRESSIVE JUNK RULES ===
# Senders that are pure noise/marketing → safe to trash
JUNK_SENDERS = {
    # vidIQ view milestone notifications (pure noise)
    "email.vidiq.com",
    # Kive marketing
    "kive.ai",
    # FLORA/FloraFauna marketing
    "florafauna.ai",
    # Zendrop Academy marketing
    "zendrop.com",
    # HeyGen marketing (not security)
    "email.heygen.com",
    "learn.heygen.com",
    # Adobe marketing
    "mail.adobe.com",
    # Envato marketing
    "elements.envato.com",
    "m.elements.envato.com",
    # Canva marketing
    "engage.canva.com",
    # ChatGPT/OpenAI marketing
    "email.openai.com",
    # Instagram digests (post recaps, follow suggestions — NOT security@mail.instagram.com)
    "posts-recap@mail.instagram.com",
    "follow-suggestions@mail.instagram.com",
    # Fiverr marketing
    "announce.fiverr.com",
    # Epidemic Sound re-engagement
    "email.epidemicsound.com",
    # Exponent marketing
    "tryexponent.com",
    # Reve contest marketing
    "blog.reve.com",
    # Alibaba Cloud marketing
    "notice.alibabacloud.com",
    "info.alibabacloud.com",
    # Google marketing (product updates, not security)
    "googleplay-noreply@google.com",
    "google-maps-noreply@google.com",
    "google-gemini-noreply@google.com",
    "googleaistudio-noreply@google.com",
    "CloudPlatform-noreply@google.com",
    "googlecommunityteam-noreply@google.com",
    "businessprofile-noreply@google.com",
    # YouTube social notification
    "youtube.com",
    # Substack newsletter
    "finzar@substack.com",
    "finzar@creator.patreon.com",
    # Salesforce marketing
    "agentforcesalesagent@salesforce.com",
    # n8n onboarding/marketing (NOT verification or password reset)
    "info.n8n.io",
    "mail.n8n.cloud",
    # OpenRouter welcome/marketing
    "openrouter.ai",
    # Anthropic privacy notice (already seen, not actionable)
    "email.anthropic.com",
    # Welcome emails from apps (GenTube, RoboNeo, Akool, vheer)
    "gentube.app",
    "roboneo.com",
    "akool.io",
    "vheer.com",
    # Google Gemini welcome
    # Alibaba Cloud welcome
    # Google AI Studio
}

# Subjects that indicate pure noise (even from non-listed senders)
JUNK_SUBJECTS = (
    "you reached",  # vidIQ milestones
    "preset of the week",  # Kive
    "posts-recap",  # Instagram digest
    "follow-suggestions",  # Instagram
    "from zero to sales",  # Zendrop
)

# n8n emails: keep verification (transactional.n8n.io) and password reset (mail.n8n.cloud password reset)
# but trash marketing/onboarding (info.n8n.io, mail.n8n.cloud workflow failed)
N8N_KEEP = {
    "transactional.n8n.io",  # verification emails
}
N8N_PASSWORD_RESET_SUBJECT = "password reset"

trash_uids = []
keep_uids = []
reason_map = {}

for e in results:
    uid = e["uid"]
    sender = e["sender"]
    subject = e["subject"]
    body = e["body_preview"]
    
    sender_lower = sender.lower()
    subject_lower = subject.lower()
    text = f"{subject} {body}".lower()
    
    # Extract bare email addr
    addr = sender_lower
    if "<" in sender_lower and ">" in sender_lower:
        addr = sender_lower[sender_lower.rfind("<")+1:sender_lower.rfind(">")]
    domain = addr.split("@")[-1] if "@" in addr else addr
    
    # SECURITY GUARD — never trash
    if is_security(sender, subject, body):
        keep_uids.append(uid)
        reason_map[uid] = "SECURITY/IMPORTANT — keep"
        continue
    
    # n8n: keep verification + password reset, trash marketing
    if "n8n" in domain or "n8n" in addr:
        if domain in N8N_KEEP or N8N_PASSWORD_RESET_SUBJECT in subject_lower:
            keep_uids.append(uid)
            reason_map[uid] = "n8n verification/password — keep"
            continue
        # workflow error notification — keep for now
        if "workflow failed" in subject_lower:
            keep_uids.append(uid)
            reason_map[uid] = "n8n workflow error — keep"
            continue
    
    # Check junk senders
    is_junk = False
    for js in JUNK_SENDERS:
        if domain == js or domain.endswith("." + js) or addr == js or addr.endswith("@" + js):
            is_junk = True
            break
    
    if is_junk:
        trash_uids.append(uid)
        reason_map[uid] = f"Marketing/junk sender ({domain})"
        continue
    
    # Check junk subjects
    if any(js in subject_lower for js in JUNK_SUBJECTS):
        trash_uids.append(uid)
        reason_map[uid] = f"Junk subject pattern"
        continue
    
    # Keep everything else
    keep_uids.append(uid)
    reason_map[uid] = "Not clearly junk — keep"

print("=" * 90)
print(f"SMART SECOND-PASS ANALYSIS")
print(f"Total: {len(results)} emails")
print(f"TRASH: {len(trash_uids)}")
print(f"KEEP:  {len(keep_uids)}")
print("=" * 90)

print("\nEMAILS TO TRASH:")
for uid in sorted(trash_uids):
    e = next(x for x in results if x["uid"] == uid)
    print(f"  UID {uid:>3} | {reason_map[uid]:45s} | {e['sender'][:35]:35s} | {e['subject'][:45]}")

print(f"\nEMAILS TO KEEP ({len(keep_uids)}):")
for uid in sorted(keep_uids):
    e = next(x for x in results if x["uid"] == uid)
    print(f"  UID {uid:>3} | {reason_map[uid]:45s} | {e['sender'][:35]:35s} | {e['subject'][:45]}")

# Ask for confirmation by saving the plan
Path("trash_plan.json").write_text(json.dumps({
    "trash_uids": sorted(trash_uids),
    "keep_uids": sorted(keep_uids),
    "reasons": {str(k): v for k, v in reason_map.items()},
}, indent=2))
print(f"\nPlan saved to trash_plan.json")
print(f"\nTo execute: {len(trash_uids)} emails will be moved to Gmail Trash (recoverable for 30 days)")
