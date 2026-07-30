@echo off
rem ============================================================
rem  gmail_cli.bat  -  Run the Gmail Agent CLI (Windows)
rem  Uses the local .venv Python so it works without `uv` installed.
rem  Usage: gmail_cli.bat <args>   e.g.  gmail_cli.bat run --dry-run
rem ============================================================
cd /d %~dp0
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" run.py %*
) else (
    echo [gmail_cli] .venv not found - using system python3. Run setup first if needed.
    python run.py %*
)
