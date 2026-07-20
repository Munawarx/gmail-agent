@echo off
cd /d %~dp0
echo ============================================
echo  GMAIL AGENT - DRY RUN (safe preview)
echo  Looks at your mail but MOVES NOTHING.
echo ============================================
"%~dp0\.venv\Scripts\python.exe" run.py run --dry-run
echo.
echo Done. You can close this window.
pause
