@echo off
REM ============================================================
REM  Gmail Agent - confirm HY3 is the PRIMARY model
REM  (double-click this; no typing needed)
REM  This just prints the active model configuration. The actual
REM  model is set in .env (LLM_MODEL=tencent/hy3) + the code in
REM  gmail_agent/llm.py, which falls back to a free model only if
REM  HY3 is unavailable.
REM ============================================================
cd /d %~dp0
".\venv\Scripts\python.exe" - <<'PY'
from gmail_agent import config
s = config.get_settings()
print("USE_LLM      :", s["use_llm"])
print("PRIMARY (HY3):", s["llm_model"])
print("FALLBACK     :", s["llm_fallback_model"])
print("ENDPOINT     :", s["llm_base_url"])
print("API KEY OK   :", bool(s["llm_api_key"]), "(reused from Hermes .env)")
print()
print("HY3 is the primary model. Fallback activates ONLY if HY3 fails.")
PY
echo.
pause
