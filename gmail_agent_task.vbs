' Gmail Agent - daily scheduled sweep launcher
Option Explicit
Dim sh, env
Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
env.Item("PYTHONIOENCODING") = "utf-8"
sh.CurrentDirectory = "%~dp0"
' Runs one pass; resumes from checkpoint. Trashes real mail (not dry-run).
sh.Run "cmd.exe /c ""%~dp0\.venv\Scripts\python.exe run.py run > %~dp0\logs\sweep.log 2>&1""", 0, False
