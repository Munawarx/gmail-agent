@echo off
cd /d %~dp0
echo ============================================
echo  Gmail Agent - ONE-TIME LOGIN
echo  A browser will open. Pick your Google
echo  account and click ALLOW.
echo ============================================
"%~dp0\.venv\Scripts\python.exe" run.py auth
echo.
echo Done. You can close this window.
pause
