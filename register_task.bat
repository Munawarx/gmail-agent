@echo off
schtasks /create /xml "%~dp0\gmail_agent_task.xml" /tn "Gmail_Agent" /f
schtasks /query /tn "Gmail_Agent"
