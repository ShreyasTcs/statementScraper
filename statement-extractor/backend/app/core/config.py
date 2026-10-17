"""Application configuration.

Settings come from ``backend/.env``, overridden by real environment variables.
Names are case-insensitive (``EXTRACTION_ENGINE`` = ``extraction_engine``).

Defaults are safe: the local engine, no external calls, no authentication
(single local user). Anything that sends statement text off the machine needs
two explicit settings (an engine that uses Groq *and*
``ALLOW_EXTERNAL_LLM_CALLS=true``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent

#: The only model the Groq engine will use. Changing GROQ_MODEL to anything else
#: is reported as a configuration error rather than silently honoured.
REQUIRED_GROQ_MODEL = "openai/gpt-oss-20b"

Engine = Literal["local", "groq", "hybrid"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Bank Statement Extractor"
    api_prefix: str = "/api/v1"

    # ------------------------------------------------------------ storage
    data_dir: Path = PROJECT_ROOT / "data"
    upload_dir: Path = PROJECT_ROOT / "data" / "uploads"
    processed_dir: Path = PROJECT_ROOT / "data" / "processed"
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'statements.db').as_posix()}"
    #: Copy the SQLite file aside before applying schema migrations.
    db_backup_before_migration: bool = True

    # ------------------------------------------------------------ uploads
    max_file_size_bytes: int = 25 * 1024 * 1024
    max_pages: int = 200
    allowed_extensions: tuple[str, ...] = ("pdf", "jpg", "jpeg", "png")
    allowed_mime_types: tuple[str, ...] = ("application/pdf", "image/jpeg", "image/png")
    #: A PDF page needs this many characters of selectable text before its text
    #: layer is trusted and OCR is skipped.
    min_chars_for_text_layer: int = 40

    # ------------------------------------------------------------ OCR / layout
    #: auto | tesseract | rapidocr | paddleocr
    ocr_engine: str = "auto"
    ocr_dpi: int = 200
    #: geometric | layout | ppstructure | auto (see README; geometric is the default)
    structure_engine: str = "geometric"
    paddle_enable_mkldnn: bool = False
    paddle_text_det_max_side: int = 960

    # ------------------------------------------------------------ extraction engine
    #: local  - deterministic engine only (no external calls)
    #: groq   - Groq openai/gpt-oss-20b maps the document; deterministic validation
    #: hybrid - both, cross-checked; the local engine is the fallback
    extraction_engine: Engine = "local"
    allow_external_llm_calls: bool = False
    groq_api_key: SecretStr | None = None
    groq_model: str = REQUIRED_GROQ_MODEL
    groq_timeout_seconds: float = 60.0
    groq_max_retries: int = 2
    groq_reasoning_effort: Literal["low", "medium", "high"] = "low"
    #: Output budget per request (reasoning plus JSON). The model allows 65,536.
    groq_max_completion_tokens: int = 8192
    #: Estimated input tokens per request. Kept small by default because the
    #: Groq free tier allows about 8K tokens per minute for this model.
    groq_chunk_max_input_tokens: int = 3500
    #: How long to wait in total for rate-limit windows before failing a chunk.
    groq_rate_limit_max_wait_seconds: float = 90.0
    groq_seed: int = 7
    #: What the model reads:
    #: markdown - the statement as Markdown: tables from the local table detector (one cell per column, so an
    #:            empty cell stays empty), balance/totals lines outside the table, all other text verbatim, plus
    #:            the layout reader's notes. Falls back to ``lines`` if the Markdown would lose any printed text.
    #: lines    - the layout lines with their horizontal spacing (the format before 2026-10.3).
    groq_input: Literal["markdown", "lines"] = "markdown"

    # ------------------------------------------------------------ processing
    processing_workers: int = 2

    # ------------------------------------------------------------ confidence
    confidence_high: float = 0.90
    confidence_review: float = 0.70

    # ------------------------------------------------------------ security
    #: none  - single local user ("local"); suitable only on a trusted machine
    #: token - every request needs "Authorization: Bearer <token>"; documents are
    #:         visible only to the user the token belongs to
    auth_mode: Literal["none", "token"] = "none"
    #: "alice:token-one,bob:token-two" - user names and their bearer tokens.
    api_tokens: SecretStr | None = None
    #: Delete documents (files and extracted data) older than this. 0 = keep.
    retention_days: int = 0
    cors_origins: tuple[str, ...] = ("http://localhost:5173", "http://127.0.0.1:5173")

    # ------------------------------------------------------------ helpers

    def ensure_dirs(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.processed_dir.mkdir(parents=True, exist_ok=True)

    def token_map(self) -> dict[str, str]:
        """bearer token -> user name."""
        raw = self.api_tokens.get_secret_value() if self.api_tokens else ""
        mapping: dict[str, str] = {}
        for pair in raw.split(","):
            user, sep, token = pair.strip().partition(":")
            if sep and user.strip() and len(token.strip()) >= 16:
                mapping[token.strip()] = user.strip()
        return mapping

    def engine_problems(self, engine: str | None = None) -> list[dict[str, str]]:
        """Why an engine cannot run, as stable codes with actionable messages."""
        engine = engine or self.extraction_engine
        problems: list[dict[str, str]] = []
        if engine not in ("local", "groq", "hybrid"):
            problems.append({"code": "engine_unknown", "message": f"Unknown extraction engine '{engine}'. Use local, groq or hybrid."})
            return problems
        if engine == "local":
            return problems
        if not self.allow_external_llm_calls:
            problems.append({
                "code": "external_calls_disabled",
                "message": "The groq and hybrid engines send statement text to Groq. Set ALLOW_EXTERNAL_LLM_CALLS=true to permit this.",
            })
        if not (self.groq_api_key and self.groq_api_key.get_secret_value().strip()):
            problems.append({"code": "groq_key_missing", "message": "Set GROQ_API_KEY to use the groq or hybrid engine."})
        if self.groq_model != REQUIRED_GROQ_MODEL:
            problems.append({
                "code": "groq_model_unsupported",
                "message": f"GROQ_MODEL must be {REQUIRED_GROQ_MODEL}; '{self.groq_model}' is not used as a substitute.",
            })
        try:
            import groq  # noqa: F401
        except Exception:
            problems.append({"code": "groq_sdk_missing", "message": "Install the groq package (pip install -r requirements.txt)."})
        return problems

    def security_problems(self) -> list[dict[str, str]]:
        problems: list[dict[str, str]] = []
        if self.auth_mode == "token" and not self.token_map():
            problems.append({
                "code": "auth_tokens_missing",
                "message": "AUTH_MODE=token needs API_TOKENS like 'alice:<16+ char token>'.",
            })
        return problems


settings = Settings()
settings.ensure_dirs()
