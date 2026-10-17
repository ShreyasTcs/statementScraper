"""API request and response models.

Every user-facing message travels as a ``MessageOut``: a stable ``code`` the
frontend translates, the ``params`` it interpolates (money as exact decimal
strings, dates as ISO), and English ``text`` as the fallback. Values copied from
the statement (descriptions, names, references) are returned exactly as
extracted and are never translated.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal["high", "review", "needs_review", "not_found"]
Completeness = Literal["complete", "partial", "needs_review", "failed"]


class MessageOut(BaseModel):
    code: str
    params: dict[str, Any] = Field(default_factory=dict)
    text: str = ""


class SourceRef(BaseModel):
    """Where a value sits in the original PDF."""

    page: int
    bbox: list[float] = Field(description="x0, y0, x1, y1 in PDF points")
    text: str | None = None
    anchor: str | None = None


class EvidenceOut(BaseModel):
    engine: str | None = None
    page: int | None = None
    bbox: list[float] | None = None
    lines: list[str] | None = None
    excerpt: str | None = None
    text_source: str | None = None
    #: Per attribute: was the value found again in the cited text (language-model rows)?
    verified: dict[str, bool] | bool | None = None
    confirmed_by: str | None = None


class FieldOut(BaseModel):
    id: str | None = None
    account: str = "A1"
    name: str
    label: str
    value: str | None
    normalized: str | None = None
    value_type: str
    required: bool
    found: bool
    confidence: float | None = None
    status: Status
    validation: Literal["valid", "invalid", "not_found", "review"]
    validation_message: str | None = None
    validation_code: str | None = None
    reasons: list[str] = Field(default_factory=list)
    reason_codes: list[MessageOut] = Field(default_factory=list)
    source: SourceRef | None = None
    alternatives: list[str] = Field(default_factory=list)
    provenance: str | None = None
    evidence: EvidenceOut | None = None
    corrected: bool = False
    original_value: str | None = None


class ColumnOut(BaseModel):
    key: str
    label: str
    required: bool
    found: bool
    printed_as: str | None = None


class TransactionOut(BaseModel):
    id: str | None = None
    account: str = "A1"
    index: int
    page: int
    date: str | None
    value_date: str | None = None
    date_inherited: bool = False
    description: str | None
    reference: str | None
    debit: str | None
    credit: str | None
    #: An amount whose direction is unknown (never dropped, never guessed).
    amount: str | None = None
    balance: str | None
    currency: str | None = None
    direction_inferred: bool = False
    balance_check: Literal["ok", "mismatch", "unchecked"]
    confidence: float | None
    status: Literal["high", "review", "needs_review"]
    issues: list[str] = Field(default_factory=list)
    issue_codes: list[MessageOut] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    reason_codes: list[MessageOut] = Field(default_factory=list)
    bbox: list[float] | None = None
    source_engine: str | None = None
    provenance: str | None = None
    evidence: EvidenceOut | None = None
    corrected: bool = False
    deleted: bool = False
    original: dict[str, Any] | None = None


class CheckOut(BaseModel):
    key: str
    label: str
    status: Literal["pass", "fail", "warn", "skipped", "info"]
    detail: str
    detail_code: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class BalanceRowOut(BaseModel):
    kind: str
    page: int
    text: str
    amount: str | None = None
    date: str | None = None


class UnresolvedOut(BaseModel):
    index: int
    page: int
    text: str
    reason: str
    amounts: list[str] = Field(default_factory=list)
    bbox: list[float] | None = None
    dismissed: bool = False


class ConventionsOut(BaseModel):
    decimal: str = "."
    group: str = ","
    confident: bool = False
    date_order: str = "dmy"


class TotalsOut(BaseModel):
    opening_balance: str | None = None
    opening_source: str | None = None
    opening_date: str | None = None
    closing_balance: str | None = None
    closing_source: str | None = None
    closing_date: str | None = None
    currency: str | None = None
    #: running_balance | statement_totals | none
    mode: str = "none"
    #: oldest_first | newest_first
    order: str = "oldest_first"
    total_debits: str = "0.00"
    total_credits: str = "0.00"
    printed_debits: str | None = None
    printed_credits: str | None = None
    net_movement: str = "0.00"
    unresolved_rows: int = 0


class AccountOut(BaseModel):
    key: str
    label: str | None = None
    engine: str | None = None
    currency: str | None = None
    header: list[FieldOut]
    columns: list[ColumnOut]
    transactions: list[TransactionOut]
    checks: list[CheckOut]
    totals: TotalsOut
    balance_rows: list[BalanceRowOut] = Field(default_factory=list)
    unresolved: list[UnresolvedOut] = Field(default_factory=list)
    duplicates: list[list[int]] = Field(default_factory=list)
    has_balance_column: bool = True
    empty_table_text: str | None = None
    notes: list[str] = Field(default_factory=list)
    note_codes: list[MessageOut] = Field(default_factory=list)


class CompletenessOut(BaseModel):
    status: Completeness
    reasons: list[MessageOut] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)


class ChunkOut(BaseModel):
    index: str
    pages: list[int]
    lines: int = 0
    status: str
    error_code: str | None = None
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    latency_ms: int = 0
    context_repeats: int = 0


class RunOut(BaseModel):
    id: str
    status: Literal["queued", "processing", "completed", "failed"]
    stage: str | None = None
    progress_current: int | None = None
    progress_total: int | None = None
    engine_requested: str | None = None
    engine_used: str | None = None
    fallback_reason: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    schema_version: str | None = None
    llm_calls: int | None = None
    llm_input_tokens: int | None = None
    llm_output_tokens: int | None = None
    llm_latency_ms: int | None = None
    pages_processed: int | None = None
    pages_failed: int | None = None
    chunks: list[ChunkOut] = Field(default_factory=list)
    error: MessageOut | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class DocumentOut(BaseModel):
    id: str
    filename: str
    file_type: str
    page_count: int
    file_size_bytes: int = 0
    created_at: str | None = None
    ocr_used: bool
    preview_url: str


class EngineOut(BaseModel):
    requested: str | None = None
    used: str | None = None
    fallback_reason: str | None = None
    selected: dict[str, str] = Field(default_factory=dict)
    disagreements: list[dict[str, Any]] = Field(default_factory=list)
    disagreement_count: int = 0
    model: str | None = None


class SummaryOut(BaseModel):
    headline: MessageOut
    detail: MessageOut
    tone: Literal["ready", "review", "issue"]
    accounts: int = 1
    header_total: int
    header_found: int
    header_missing: list[str]
    rows: int
    rows_high: int
    rows_review: int
    rows_needs_review: int
    rows_reconciled: int
    rows_mismatched: int
    rows_corrected: int = 0
    rows_deleted: int = 0
    overall_confidence: float | None = None


class ResultOut(BaseModel):
    document: DocumentOut
    run: RunOut | None = None
    summary: SummaryOut
    completeness: CompletenessOut
    engine: EngineOut
    accounts: list[AccountOut]
    conventions: ConventionsOut = Field(default_factory=ConventionsOut)
    notice: MessageOut | None = None
    corrected: bool = False
    corrections: int = 0
    revalidated_at: str | None = None


class UploadOut(BaseModel):
    document_id: str
    run_id: str
    status: str
    engine: str
    page_count: int
    duplicate_of_running: bool = False


class DocumentListItem(BaseModel):
    id: str
    filename: str
    created_at: str | None
    page_count: int
    file_size_bytes: int
    status: str | None
    stage: str | None = None
    engine_requested: str | None = None
    engine_used: str | None = None
    completeness: str | None = None
    rows: int = 0
    overall_confidence: float | None = None
    corrected: bool = False
    error_code: str | None = None


class DocumentList(BaseModel):
    items: list[DocumentListItem]
    total: int
    limit: int
    offset: int


class StatusOut(BaseModel):
    document_id: str
    run: RunOut


class CorrectionOut(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    action: str
    attribute: str | None
    old_value: str | None
    new_value: str | None
    user: str
    created_at: str | None


# ------------------------------------------------------------------ requests


class FieldUpdate(BaseModel):
    """Set (or clear, with null) a statement detail, or confirm the extracted value."""

    value: str | None = None
    confirm: bool = False


class TransactionUpdate(BaseModel):
    """Only the attributes present in the request body are changed; null clears one."""

    date: str | None = None
    value_date: str | None = None
    description: str | None = None
    reference: str | None = None
    debit: str | None = None
    credit: str | None = None
    amount: str | None = None
    balance: str | None = None
    currency: str | None = None
    #: The reviewer checked the row against the document.
    confirmed: bool | None = None
    #: The reviewer says this row is genuine, not a duplicate.
    not_duplicate: bool | None = None


class TransactionCreate(BaseModel):
    account: str = "A1"
    #: Insert after this row index (0 or null = at the top).
    after_index: int | None = None
    page: int | None = None
    date: str | None = None
    value_date: str | None = None
    description: str | None = None
    reference: str | None = None
    debit: str | None = None
    credit: str | None = None
    amount: str | None = None
    balance: str | None = None
    currency: str | None = None


class ReprocessRequest(BaseModel):
    engine: Literal["local", "groq", "hybrid"] | None = None
