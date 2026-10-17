"""Side-by-side PoC: Pipeline A (direct text/OCR lines + local LLM), Pipeline B (Markdown + the same local LLM)
and the existing deterministic engine as a reference. See README.md in this folder.

Importing this package pins the application's settings for the experiment process *before* any ``app`` module
is imported: external LLM calls off, an empty Groq key, the local engine as the default, and a database URL that
points into a folder that does not exist, so the live statement database can never be opened by this process.
Environment variables take precedence over ``backend/.env`` in the application's settings.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_DIR = BACKEND_DIR.parent                     # statement-extractor/
DATA_DIR = PROJECT_DIR / "data"
EVAL_DIR = DATA_DIR / "evaluations"
EVAL_DB = DATA_DIR / "evaluation.db"
LIVE_DB_FILES = [DATA_DIR / name for name in ("statements.db", "statements.db-wal", "statements.db-shm")]
#: The application's database setting points here. The parent folder is never created, so any attempt to open it fails.
APP_DB_SENTINEL = EVAL_DIR / "_app_database_disabled" / "must-not-exist.db"

if any(name == "app" or name.startswith("app.") for name in sys.modules):
    raise RuntimeError("experiments.markdown_ab must be imported before any application module")

PINNED_ENV = {
    "ALLOW_EXTERNAL_LLM_CALLS": "false",
    "GROQ_API_KEY": "",
    "EXTRACTION_ENGINE": "local",
    "DATABASE_URL": f"sqlite:///{APP_DB_SENTINEL.as_posix()}",
    # Pipeline A is the application's Groq path on the layout lines; the application now reads Markdown by default.
    "GROQ_INPUT": "lines",
}
os.environ.update(PINNED_ENV)
