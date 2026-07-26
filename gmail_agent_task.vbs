' Gmail Agent - daily scheduled sweep launcher
Option Explicit
Dim sh, env
Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
env.Item("PYTHONIOENCODING") = "utf-8"
sh.CurrentDirectory = "C:\PATH\TO\gmail_agent"
' Runs one pass; resumes from checkpoint. Trashes real mail (not dry-run).
sh.Run "cmd.exe /c ""C:\PATH\TO\gmail_agent\.venv\Scripts\python.exe run.py run > C:\PATH\TO\gmail_agent\logs\sweep.log 2>&1""", 0, False
