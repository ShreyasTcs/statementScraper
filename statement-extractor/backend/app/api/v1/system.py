"""Health and configuration (no secrets, no document data)."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import REQUIRED_GROQ_MODEL, settings
from app.core.security import current_user
from app.db.migrations import CURRENT_VERSION
from app.services.llm_extraction_service import PROMPT_VERSIONS
from app.services.ocr_service import ocr_service
from app.services.structure_service import structure_analyzer

router = APIRouter(tags=["system"])

VERSION = "3.0.0"


@router.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": VERSION,
        "schema_version": CURRENT_VERSION,
        "ocr_backend": ocr_service.backend_name,
        "structure_model": structure_analyzer.model,
        "default_engine": settings.extraction_engine,
        "default_engine_ready": not settings.engine_problems(settings.extraction_engine),
    }


@router.get("/config")
def config(_user: str = Depends(current_user)) -> dict:
    engines = []
    for name in ("local", "groq", "hybrid"):
        problems = settings.engine_problems(name)
        engines.append({
            "name": name,
            "available": not problems,
            "external": name != "local",
            "problems": problems,
        })
    return {
        "default_engine": settings.extraction_engine,
        "engines": engines,
        "external_calls_allowed": settings.allow_external_llm_calls,
        "model": REQUIRED_GROQ_MODEL,
        "prompt_version": PROMPT_VERSIONS[settings.groq_input],
        "groq_input": settings.groq_input,
        "max_file_size_bytes": settings.max_file_size_bytes,
        "max_pages": settings.max_pages,
        "allowed_extensions": list(settings.allowed_extensions),
        "auth_mode": settings.auth_mode,
        "retention_days": settings.retention_days,
    }
