"""Test configuration.

The environment is pinned *before* the app is imported: a throw-away database
and upload folder, the local engine, external calls disabled and no API key -
so no test can touch real data or reach Groq, whatever backend/.env says.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="stmt-tests-"))
os.environ.update({
    "DATABASE_URL": f"sqlite:///{(_TMP / 'test.db').as_posix()}",
    "DATA_DIR": str(_TMP),
    "UPLOAD_DIR": str(_TMP / "uploads"),
    "PROCESSED_DIR": str(_TMP / "processed"),
    "EXTRACTION_ENGINE": "local",
    "ALLOW_EXTERNAL_LLM_CALLS": "false",
    "GROQ_API_KEY": "",
    "AUTH_MODE": "none",
    "API_TOKENS": "",
    "RETENTION_DAYS": "0",
    "DB_BACKUP_BEFORE_MIGRATION": "false",
    "PROCESSING_WORKERS": "2",
})

BACKEND = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
for path in (str(BACKEND), str(TESTS)):
    if path not in sys.path:
        sys.path.insert(0, path)

import pytest  # noqa: E402

import statement_factory as sf  # noqa: E402

collect_ignore = ["test_pipeline.py"]  # the scorecard script, run directly (it reads local sample folders)


@pytest.fixture(scope="session")
def tmp_root() -> Path:
    return _TMP


@pytest.fixture()
def statement(tmp_path):
    """Build a synthetic statement: ``path, truth = statement(Spec(...))``."""
    counter = {"n": 0}

    def make(spec: sf.Spec | None = None, txns=None, name: str | None = None):
        counter["n"] += 1
        path = tmp_path / f"{name or 'statement'}-{counter['n']}.pdf"
        truth = sf.build(path, spec, txns)
        return path, truth

    return make


@pytest.fixture()
def app_client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def groq_enabled(monkeypatch):
    """Pretend Groq is configured, with a scripted client instead of the network."""
    from pydantic import SecretStr

    from app.core.config import settings
    from app.services import jobs

    monkeypatch.setattr(settings, "allow_external_llm_calls", True)
    monkeypatch.setattr(settings, "groq_api_key", SecretStr("test-key-not-real"))
    holder: dict = {"factory": None}

    def install(factory):
        holder["factory"] = factory
        monkeypatch.setattr(jobs, "llm_client_factory", factory)

    yield install


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    shutil.rmtree(_TMP, ignore_errors=True)
