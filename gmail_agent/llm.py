"""LLM escalation layer for the Gmail agent.

Design goals (per project brief):
  * HY3 (tencent/hy3) is the PRIMARY model, used through OpenRouter.
  * If HY3 is unavailable, fall back automatically to another model
    (a free OpenRouter model by default) -- never fail silently, never
    trash mail unless the model explicitly says so.
  * The deterministic rule engine in classifier.py ALWAYS runs first.
    The LLM is only consulted for borderline emails the rules left as
    KEEP, and it can only ever promote KEEP -> {PROMO, SOCIAL, OTP_EXPIRED}.
    It can NEVER downgrade an IMPORTANT/security email.
  * No external dependencies: uses only the Python standard library
    (urllib) so it works inside the project's existing .venv.

Model chain (overridable via .env):
  PRIMARY : LLM_MODEL            (default tencent/hy3  -> HY3)
  FALLBACK: LLM_FALLBACK_MODEL   (default google/gemma-4-26b-a4b-it:free)
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Optional

from .schemas import Category, Classification, Email

# ── Defaults (HY3 is the primary; a free model is the fallback) ──────────
DEFAULT_PRIMARY = "tencent/hy3"
DEFAULT_FALLBACK = "google/gemma-4-26b-a4b-it:free"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_TIMEOUT = 45

# Categories the LLM is allowed to assign when escalating a borderline KEEP.
_ALLOWED = {Category.PROMO, Category.SOCIAL, Category.OTP_EXPIRED}


def _extract_json(text: str) -> Optional[dict]:
    """Robustly pull a JSON object out of an LLM response that may be wrapped
    in markdown code fences or contain prose around the JSON."""
    if text is None:
        return None
    t = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences.
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.lower().startswith("json"):
            t = t[4:]
    # Find the first {...} span.
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except ValueError:
            return None
    try:
        return json.loads(t)
    except ValueError:
        return None


# Known location of the Hermes gateway .env (holds OPENROUTER_API_KEY).
_HERMES_ENV = os.path.join(
    os.environ.get("LOCALAPPDATA", ""), "hermes", ".env"
)


class LLMClassifier:
    """Calls OpenAI-compatible chat endpoints with a HY3-first fallback chain."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        primary: str = DEFAULT_PRIMARY,
        fallback: str = DEFAULT_FALLBACK,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.primary = primary
        self.fallback = fallback
        self.timeout = timeout

    def _chat(self, model: str, email: Email) -> Optional[Classification]:
        """Call one model. Returns a Classification or None on any failure."""
        system = (
            "You are an email triage classifier for a Gmail cleanup agent. "
            "Classify the email into exactly one of these categories:\n"
            "  - keep      : important, personal, financial, security, or "
            "anything that must NOT be trashed\n"
            "  - promo     : marketing / promotional / newsletter / sale\n"
            "  - social    : social network notifications (LinkedIn, Facebook, "
            "Instagram, X, etc.)\n"
            "  - otp_expired : a one-time code that is already expired\n"
            "Trash is RECOVERABLE, but only trash clearly low-value mail. "
            "When unsure, reply 'keep'. Never trash security, billing, "
            "banking, legal, or account-recovery mail."
        )
        user = (
            f"Subject: {email.subject}\n"
            f"From: {email.sender}\n"
            f"Snippet: {email.snippet}\n"
            f"Body:\n{(email.body_text or '')[:1500]}\n\n"
            "Reply with ONLY a JSON object, no markdown, for example:\n"
            '{"category":"promo","reason":"marketing sender","confidence":0.9}'
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": 600,
            "temperature": 0.0,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://localhost/gmail_agent",
                "X-Title": "Gmail Agent",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        # Some providers (e.g. HY3 on certain routes) return content=None with
        # tool_calls, or an empty string. Be tolerant and never crash here; the
        # caller treats a None result as "try next in the chain".
        text = msg.get("content")
        if not text:
            tc = msg.get("tool_calls") or []
            if tc and tc[0].get("function", {}).get("arguments"):
                text = tc[0]["function"]["arguments"]
        if not text:
            return None
        obj = _extract_json(text)
        if obj is None:
            return None
        cat = str(obj.get("category", "")).strip().lower()
        mapping = {
            "keep": Category.KEEP,
            "promo": Category.PROMO,
            "social": Category.SOCIAL,
            "otp_expired": Category.OTP_EXPIRED,
        }
        category = mapping.get(cat)
        if category is None or category not in _ALLOWED:
            # LLM said keep, or something we don't allow it to assign.
            return None
        try:
            conf = float(obj.get("confidence", 0.8))
        except (TypeError, ValueError):
            conf = 0.8
        reason = str(obj.get("reason", "LLM escalation"))[:200]
        return Classification(category, f"LLM({model}): {reason}", conf)

    def classify(self, email: Email) -> Optional[Classification]:
        """Try PRIMARY (HY3) then FALLBACK. Returns Classification or None."""
        if not self.api_key:
            return None
        last_err: Optional[Exception] = None
        for model in (self.primary, self.fallback):
            if not model:
                continue
            try:
                result = self._chat(model, email)
                if result is not None:
                    return result
            except (urllib.error.URLError, urllib.error.HTTPError,
                    TimeoutError, OSError, ValueError, KeyError) as exc:
                last_err = exc
                continue
        if last_err is not None:
            # Surface the failure so the caller can log it; rules still win.
            print(f"[llm] escalation failed: {last_err!r}")
        return None


# ── Module-level cached client (built lazily from config) ────────────────
_client: Optional[LLMClassifier] = None


def get_llm_classifier(settings: dict) -> Optional[LLMClassifier]:
    global _client
    if _client is not None:
        return _client
    if not settings.get("use_llm"):
        return None
    api_key = settings.get("llm_api_key") or settings.get("openrouter_api_key")
    if not api_key:
        print("[llm] USE_LLM=true but no API key found; skipping LLM tier.")
        return None
    _client = LLMClassifier(
        api_key=api_key,
        base_url=settings.get("llm_base_url") or DEFAULT_BASE_URL,
        primary=settings.get("llm_model") or DEFAULT_PRIMARY,
        fallback=settings.get("llm_fallback_model") or DEFAULT_FALLBACK,
        timeout=int(settings.get("llm_timeout") or DEFAULT_TIMEOUT),
    )
    return _client


def reset_llm_classifier() -> None:
    """Clear the cached client (used by tests)."""
    global _client
    _client = None
