# Security Policy

## Supported Versions

The latest `master` branch is supported with security fixes.

## Reporting a Vulnerability

If you discover a security vulnerability in this project, **please do not open a
public issue.** Instead, report it privately:

- Email: [REDACTED — set a real contact before publishing]
- Or use GitHub's private vulnerability reporting (Security → Report a
  vulnerability) if enabled.

We aim to acknowledge reports within 72 hours and provide a remediation plan
within 14 days.

## Security Design Notes

This tool is built safety-first:

- **Recoverable only:** it moves mail to Gmail Trash (30-day retention). It
  never permanently deletes.
- **Credentials:** Gmail App Password / OAuth token are stored locally in
  `.env` / `token.json`, which are git-ignored. Never commit them.
- **LLM boundary:** the optional LLM escalation tier can only *promote* a
  borderline email to `PROMO` / `SOCIAL` / `OTP_EXPIRED`. It can **never**
  downgrade security/account/financial mail, which is always protected by the
  rule engine.
- **Least privilege:** OAuth uses the `gmail.modify` scope only (read + label/
  trash), never `gmail.delete` (permanent delete) or `gmail.send`.
- **No telemetry:** the agent makes no network calls except to Gmail (read/
  trash) and, if `USE_LLM=true`, to the configured OpenAI-compatible endpoint.

## Scope

`gmail.modify` is the maximum requested OAuth scope. If you see a request for
broader scopes, treat it as a red flag and report it.
