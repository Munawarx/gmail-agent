# Contributing

Thanks for your interest in improving the Gmail Agent! This project follows a
small, safety-first contribution process.

## Ground Rules

1. **Safety is non-negotiable.** This tool touches a real mailbox. Any change
   that could cause unintended permanent deletion, send mail, or weaken the
   "never trash security/account/financial mail" guardrail requires explicit
   maintainer review.
2. **No secrets.** Never commit `.env`, `token.json`, `credentials.json`, or
   any real email content. `.gitignore` already excludes them.
3. **Tests first.** Add or update a test for any behavioral change.
   `uv run pytest -q` must stay green in CI.
4. **Dry-run by default.** New actions must default to `--dry-run` /
   `DRY_RUN=true` and require an explicit confirmation to mutate mail.

## Development Setup

```bash
git clone https://github.com/Munawarx/gmail-agent.git
cd gmail-agent
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -e ".[test]"                          # or: uv sync
cp .env.example .env                              # fill in credentials
```

## Workflow

1. Fork the repo and create a branch: `git checkout -b fix/my-change`.
2. Make your change with tests.
3. Run `uv run pytest -q` (or `python -m pytest -q`).
4. Commit with a clear message (Conventional Commits preferred, e.g.
   `fix: ...`, `feat: ...`).
5. Push and open a Pull Request against `master`.
6. Describe *what* changed and *why*, and note any mailbox-safety impact.

## Code Style

- `ruff` (or `flake8`) for lint; keep functions small and documented.
- Every user-data function should have a docstring stating what it reads/writes.
- Type hints are required on new public functions.

## Reporting Issues

Open a public issue for bugs/feature requests. For security issues, see
[SECURITY.md](SECURITY.md) and report privately.
