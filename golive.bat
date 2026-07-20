@echo off
cd /d %~dp0
echo ============================================
echo  GMAIL AGENT - GO LIVE
echo  This MOVES trash mail to your Trash folder.
echo ============================================
"%~dp0\.venv\Scripts\python.exe" run.py run
echo.
echo Done. You can close this window.
pause
