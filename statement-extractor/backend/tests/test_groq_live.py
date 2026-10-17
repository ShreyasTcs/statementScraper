"""Optional live smoke test against the real Groq API.

Skipped unless ALL of these are set in the environment:

    RUN_LIVE_GROQ=1
    LIVE_GROQ_API_KEY=<key>        (a separate variable: the test configuration blanks GROQ_API_KEY)

It sends one small SYNTHETIC statement (invented bank, holder and numbers) to
openai/gpt-oss-20b and checks that the pipeline reconciles it. Never point it at
real customer documents.
"""
from __future__ import annotations

import os

import pytest
import statement_factory as sf
from pydantic import SecretStr

from app.core.config import settings
from app.services import pipeline
from app.services.llm_client import GroqClient
from app.services.pdf_service import TextReader

LIVE = os.environ.get("RUN_LIVE_GROQ") == "1" and bool(os.environ.get("LIVE_GROQ_API_KEY"))


@pytest.mark.skipif(not LIVE, reason="live Groq test: set RUN_LIVE_GROQ=1 and LIVE_GROQ_API_KEY")
def test_live_groq_extracts_a_synthetic_statement(statement, monkeypatch):
    monkeypatch.setattr(settings, "allow_external_llm_calls", True)
    monkeypatch.setattr(settings, "groq_api_key", SecretStr(os.environ["LIVE_GROQ_API_KEY"]))
    path, truth = statement(sf.Spec())
    analysis = pipeline.analyze(TextReader().read(path, document_id="live"), engine="groq", client=GroqClient(settings))
    assert analysis.engine.used == "groq"
    assert analysis.engine.llm.model == "openai/gpt-oss-20b"
    assert len(analysis.table.rows) == len(truth["rows"])
    assert analysis.completeness.status in ("complete", "needs_review")
