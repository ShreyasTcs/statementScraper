"""ORM models.

documents -< processing_runs
          -< statement_extractions -< extraction_fields      (header fields, per account)
                                   -< transactions           (table rows, per account)
                                   -< corrections            (audit trail of user edits)

Every column added after the first release is nullable or has a default, and
is added to existing databases by ``app/db/migrations.py`` - never by
recreating tables.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    original_filename: Mapped[str] = mapped_column(String(255))
    stored_filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(16))
    mime_type: Mapped[str] = mapped_column(String(64))
    file_size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    page_count: Mapped[int] = mapped_column(Integer, default=1)
    has_text_layer: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_ocr: Mapped[bool] = mapped_column(Boolean, default=False)
    preview_path: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    #: The user the document belongs to ("local" when authentication is off).
    owner: Mapped[str] = mapped_column(String(64), default="local", index=True)

    runs: Mapped[list["ProcessingRun"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    extractions: Mapped[list["StatementExtraction"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class ProcessingRun(Base):
    __tablename__ = "processing_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    #: queued | processing | completed | failed
    status: Mapped[str] = mapped_column(String(16), default="queued")
    text_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ocr_used: Mapped[bool] = mapped_column(Boolean, default=False)
    structure_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- added in schema version 2 -------------------------------------------
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=_now)
    #: What was asked for, and what actually produced the result.
    engine_requested: Mapped[str | None] = mapped_column(String(16), nullable=True)
    engine_used: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fallback_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Live progress: queued, reading, ocr, extracting, llm, validating, saving, done.
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    progress_current: Mapped[int | None] = mapped_column(Integer, nullable=True)
    progress_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_processed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pages_failed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: LLM bookkeeping (no document content is ever stored here).
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    schema_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    llm_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    llm_latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Per chunk: pages, status, error code, tokens, latency.
    chunks: Mapped[list | None] = mapped_column(JSON, nullable=True)

    document: Mapped[Document] = relationship(back_populates="runs")


class StatementExtraction(Base):
    __tablename__ = "statement_extractions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"))
    processing_run_id: Mapped[str] = mapped_column(ForeignKey("processing_runs.id"))
    overall_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Detected columns, reconciliation checks, totals, completeness, accounts.
    summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    # --- added in schema version 2 -------------------------------------------
    #: complete | partial | needs_review | failed
    completeness_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    engine: Mapped[str | None] = mapped_column(String(16), nullable=True)
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    revalidated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    document: Mapped[Document] = relationship(back_populates="extractions")
    fields: Mapped[list["ExtractionField"]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        order_by="ExtractionField.display_order",
    )
    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        order_by="Transaction.row_index",
    )
    corrections: Mapped[list["Correction"]] = relationship(
        back_populates="extraction",
        cascade="all, delete-orphan",
        order_by="Correction.created_at",
    )


class ExtractionField(Base):
    """One statement-level value: account number, period, opening balance..."""

    __tablename__ = "extraction_fields"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("statement_extractions.id"))
    field_name: Mapped[str] = mapped_column(String(64))
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    normalized_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_type: Mapped[str] = mapped_column(String(24), default="text")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_status: Mapped[str] = mapped_column(String(16), default="not_found")
    validation_status: Mapped[str] = mapped_column(String(16), default="not_found")
    validation_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    anchor_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    # --- added in schema version 2 -------------------------------------------
    account_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: explicit | inferred | derived | unknown | user
    provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: Source line ids, page, excerpt, verification result.
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    validation_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    #: What extraction produced, kept when a user corrects the value.
    original_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    original_normalized: Mapped[str | None] = mapped_column(Text, nullable=True)

    extraction: Mapped[StatementExtraction] = relationship(back_populates="fields")


class Transaction(Base):
    """One row of the statement's transaction table."""

    __tablename__ = "transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("statement_extractions.id"), index=True)
    row_index: Mapped[int] = mapped_column(Integer)
    page: Mapped[int] = mapped_column(Integer)
    bbox: Mapped[list | None] = mapped_column(JSON, nullable=True)
    txn_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    value_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    date_inherited: Mapped[bool] = mapped_column(Boolean, default=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    reference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Money is stored as decimal strings: no float rounding in a ledger.
    debit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    credit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    balance: Mapped[str | None] = mapped_column(String(32), nullable=True)
    direction_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    balance_check: Mapped[str] = mapped_column(String(16), default="unchecked")
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="needs_review")
    issues: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    signals: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- added in schema version 2 -------------------------------------------
    account_key: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: An amount whose direction could not be determined (kept, never dropped).
    amount: Mapped[str | None] = mapped_column(String(32), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    #: local | groq | user
    source_engine: Mapped[str | None] = mapped_column(String(16), nullable=True)
    provenance: Mapped[str | None] = mapped_column(String(16), nullable=True)
    evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    issue_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reason_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The values extraction produced, frozen at extraction time.
    original: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    extraction: Mapped[StatementExtraction] = relationship(back_populates="transactions")


class Correction(Base):
    """One user edit. The extracted value is never overwritten without a record here."""

    __tablename__ = "corrections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    extraction_id: Mapped[str] = mapped_column(ForeignKey("statement_extractions.id"), index=True)
    #: field | transaction
    entity_type: Mapped[str] = mapped_column(String(16))
    entity_id: Mapped[str] = mapped_column(String(36))
    #: update | create | delete | restore
    action: Mapped[str] = mapped_column(String(16), default="update")
    attribute: Mapped[str | None] = mapped_column(String(64), nullable=True)
    old_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    user: Mapped[str] = mapped_column(String(64), default="local")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    extraction: Mapped[StatementExtraction] = relationship(back_populates="corrections")
