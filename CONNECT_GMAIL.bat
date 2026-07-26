@echo off
rem ============================================================
rem  CONNECT_GMAIL.bat  -  Captain's one-click Gmail connect
rem  STEP 1: edit .env.EDITME.txt first (paste app password)
rem  STEP 2: double-click THIS file
rem  It: (a) puts .env.EDITME.txt into place as .env
rem      (b) runs a SAFE dry-run (analyzes, trashes NOTHING)
rem  Nothing is trashed until you say "go live" to Peter.
rem ============================================================
cd /d "%~dp0"

echo ============================================================
echo   Gmail Agent - safe connect (dry-run, no trashing)
echo ============================================================
echo.

if not exist ".env.EDITME.txt" (
  echo ERROR: .env.EDITME.txt is missing. Ask Peter to recreate it.
  pause
  exit /b 1
)

rem Check that the password line was actually filled in
findstr /i "PASTE_16_LETTERS_HERE" ".env.EDITME.txt" >nul
if %errorlevel%==0 (
  echo.
  echo ************************************************************
  echo   STOP: you have NOT pasted your app password yet.
  echo   Open .env.EDITME.txt in Notepad, paste the 16 letters,
  echo   save, then run this file again.
  echo ************************************************************
  echo.
  pause
  exit /b 1
)
echo [ok] app password looks filled in.

rem Put the edited file into place as the real .env
copy /Y ".env.EDITME.txt" ".env" >nul
echo [ok] wrote .env from your edits.

echo.
echo [*] Running SAFE DRY-RUN (reads your inbox, moves nothing)...
echo.
call ".venv\Scripts\python.exe" -m gmail_agent.cli run --dry-run
echo.
echo ============================================================
echo   DRY-RUN DONE. Above you should see emails it WOULD trash.
echo   If it shows real analysis (not an error), tell Peter:
echo        "gmail go live"
echo   and Peter will run the real sweep.
echo   Nothing was trashed during this run.
echo ============================================================
echo.
pause
