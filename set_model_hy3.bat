@echo off
REM ============================================================
REM  Gmail Agent - confirm HY3 is the PRIMARY model
REM  (double-click this; no typing needed)
REM  Prints the active model configuration. The model is set in .env
REM  (LLM_MODEL=tencent/hy3); gmail_agent/llm.py falls back to a free
REM  model only if HY3 is unavailable.
REM ============================================================
cd /d %~dp0
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" show_model.py
) else (
    python show_model.py
)
echo.
pause
