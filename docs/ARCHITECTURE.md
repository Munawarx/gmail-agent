# Intelligent Gmail Management Agent — Architecture & Implementation Plan

> Deliverables 1–10 requested in the brief, answered concretely. The working
> code lives in this repo (`gmail_agent/`); this document is the design record.

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   SCHEDULER (daily / cron)                    │
│              (Windows Task Scheduler / cron / Hermes cron)    │
└───────────────────────────┬─────────────────────────────────┘
                            │  triggers one pass
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                     AGENT  (agent.py)                         │
│  * load checkpoint (Store)                                    │
│  * fetch next unprocessed email (GmailClient, oldest-first)   │
│  * classify (Classifier)                                      │
│  * act (trash / keep)  — DRY_RUN respected                    │
│  * save checkpoint + append log (Store)                       │
│  * loop until inbox exhausted or cap reached                  │
└───────┬───────────────┬───────────────┬──────────────────────┘
        │               │               │
        ▼               ▼               ▼
   ┌─────────┐    ┌────────────┐   ┌──────────────┐
   │ Gmail   │    │ Classifier │   │   Store      │
   │ API     │    │ (rules+LLM)│   │  (SQLite)    │
   │ (read/  │    │            │   │ checkpoint + │
   │  trash) │    └────────────┘   │ processing   │
   └─────────┘                     │ logs         │
        ▲                           └──────────────┘
        │ OAuth token.json
   ┌────┴──────────┐
   │ Google account│
   │ (your mailbox)│
   └───────────────┘
```

Single-process, stateless-between-runs design. All durable state is in SQLite.
The agent is idempotent: re-running never double-processes (checkpoint + log
dedup).

---

## 2. Technology Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11 | Mature email/Google ecosystem |
| Gmail access | Google API Client (`google-api-python-client`) | Official, stable |
| Auth | `google-auth-oauthlib` (OAuth 2.0) | Desktop flow, token refresh |
| Storage | SQLite | Zero-infra, file-based, atomic, resumable |
| Optional LLM | OpenAI API | Escalation for ambiguous emails |
| Scheduling | OS cron / Windows Task Scheduler / Hermes cron | No extra service |
| Tests | pytest (mocked Gmail) | Fast, offline, no quota use |

---

## 3. AI Model Selection

* **Primary: deterministic rule engine** (`classifier.py`). Covers ~95% of
  volume (OTP/social/promo) with regex + sender heuristics. Fast, free,
  explainable — satisfies "minimize API calls" and "high speed".
* **Optional escalation: LLM** (`USE_LLM=true`). For emails the rules score as
  borderline, an LLM (e.g. `gpt-4o-mini`) makes the call. Off by default to
  keep cost ~$0.
* No fine-tuning required; the rules are the model.

---

## 4. Gmail API Integration

* **Scope:** `gmail.modify` (read + label/trash; never delete permanently).
* **Read:** `users.messages.list` with `q="-in:trash"`, `orderBy="date"`
  (oldest first) → resume from checkpoint id.
* **Fetch:** `users.messages.get` `format=full` → parse headers + text/plain.
* **Act:** `users.messages.trash` (recoverable; not `delete`).
* **Auth:** OAuth desktop flow → `token.json`; auto-refreshes on expiry.
* **Quota control:** one `list` page (~100 ids) + one `get` per unprocessed
  email; checkpoint avoids re-listing already-done mail.

---

## 5. Persistent Memory Strategy

Two-part state in SQLite (`state/gmail_agent.db`):

* **`checkpoint`** table (single row): `last_email_id`, `last_timestamp`,
  `updated_at`. This is the resume cursor.
* **`logs`** table: one row per email processed (subject, sender, received_at,
  category, action, status, error, ts). Doubles as audit + dedup source.

On startup the agent reads the checkpoint, skips everything at-or-before it,
and processes forward. After each email it writes the checkpoint — so a crash
mid-run loses at most the one in-flight email (retried next run).

---

## 6. Database Schema

```sql
CREATE TABLE checkpoint (
  id INTEGER PRIMARY KEY CHECK (id = 1),   -- always row 1
  last_email_id TEXT NOT NULL,
  last_timestamp TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE logs (
  rowid INTEGER PRIMARY KEY AUTOINCREMENT,
  email_id TEXT NOT NULL,
  subject TEXT,
  sender TEXT,
  received_at TEXT,
  category TEXT,
  action TEXT,            -- 'trash' | 'none'
  status TEXT,            -- 'processed' | 'skipped' | 'error' | 'retry'
  error TEXT,
  ts TEXT NOT NULL
);
CREATE INDEX idx_logs_email ON logs(email_id);
```

`email_id` dedup: an email already in `logs` with status `processed`/`error`
is skipped (prevents double work; errors retry because they're re-fetched by
id and re-evaluated).

---

## 7. Agent Workflow Diagram

```
START
  │
  ▼
load checkpoint (last_id, last_ts)
  │
  ▼
list messages oldest-first (skip until last_id reached)
  │
  for each message id:
  │     get_message(id)
  │     already_processed? ──yes──▶ skip (count+1) ──▶ next
  │     │ no
  │     ▼
  │   classify(email) ──▶ Category
  │     │
  │     ├─ OTP_EXPIRED ──▶ trash()  ──▶ log+checkpoint
  │     ├─ SOCIAL       ──▶ trash()  ──▶ log+checkpoint
  │     ├─ PROMO        ──▶ trash()  ──▶ log+checkpoint
  │     └─ KEEP/OTP_VALID▶ log(no action)+checkpoint
  │     │
  │   on error: log(status=error), skip THIS email, continue
  │
  ▼
no more messages? ──▶ STOP gracefully
  │
  ▼ (next scheduled run)
resume from new checkpoint
```

---

## 8. Folder / Action Rules

| Category | Detection | Action |
|---|---|---|
| OTP_EXPIRED | OTP phrase + "expired/invalid" | → Trash |
| OTP_VALID | OTP phrase, still valid | Keep |
| SOCIAL | social domain (linkedin/facebook/ig/x/threads/reddit…) or social phrases | → Trash |
| PROMO | promo sender + ≥1 promo phrase, or ≥2 promo phrases | → Trash |
| KEEP / IMPORTANT | no rule matched | Keep |

Trash is **recoverable** (Gmail Trash, 30-day retention) — safer than permanent
delete, and easy to audit.

---

## 9. Error Recovery Strategy

* **Per-email try/except:** a failure logs `status='error'` with the message,
  skips *only that email*, and continues. The checkpoint does **not** advance
  past a failed email, so it is retried on the next run.
* **Checkpoint durability:** written after every successful email → crash
  loses at most one in-flight message.
* **API/quota errors:** surfaced in logs; the run ends gracefully and resumes
  later (Gmail 429s back off naturally; token refreshes automatically).
* **Idempotency:** `already_processed()` prevents any email being analyzed
  twice even if the checkpoint and log momentarily disagree.

---

## 10. Production-Ready Implementation Plan & Best Practices

**Implementation (done in this repo):**
1. `config.py` — `.env` loader (secrets never printed).
2. `schemas.py` — `Email`, `Classification`, `LogEntry`, `Category` enum.
3. `store.py` — SQLite checkpoint + logs, dedup, error tracking.
4. `gmail.py` — OAuth client: list (chronological), get, trash.
5. `classifier.py` — rule engine (OTP/social/promo) + optional LLM hook.
6. `agent.py` — the resumable loop.
7. `cli.py` — `run` / `status` / `auth` commands.
8. `tests/test_agent.py` — 10 tests, Gmail mocked, all offline.

**Best practices:**
* Run **`--dry-run`** first; review the logs; only then go live.
* Schedule off-peak; cap with `MAX_EMAILS` for the first few runs.
* Keep `token.json` out of git (add to `.gitignore`).
* Monitor `status` output for growing `error` counts.
* For very large inboxes, raise `maxResults` and run in capped batches daily.
* Prefer Trash over delete; periodically review Trash before empty.

**Verification:**
* `uv run pytest -q` → all unit tests pass (mocked Gmail).
* `uv run python -m gmail_agent.cli run --dry-run` → real inbox preview, no
  writes.
* `uv run python -m gmail_agent.cli status` → checkpoint + error counts.
