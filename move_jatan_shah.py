"""Move all Jatan Shah (jatanshah.in) emails into a 'Jatan Shah' label and
archive them out of INBOX. Live-printing, resilient to drops."""
import imaplib
import re
import time
from pathlib import Path
from email.header import decode_header as _dh

LABEL = "Jatan Shah"
STATE = Path(__file__).resolve().parent / ".jatan_shah_state.json"


def dec(v):
    if not v:
        return ""
    out = []
    for b, e in _dh(v):
        try:
            out.append(b.decode(e or "utf-8", "replace") if isinstance(b, bytes) else b)
        except Exception:
            out.append(str(b))
    return " ".join(out)


def load_creds():
    env = Path(__file__).resolve().parent / ".env.second"
    creds = {}
    for line in env.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip().strip('"').strip("'")
    return creds["GMAIL_USER_EMAIL"], creds["GMAIL_APP_PASSWORD"]


def reconnect(user, pw):
    c = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    c.login(user, pw)
    return c


def main():
    user, pw = load_creds()
    done = set()
    if STATE.exists():
        try:
            done = set(eval(STATE.read_text()).get("done", []))
        except Exception:
            done = set()

    c = reconnect(user, pw)
    c.select('"[Gmail]/All Mail"', readonly=True)
    typ, data = c.search(None, 'X-GM-RAW "Jatan Shah"')
    uids = data[0].split() if data and data[0] else []
    c.logout()

    true_uids = []
    for uid in uids:
        c = reconnect(user, pw)
        c.select('"[Gmail]/All Mail"', readonly=True)
        _, d = c.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (FROM)])")
        if d and d[0] and isinstance(d[0], tuple):
            raw = d[0][1].decode("utf-8", "replace")
            fm = re.search(r"From: (.*)", raw)
            s = dec(fm.group(1).strip()) if fm else ""
            if "jatanshah.in" in s.lower():
                true_uids.append(uid.decode())
        c.logout()

    pending = [u for u in true_uids if u not in done]
    print(f"Total jatanshah.in: {len(true_uids)} | done already: {len(done)} | pending: {len(pending)}", flush=True)

    labeled = archived = 0
    for i, uid in enumerate(pending):
        try:
            c = reconnect(user, pw)
            c.select('"[Gmail]/All Mail"')  # read-write
            c.store(uid, "+X-GM-LABELS", '("Jatan Shah")')
            labeled += 1
            c.store(uid, "-X-GM-LABELS", '\\\\Inbox')  # archive out of INBOX
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
        if (i + 1) % 10 == 0:
            print(f"  progress {i+1}/{len(pending)} labeled={labeled} archived={archived}", flush=True)
        time.sleep(0.3)

    print(f"\nDONE: labeled={labeled} archived={archived}", flush=True)

    c = reconnect(user, pw)
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
