"""Launcher: ensures the project root is on sys.path so `gmail_agent` imports
without an editable install. Usage: uv run python run.py <cli args>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gmail_agent.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
