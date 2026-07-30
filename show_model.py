"""Print the active Gmail Agent LLM model configuration.

The actual model is set in .env (LLM_MODEL=tencent/hy3) and the code in
gmail_agent/llm.py falls back to a free model only if HY3 is unavailable.
This is a thin, import-safe helper invoked by set_model_hy3.bat on Windows.
"""
from gmail_agent import config

s = config.get_settings()
print("USE_LLM      :", s["use_llm"])
print("PRIMARY (HY3):", s["llm_model"])
print("FALLBACK     :", s["llm_fallback_model"])
print("ENDPOINT     :", s["llm_base_url"])
print("API KEY OK   :", bool(s["llm_api_key"]), "(reused from Hermes .env)")
print()
print("HY3 is the primary model. Fallback activates ONLY if HY3 fails.")
