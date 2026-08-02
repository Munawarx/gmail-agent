"""Archive all emails labeled 'Jatan Shah' out of INBOX.
Use chr(92) for backslash to avoid all quoting/escaping ambiguity.
"""
import imaplib
import time
from pathlib import Path

LABEL = "Jatan Shah"
STATE = Path(__file__).resolve().parent / ".jatan_shah_state.json"
BS = chr(92)  # backslash
INBOX_FLAG = '(' + BS + 'Inbox)'  # -> (\Inbox) on the wire


def load_creds():
    env = Path(__file__).resolve().parent / ".env.second"
    creds = {}
    for line in env.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")
    return creds["GMAIL_USER_EMAIL"], creds["GMAIL_APP_PASSWORD"]


def main():
    user, pw = load_creds()
    print("INBOX_FLAG wire value:", INBOX_FLAG, flush=True)
    done = set()
    if STATE.exists():
        try:
            done = set(eval(STATE.read_text()).get("done", []))
        except Exception:
            done = set()

    c = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    c.login(user, pw)
    c.select('"[Gmail]/All Mail"', readonly=True)
    typ, data = c.search(None, 'X-GM-RAW "label:Jatan Shah"')
    uids = [u.decode() for u in (data[0].split() if data and data[0] else [])]
    c.logout()

    pending = [u for u in uids if u not in done]
    print(f"Labeled 'Jatan Shah': {len(uids)} | archived already: {len(done)} | pending: {len(pending)}", flush=True)

    archived = 0
    for i, uid in enumerate(pending):
        try:
            c = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            c.login(user, pw)
            c.select('"[Gmail]/All Mail"')  # read-write
            c.store(uid, "-X-GM-LABELS", INBOX_FLAG)  # remove Inbox label = archive
            archived += 1
            done.add(uid)
            STATE.write_text(repr({"done": sorted(done, key=int)}))
        except Exception as e:
            print(f"  FAIL {uid}: {e}", flush=True)
            try:
                c.logout()
            except Exception:
                pass
            time.sleep(2)
            continue
        finally:
            try:
                c.logout()
            except Exception:
                pass
        if (i + 1) % 20 == 0:
            print(f"  progress {i+1}/{len(pending)} archived={archived}", flush=True)
        time.sleep(0.3)

    print(f"\nDONE: archived={archived}", flush=True)

    c = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    c.login(user, pw)
    c.select('"[Gmail]/All Mail"', readonly=True)
    typ, data = c.search(None, 'X-GM-RAW "label:Jatan Shah"')
    n_label = len(data[0].split()) if data and data[0] else 0
    c.select('"INBOX"', readonly=True)
    typ, data = c.search(None, 'X-GM-RAW "label:Jatan Shah"')
    n_inbox = len(data[0].split()) if data and data[0] else 0
    c.logout()
    print(f"VERIFY: {n_label} labeled | {n_inbox} still in INBOX", flush=True)


if __name__ == "__main__":
    main()
