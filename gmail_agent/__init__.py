"""Intelligent Gmail Management Agent — package root.

Architecture (see docs/ARCHITECTURE.md for the full 10-part spec):
  config      -> .env loader (credentials never printed)
  schemas     -> dataclasses for Email, Classification, LogEntry
  store       -> SQLite: checkpoint + processing logs (single source of truth)
  gmail       -> Gmail API OAuth client (read + trash + checkpoint)
  classifier  -> rule-based + optional LLM classification
  agent       -> the processing loop (resume from checkpoint, classify, act)
  cli         -> command entry point

The agent is designed to run on a schedule (cron / Task Scheduler). It resumes
from the last checkpoint so it never re-scans the whole inbox.
"""
from __future__ import annotations

__version__ = "1.0.0"
