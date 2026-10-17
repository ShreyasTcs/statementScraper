"""End-to-end processing pipeline.

    PDF -> text (+ OCR only where a page has none) -> structure
        -> engine (local | groq | hybrid) -> per-account validation
        -> confidence -> completeness -> DB

`analyze` is the pure half (no database), which is what the tests exercise.

Reliability rules:

* a failure in one stage must not lose the others: if the table cannot be found
  the header fields are still returned, and vice versa;
* one unreadable page does not fail the document - it is recorded and the
  result is marked ``partial``;
* the engine that produced the result is always recorded. The ``groq`` engine
  never falls back silently: if Groq fails the run fails with the error code.
  The ``hybrid`` engine may use the local result when Groq fails, and says so
  (``engine_used=local`` plus ``fallback_reason``).
"""
from __future__ import annotations

import copy
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.core.logging import get_logger
from app.core.messages import Message, message
from app.models import Document, ExtractionField, ProcessingRun, StatementExtraction, Transaction
from app.rules.numbers import NumberConvention, format_decimal
from app.rules.statement import HEADER_FIELDS
from app.services import completeness_service
from app.services.completeness_service import AccountEvidence, ChunkReport, Completeness, PageReport
from app.services.confidence_service import FieldConfidence, confidence_service
from app.services.document_model import DocumentContent, StructuredDocument
from app.services.extraction_service import Candidate, FieldExtraction, header_extractor
from app.services.llm_client import LLMClient, LLMError
from app.services.llm_extraction_service import LLMExtractor, LLMRun, ResultMapper
from app.services.ocr_service import OCRUnavailableError, ocr_service
from app.services.markdown_view import build_markdown
from app.services.page_text import document_lines
from app.services.pdf_service import TextReader
from app.services.structure_service import structure_analyzer
from app.services.table_service import (
    BalanceRow,
    Conventions,
    DetectedColumn,
    TableResult,
    TableRow,
    TotalsRow,
    UnresolvedLine,
    infer_conventions,
    table_extractor,
)
from app.services.validation_service import (
    FAIL,
    INFO,
    NOT_FOUND,
    PASS,
    WARN,
    Check,
    FieldValidation,
    StatementValidation,
    validation_service,
)

logger = get_logger(__name__)

text_reader = TextReader()

Progress = Callable[[str, int | None, int | None], None]

#: Issue codes produced by validation; recomputed on every (re)validation.
VALIDATION_CODES = frozenset({
    "debit_credit_swapped", "balance_delta_mismatch", "direction_too_many", "direction_not_inferable",
    "direction_ambiguous", "direction_no_solution", "duplicate_candidate", "amount_balance_mismatch",
})


class ProcessingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _noop(stage: str, current: int | None = None, total: int | None = None) -> None:
    return None


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def read_document(document: Document, path: Path, progress: Progress = _noop) -> tuple[DocumentContent, PageReport]:
    """Acquire text + coordinates, using OCR only where it is actually needed."""
    if document.file_type == "pdf":
        content, report = _read_pdf(document, path, progress)
    else:
        content, report = _read_image(document, path)
    content.source_path = str(path)
    return content, report


def _read_pdf(document: Document, path: Path, progress: Progress) -> tuple[DocumentContent, PageReport]:
    progress("reading", 0, document.page_count)
    content = (
        text_reader.read(path, document_id=document.id)
        if document.has_text_layer
        else DocumentContent(document_id=document.id, pages=[], source="ocr")
    )
    report = PageReport(detected=document.page_count)
    pages_needing_ocr = [n for n in range(1, document.page_count + 1) if not _page_has_blocks(content, n)]
    by_number = {p.page_number: p for p in content.pages}
    for done, page_number in enumerate(pages_needing_ocr):
        progress("ocr", done, len(pages_needing_ocr))
        try:
            image_bytes = text_reader.render_page(path, page_number, dpi=settings.ocr_dpi)
            target_size = text_reader.page_size(path, page_number)
            by_number[page_number] = ocr_service.extract(image_bytes, page_number=page_number, target_size=target_size)
        except OCRUnavailableError:
            report.failed.append({"page": page_number, "code": "ocr_unavailable"})
            by_number.pop(page_number, None)
        except Exception:  # one bad page must not lose the rest
            logger.exception("ocr failed for page %s of document %s", page_number, document.id)
            report.failed.append({"page": page_number, "code": "ocr_failed"})
            by_number.pop(page_number, None)
    if pages_needing_ocr:
        content.ocr_used = True
        content.source = "ocr" if not document.has_text_layer else "pdf_text"
    content.pages = [by_number[n] for n in sorted(by_number)]
    failed = {f["page"] for f in report.failed}
    report.processed = [n for n in range(1, document.page_count + 1) if n not in failed]
    return content, report


def _read_image(document: Document, path: Path) -> tuple[DocumentContent, PageReport]:
    report = PageReport(detected=1)
    try:
        page = ocr_service.extract(path, page_number=1)
    except OCRUnavailableError:
        report.failed.append({"page": 1, "code": "ocr_unavailable"})
        return DocumentContent(document_id=document.id, pages=[], source="ocr", ocr_used=True), report
    report.processed = [1]
    return DocumentContent(document_id=document.id, pages=[page], source="ocr", ocr_used=True), report


def _page_has_blocks(content: DocumentContent, page_number: int) -> bool:
    return any(p.page_number == page_number and p.blocks for p in content.pages)


# --------------------------------------------------------------------------
# Analysis (pure)
# --------------------------------------------------------------------------


@dataclass
class AccountAnalysis:
    key: str
    headers: dict[str, FieldExtraction]
    table: TableResult
    validation: StatementValidation
    header_confidence: dict[str, FieldConfidence] = field(default_factory=dict)
    unverified_values: int = 0
    engine: str = "local"


@dataclass
class EngineReport:
    requested: str = "local"
    used: str | None = None
    fallback_reason: str | None = None
    #: account key -> engine whose rows were kept (hybrid)
    selected: dict[str, str] = field(default_factory=dict)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    llm: LLMRun | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "requested": self.requested, "used": self.used, "fallback_reason": self.fallback_reason,
            "selected": self.selected, "disagreements": self.disagreements[:50],
            "disagreement_count": len(self.disagreements),
        }
        if self.llm is not None:
            out.update({"model": self.llm.model, "prompt_version": self.llm.prompt_version,
                        "schema_version": self.llm.schema_version})
        return out


@dataclass
class Analysis:
    structured: StructuredDocument
    accounts: list[AccountAnalysis]
    engine: EngineReport = field(default_factory=EngineReport)
    pages: PageReport = field(default_factory=PageReport)
    chunks: ChunkReport = field(default_factory=ChunkReport)
    completeness: Completeness = field(default_factory=lambda: Completeness("needs_review"))
    overall: float | None = None

    # The first account, for callers that think in single-account statements.
    @property
    def primary(self) -> AccountAnalysis:
        return self.accounts[0]

    @property
    def headers(self) -> dict[str, FieldExtraction]:
        return self.primary.headers

    @property
    def table(self) -> TableResult:
        return self.primary.table

    @property
    def validation(self) -> StatementValidation:
        return self.primary.validation

    @property
    def header_confidence(self) -> dict[str, FieldConfidence]:
        return self.primary.header_confidence


def _finish_account(key: str, headers: dict[str, FieldExtraction], table: TableResult,
                    unverified: int = 0, engine: str = "local") -> AccountAnalysis:
    for row in table.rows:
        row.account_key = key
    validation = validation_service.validate(headers, table)
    header_confidence = {
        name: confidence_service.score_header(extraction, validation.fields[name])
        for name, extraction in headers.items()
    }
    for row in table.rows:
        confidence_service.score_row(row, table.has_balance_column)
    return AccountAnalysis(key, headers, table, validation, header_confidence, unverified, engine)


def _local_raw(structured: StructuredDocument, conv: Conventions) -> tuple[dict[str, FieldExtraction], TableResult]:
    table = table_extractor.extract(structured, conv)
    headers = header_extractor.extract(structured, table.claimed_block_ids, table.conventions, table)
    text_source = "ocr" if conv.ocr else "pdf_text"
    for row in table.rows:
        row.source_engine = "local"
        row.provenance = "explicit"
        row.evidence = {
            "engine": "local", "page": row.page, "bbox": list(row.bbox), "text_source": text_source,
            "excerpt": " | ".join(v for v in row.raw.values() if v)[:600],
        }
        currency = headers["currency"].best
        row.currency = currency.normalized if currency else None
    return headers, table


def _suspect_multiple_sections(table: TableResult) -> bool:
    """Several distinct opening balances printed as rows: more than one ledger in one file."""
    openings = {b.amount for b in table.balance_rows if b.kind == "opening"}
    return len(openings) >= 2


def analyze(
    content: DocumentContent,
    engine: str = "local",
    client: LLMClient | None = None,
    progress: Progress = _noop,
    pages: PageReport | None = None,
) -> Analysis:
    """Run one engine (or both) over already-read content. Raises ProcessingError on engine failure."""
    pages = pages or PageReport(detected=len(content.pages), processed=[p.page_number for p in content.pages])
    progress("analyzing", None, None)
    structured = structure_analyzer.analyze(content)
    conv = infer_conventions(structured, ocr=content.ocr_used)
    report = EngineReport(requested=engine)
    chunks = ChunkReport()
    multiple = False

    local_acc: AccountAnalysis | None = None
    local_copy: TableResult | None = None
    if engine in ("local", "hybrid"):
        headers, table = _local_raw(structured, conv)
        multiple = _suspect_multiple_sections(table)
        if engine == "hybrid":
            local_copy = copy.deepcopy(table)
        local_acc = _finish_account("A1", headers, table)

    groq_accs: list[AccountAnalysis] = []
    groq_error: ProcessingError | None = None
    if engine in ("groq", "hybrid"):
        try:
            groq_accs, run, chunks = _run_groq(content, structured, conv, client, progress, local_copy)
            report.llm = run
        except ProcessingError as exc:
            groq_error = exc
            report.llm = getattr(exc, "run", None)
            if report.llm is not None:
                chunks = ChunkReport(total=len(report.llm.outcomes), failed=len(report.llm.failed))

    progress("validating", None, None)
    if engine == "local":
        assert local_acc is not None
        accounts = [local_acc]
        report.used = "local"
    elif engine == "groq":
        if groq_error is not None:
            raise groq_error
        accounts = groq_accs
        report.used = "groq"
    else:
        assert local_acc is not None
        if groq_error is not None:
            accounts = [local_acc]
            report.used, report.fallback_reason = "local", groq_error.code
            chunks = ChunkReport()  # nothing of Groq's is in the result
            local_acc.validation.checks.append(Check.of("engine", "Extraction engine", WARN, message(
                "engine_fallback", "The language model failed; this result comes from the local engine only.",
                reason=groq_error.code)))
        else:
            accounts = _choose(local_acc, groq_accs, report)
            if not any(report.selected.get(a.key) == "groq" for a in accounts):
                chunks = ChunkReport(total=chunks.total, failed=0)  # Groq's failures did not affect the kept rows
            report.used = "hybrid"

    completeness = completeness_service.assess(
        [AccountEvidence(a.key, a.headers, a.table, a.validation, a.unverified_values) for a in accounts],
        pages, chunks,
        multiple_accounts_suspected=multiple and report.used == "local" and len(accounts) == 1,
        engine_disagreements=_open_disagreements(accounts, report),
    )
    header_scores = [c for a in accounts for c in a.header_confidence.values()]
    overall = confidence_service.overall(header_scores, [r for a in accounts for r in a.table.rows])
    return Analysis(structured, accounts, report, pages, chunks, completeness, overall)


def _run_groq(content: DocumentContent, structured: StructuredDocument, conv: Conventions,
              client: LLMClient | None, progress: Progress,
              local_copy: TableResult | None) -> tuple[list[AccountAnalysis], LLMRun, ChunkReport]:
    if client is None:
        from app.services.llm_client import GroqClient

        try:
            client = GroqClient(settings)
        except LLMError as exc:
            raise ProcessingError(exc.code, exc.message) from None
    lines = document_lines(content)
    if not lines:
        raise ProcessingError("no_text", "No readable text was found in this document.")
    input_format, printed, notes = "lines", None, None
    if settings.groq_input == "markdown":
        try:
            view = build_markdown(content, structured, conv, table=local_copy)
        except Exception:  # never let the richer input stop the run: the layout lines still work
            logger.exception("markdown input could not be built; the model reads the layout lines")
            view = None
        if view is not None and view.faithful:
            lines, printed, notes, input_format = view.lines, view.printed_lines, view.notes.for_lines, "markdown"
        elif view is not None:
            logger.warning("markdown input not used: %s printed token(s) would be lost", view.stats["tokens_lost"])
    extractor = LLMExtractor(client)
    run, registry = extractor.run(lines, len(content.pages), lambda done, total: progress("llm", done, total),
                                  input_format=input_format, notes=notes)
    chunks = ChunkReport(total=len(run.outcomes), failed=len(run.failed))
    if run.fatal_error is not None or not any(o.status == "ok" for o in run.outcomes):
        code = run.error_code or "llm_failed"
        error = ProcessingError(code, _llm_failure_text(code))
        error.run = run  # type: ignore[attr-defined]
        raise error

    mapped = ResultMapper(lines, conv, printed).map(run, registry)
    failed_pages = {p for o in run.failed for p in o.pages}
    if local_copy is not None and failed_pages and len(mapped) == 1:
        # Hybrid: fill pages whose chunk failed with the local engine's rows, marked as such.
        fill = [r for r in local_copy.rows if r.page in failed_pages]
        if fill:
            table = mapped[0].table
            table.rows = sorted(table.rows + fill, key=lambda r: (r.page, r.bbox[1]))
            for index, row in enumerate(table.rows, start=1):
                row.index = index
            chunks.filled_by_local = len({o.index for o in run.failed})
    accounts = [_finish_account(m.key, m.headers, m.table, m.unverified_values, "groq") for m in mapped]
    return accounts, run, chunks


_LLM_FAILURES = {
    "external_calls_disabled": "External processing is disabled (ALLOW_EXTERNAL_LLM_CALLS=false).",
    "groq_key_missing": "GROQ_API_KEY is not set.",
    "groq_model_unsupported": "GROQ_MODEL must be openai/gpt-oss-20b.",
    "groq_sdk_missing": "The groq package is not installed.",
    "llm_auth": "Groq rejected the API key.",
    "llm_permission": "The Groq API key may not use this model.",
    "llm_model_unavailable": "Groq does not offer the configured model to this key.",
    "llm_model_mismatch": "Groq answered with a different model; the answer was discarded.",
    "llm_rate_limited": "Groq's rate limit was reached and did not clear in time.",
    "llm_timeout": "Groq did not answer in time.",
    "llm_connection": "Could not connect to Groq.",
    "llm_invalid_output": "The model's answer did not match the required format.",
    "llm_truncated": "The model's answer was cut off.",
}


def _llm_failure_text(code: str) -> str:
    return _LLM_FAILURES.get(code, "The language-model extraction failed.")


def _strength(acc: AccountAnalysis) -> tuple:
    checks = {c.key: c for c in acc.validation.checks}
    rows = acc.table.rows
    proven = completeness_service._proven(rows, checks, acc.validation.totals.mode)
    failed = sum(1 for c in acc.validation.checks if c.status == FAIL)
    mismatched = sum(1 for r in rows if r.balance_check == "mismatch")
    doubts = acc.unverified_values + len(acc.table.unresolved) + acc.validation.totals.unresolved_rows
    return (proven, -failed, -mismatched, -doubts, len(rows))


def _row_key(row: TableRow) -> tuple:
    if row.debit is not None:
        return (row.date, "dr", row.debit)
    if row.credit is not None:
        return (row.date, "cr", row.credit)
    return (row.date, "?", row.amount)


def _compare(local: AccountAnalysis, other: list[AccountAnalysis]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    mine = Counter(_row_key(r) for r in local.table.rows)
    theirs = Counter(_row_key(r) for a in other for r in a.table.rows)
    for engine, diff in (("local", mine - theirs), ("groq", theirs - mine)):
        for (day, direction, amount), count in diff.items():
            out.append({"kind": "row_only_in", "engine": engine, "date": day.isoformat() if day else None,
                        "direction": direction, "amount": format_decimal(amount), "count": count})
    if len(other) == 1:
        for name in local.headers:
            a, b = local.headers[name].best, other[0].headers[name].best
            if a and b and a.normalized != b.normalized:
                out.append({"kind": "field", "field": name, "local": a.normalized, "groq": b.normalized})
    return out


def _choose(local: AccountAnalysis, groq: list[AccountAnalysis], report: EngineReport) -> list[AccountAnalysis]:
    report.disagreements = _compare(local, groq)
    if len(groq) > 1:
        chosen = groq  # the local engine cannot separate accounts
    elif groq and _strength(groq[0]) > _strength(local):
        chosen = groq
    else:
        chosen = [local]
    for acc in chosen:
        report.selected[acc.key] = acc.engine
    primary = chosen[0]
    count = len(report.disagreements)
    proven = _strength(primary)[0]
    if count == 0:
        check = Check.of("engine_agreement", "Both engines agree", PASS,
                         message("engines_agree", "The local engine and the language model found the same rows and details."))
    else:
        check = Check.of("engine_agreement", "Both engines agree", INFO if proven else WARN, message(
            "engines_disagree_detail",
            f"The engines disagree on {count} item(s); the {primary.engine} result was kept"
            + (" because it reconciles." if proven else "."),
            count=count, kept=primary.engine, proven=proven))
    primary.validation.checks.append(check)
    return chosen


def _open_disagreements(accounts: list[AccountAnalysis], report: EngineReport) -> int:
    """Disagreements only matter when the kept result cannot prove itself."""
    if not report.disagreements:
        return 0
    if _strength(accounts[0])[0]:
        return 0
    return len(report.disagreements)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def run_progress(db: Session, run: ProcessingRun) -> Progress:
    """Progress callback that records the stage on the run (same session, same thread)."""
    def update(stage: str, current: int | None = None, total: int | None = None) -> None:
        if run.stage == stage and run.progress_current == current and run.progress_total == total:
            return
        run.stage, run.progress_current, run.progress_total = stage, current, total
        db.commit()
    return update


def process(db: Session, document: Document, run: ProcessingRun, engine: str | None = None,
            client: LLMClient | None = None) -> StatementExtraction | None:
    """Run the pipeline and persist the outcome, whatever it is."""
    engine = engine or run.engine_requested or settings.extraction_engine
    run.engine_requested = engine
    run.status = "processing"
    run.started_at = _now()
    run.stage = "reading"
    db.commit()
    progress = run_progress(db, run)

    content: DocumentContent | None = None
    pages = PageReport(detected=document.page_count)
    failure: ProcessingError | None = None
    try:
        content, pages = read_document(document, Path(document.preview_path), progress)
        if content.total_characters == 0:
            codes = {f["code"] for f in pages.failed}
            if "ocr_unavailable" in codes:
                raise ProcessingError("ocr_unavailable", "This document needs OCR, which is not available on this server.")
            raise ProcessingError("no_text", "No readable text was found in this document.")
    except ProcessingError as exc:
        failure = exc
    except Exception:  # pragma: no cover - unexpected reader failure
        logger.exception("document read failed for %s", document.id)
        failure = ProcessingError("read_failed", "This document could not be read.")

    analysis: Analysis | None = None
    if content is not None and failure is None:
        try:
            analysis = analyze(content, engine, client, progress, pages)
        except ProcessingError as exc:
            failure = exc
            llm_run = getattr(exc, "run", None)
            if llm_run is not None:
                _record_llm(run, llm_run)
        except Exception:
            logger.exception("analysis failed for %s", document.id)
            failure = ProcessingError("extraction_failed", "We couldn't extract the statement from this document.")

    progress("saving", None, None)
    run.pages_processed = len(pages.processed)
    run.pages_failed = len(pages.failed)
    extraction: StatementExtraction | None = None
    if analysis is not None and content is not None:
        extraction = StatementExtraction(document_id=document.id, processing_run_id=run.id)
        db.add(extraction)
        db.flush()
        _persist(db, extraction, analysis)
        run.status = "completed"
        run.text_source = content.source
        run.ocr_used = content.ocr_used
        run.structure_model = analysis.structured.model
        run.engine_used = analysis.engine.used
        run.fallback_reason = analysis.engine.fallback_reason
        if analysis.engine.llm is not None:
            _record_llm(run, analysis.engine.llm)
    else:
        run.status = "failed"
        run.error_code = failure.code if failure else "unknown"
        run.error_message = failure.message if failure else None

    run.stage = "done"
    run.completed_at = _now()
    db.commit()
    logger.info(
        "processed document=%s status=%s engine=%s rows=%s completeness=%s",
        document.id, run.status, run.engine_used,
        sum(len(a.table.rows) for a in analysis.accounts) if analysis else 0,
        analysis.completeness.status if analysis else "failed",
    )
    return extraction


def _record_llm(run: ProcessingRun, llm: LLMRun) -> None:
    run.model = llm.model
    run.prompt_version = llm.prompt_version
    run.schema_version = llm.schema_version
    run.llm_calls = llm.calls
    run.llm_input_tokens = llm.input_tokens
    run.llm_output_tokens = llm.output_tokens
    run.llm_latency_ms = llm.latency_ms
    run.chunks = [o.as_dict() for o in llm.outcomes]


def _decimal_text(value: Decimal | None) -> str | None:
    return format_decimal(value)


def _row_values(row: TableRow) -> dict[str, Any]:
    return {
        "date": row.date.isoformat() if row.date else None,
        "value_date": row.value_date.isoformat() if row.value_date else None,
        "description": row.description or None,
        "reference": row.reference or None,
        "debit": _decimal_text(row.debit),
        "credit": _decimal_text(row.credit),
        "amount": _decimal_text(row.amount),
        "balance": _decimal_text(row.balance),
        "currency": row.currency,
        "row_index": row.index,
    }


def _field_row(extraction_id: str, account: AccountAnalysis, spec) -> ExtractionField:
    found = account.headers[spec.name]
    validation: FieldValidation = account.validation.fields[spec.name]
    confidence = account.header_confidence[spec.name]
    best = found.best
    provenance = None
    evidence = None
    if best is not None:
        provenance = {"derived": "derived", "domain": "inferred", "majority_vote": "inferred",
                      "evidence": "inferred"}.get(best.strategy, "explicit")
        if best.strategy == "groq" and best.signals.get("verified") == 0.0:
            provenance = "unknown"
        evidence = {"engine": "groq" if best.strategy == "groq" else "local", "page": best.page_number,
                    "bbox": list(best.bbox), "excerpt": (best.source_text or "")[:300],
                    "lines": best.meta.get("lines"), "strategy": best.strategy,
                    "verified": None if best.strategy != "groq" else best.signals.get("verified") == 1.0}
    return ExtractionField(
        extraction_id=extraction_id,
        account_key=account.key,
        field_name=spec.name,
        display_order=spec.order,
        value=best.value if best else None,
        normalized_value=best.normalized if best else None,
        value_type=spec.value_type,
        confidence=confidence.confidence,
        confidence_status=confidence.status,
        validation_status=validation.status,
        validation_message=validation.message,
        validation_code=validation.code,
        source_page=best.page_number if best else None,
        source_bbox=list(best.bbox) if best else None,
        anchor_text=(best.anchor if best else None),
        candidates=[c.as_evidence() for c in found.candidates[:4]],
        reasons=confidence.reasons,
        reason_codes=confidence.reason_codes,
        provenance=provenance,
        evidence=evidence,
    )


def _persist(db: Session, extraction: StatementExtraction, analysis: Analysis) -> None:
    for account in analysis.accounts:
        for spec in HEADER_FIELDS:
            db.add(_field_row(extraction.id, account, spec))
        for row in account.table.rows:
            values = _row_values(row)
            db.add(
                Transaction(
                    extraction_id=extraction.id,
                    account_key=account.key,
                    row_index=row.index,
                    page=row.page,
                    bbox=list(row.bbox),
                    txn_date=values["date"],
                    value_date=values["value_date"],
                    date_inherited=row.date_inherited,
                    description=values["description"],
                    reference=values["reference"],
                    debit=values["debit"],
                    credit=values["credit"],
                    amount=values["amount"],
                    balance=values["balance"],
                    currency=row.currency,
                    direction_inferred=row.direction_inferred,
                    balance_check=row.balance_check,
                    confidence=row.confidence,
                    status=row.status,
                    issues=row.issues,
                    issue_codes=row.issue_codes,
                    reasons=row.reasons,
                    reason_codes=row.reason_codes,
                    signals=row.signals,
                    raw=row.raw,
                    source_engine=row.source_engine,
                    provenance=row.provenance,
                    evidence=row.evidence,
                    original=values,
                )
            )

    conv = analysis.primary.table.conventions
    extraction.overall_confidence = analysis.overall
    extraction.completeness_status = analysis.completeness.status
    extraction.engine = analysis.engine.used
    extraction.summary = {
        "schema": 2,
        "accounts": [account_summary(a) for a in analysis.accounts],
        "conventions": {
            "decimal": conv.number.decimal, "group": conv.number.group,
            "confident": conv.number.confident, "date_order": conv.order, "year": conv.year,
            "zero_decimals": conv.zero_decimals, "ocr": conv.ocr,
        },
        "completeness": analysis.completeness.as_dict(),
        "engine": analysis.engine.as_dict(),
        "pages": {"detected": analysis.pages.detected, "processed": analysis.pages.processed,
                  "failed": analysis.pages.failed},
        "chunks": {"total": analysis.chunks.total, "failed": analysis.chunks.failed,
                   "filled_by_local": analysis.chunks.filled_by_local},
    }


def account_summary(account: AccountAnalysis, dismissed: list[int] | None = None) -> dict[str, Any]:
    totals = account.validation.totals
    table = account.table
    return {
        "key": account.key,
        "engine": account.engine,
        "currency": totals.currency,
        "unverified_values": account.unverified_values,
        "columns": [vars(c) for c in table.columns],
        "checks": [vars(c) for c in account.validation.checks],
        "totals": {
            "opening_balance": format_decimal(totals.opening_balance),
            "opening_source": totals.opening_source,
            "opening_date": totals.opening_date.isoformat() if totals.opening_date else None,
            "closing_balance": format_decimal(totals.closing_balance),
            "closing_source": totals.closing_source,
            "closing_date": totals.closing_date.isoformat() if totals.closing_date else None,
            "total_debits": format_decimal(totals.total_debits),
            "total_credits": format_decimal(totals.total_credits),
            "printed_debits": format_decimal(totals.printed_debits),
            "printed_credits": format_decimal(totals.printed_credits),
            "net_movement": format_decimal(totals.total_credits - totals.total_debits),
            "currency": totals.currency,
            "mode": totals.mode,
            "order": totals.order,
            "unresolved_rows": totals.unresolved_rows,
        },
        "balance_rows": [
            {
                "kind": b.kind, "page": b.page, "text": b.text, "amount": format_decimal(b.amount),
                "date": b.row_date.isoformat() if b.row_date else None, "bbox": list(b.bbox),
            }
            for b in table.balance_rows
        ],
        "totals_rows": [
            {"page": t.page, "text": t.text, "bbox": list(t.bbox), "debit": format_decimal(t.debit),
             "credit": format_decimal(t.credit), "amount": format_decimal(t.amount), "direction": t.direction}
            for t in table.totals_rows
        ],
        "unresolved": [
            {"page": u.page, "text": u.text, "bbox": list(u.bbox), "reason": u.reason, "amounts": u.amounts,
             "dismissed": index in (dismissed or [])}
            for index, u in enumerate(table.unresolved)
        ],
        "duplicates": [list(p) for p in account.validation.duplicates],
        "has_balance_column": table.has_balance_column,
        "inferred_columns": table.inferred_columns,
        "empty_table_text": table.empty_table_text,
        "notes": table.notes,
        "note_codes": table.note_codes,
        "header_pages": table.header_pages,
    }


# --------------------------------------------------------------------------
# Revalidation: rebuild the domain objects from storage and check again
# --------------------------------------------------------------------------


def conventions_from(summary: dict[str, Any]) -> Conventions:
    data = summary.get("conventions") or {}
    number = NumberConvention(data.get("decimal", "."), data.get("group", ","), bool(data.get("confident", False)))
    return Conventions(number=number, order=data.get("date_order", "dmy"), year=data.get("year"),
                       ocr=bool(data.get("ocr", False)), zero_decimals=bool(data.get("zero_decimals", False)))


def _dec(value: str | None) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


_LEGACY_VALIDATION_TEXT = ("Balance changed by", "Debit and credit look swapped", "direction", "Amount does not match",
                           "May duplicate row")


def row_from_db(t: Transaction) -> TableRow:
    debit, credit, amount = _dec(t.debit), _dec(t.credit), _dec(t.amount)
    if t.direction_inferred and not t.corrected and amount is None:
        # Inferred directions are re-derived from the current data, never frozen.
        amount = debit if debit is not None else credit
        debit = credit = None
    row = TableRow(
        index=t.row_index, page=t.page, bbox=tuple(t.bbox or (0.0, 0.0, 0.0, 0.0)),  # type: ignore[arg-type]
        raw=dict(t.raw or {}), date=_day(t.txn_date), value_date=_day(t.value_date),
        date_inherited=bool(t.date_inherited), description=t.description or "", reference=t.reference or "",
        debit=debit, credit=credit, amount=amount, balance=_dec(t.balance),
        fit=float((t.signals or {}).get("alignment", 1.0)),
        account_key=t.account_key or "A1", currency=t.currency, source_engine=t.source_engine or "local",
        provenance=t.provenance or "explicit", evidence=dict(t.evidence or {}) or None, row_id=t.id,
        corrected=bool(t.corrected),
    )
    if t.issue_codes is not None:
        for entry in t.issue_codes:
            if entry.get("code") not in VALIDATION_CODES:
                row.add_issue(Message(entry["code"], entry.get("text", ""), entry.get("params") or {}))
    else:
        for text in t.issues or []:
            if not any(marker in text for marker in _LEGACY_VALIDATION_TEXT):
                row.add_issue(message("legacy_issue", text))
    row.ocr_repaired = any(e["code"] == "amount_ocr_repaired" for e in row.issue_codes)
    return row


def _candidate_from_db(f: ExtractionField) -> Candidate | None:
    if f.value is None and f.normalized_value is None:
        return None
    first = (f.candidates or [{}])[0] if f.candidates else {}
    strategy = "user" if f.corrected else (first.get("strategy") or "labelled")
    evidence = f.evidence or {}
    signals: dict[str, float] = {}
    if strategy == "groq":
        signals["verified"] = 1.0 if evidence.get("verified") else 0.0
    return Candidate(
        value=f.value or f.normalized_value or "", normalized=f.normalized_value or f.value or "",
        page_number=f.source_page or 0, bbox=tuple(f.source_bbox or (0.0, 0.0, 0.0, 0.0)),  # type: ignore[arg-type]
        source_text=first.get("text") or "", anchor=f.anchor_text, strategy=strategy, signals=signals,
    )


def _table_from_summary(acc: dict[str, Any], rows: list[TableRow], conv: Conventions) -> TableResult:
    table = TableResult(rows=rows, conventions=conv)
    table.columns = [DetectedColumn(**c) for c in acc.get("columns", [])]
    table.balance_rows = [
        BalanceRow(b["kind"], b["page"], b["text"], tuple(b.get("bbox") or (0.0, 0.0, 0.0, 0.0)),  # type: ignore[arg-type]
                   Decimal(b["amount"]), _day(b.get("date")))
        for b in acc.get("balance_rows", []) if b.get("amount") is not None
    ]
    table.totals_rows = [
        TotalsRow(t["page"], t["text"], tuple(t.get("bbox") or (0.0, 0.0, 0.0, 0.0)),  # type: ignore[arg-type]
                  debit=_dec(t.get("debit")), credit=_dec(t.get("credit")), amount=_dec(t.get("amount")),
                  direction=t.get("direction"))
        for t in acc.get("totals_rows", [])
    ]
    table.unresolved = [
        UnresolvedLine(u["page"], u["text"], tuple(u.get("bbox") or (0.0, 0.0, 0.0, 0.0)), u["reason"],  # type: ignore[arg-type]
                       u.get("amounts") or [])
        for u in acc.get("unresolved", []) if not u.get("dismissed")
    ]
    table.header_pages = list(acc.get("header_pages") or [])
    table.inferred_columns = bool(acc.get("inferred_columns"))
    table.empty_table_text = acc.get("empty_table_text")
    table.notes = list(acc.get("notes") or [])
    table.note_codes = list(acc.get("note_codes") or [])
    return table


def legacy_account(summary: dict[str, Any]) -> dict[str, Any]:
    """Summaries written before schema 2 held one account at the top level."""
    return {
        "key": "A1", "engine": "local", "currency": (summary.get("totals") or {}).get("currency"),
        "columns": summary.get("columns", []), "checks": summary.get("checks", []),
        "totals": summary.get("totals") or {}, "balance_rows": summary.get("balance_rows", []),
        "totals_rows": [], "unresolved": [], "duplicates": [],
        "has_balance_column": summary.get("has_balance_column", True),
        "inferred_columns": summary.get("inferred_columns", False),
        "empty_table_text": summary.get("empty_table_text"), "notes": summary.get("notes", []),
        "note_codes": [], "header_pages": summary.get("header_pages", []), "unverified_values": 0,
    }


def summary_accounts(summary: dict[str, Any]) -> list[dict[str, Any]]:
    if summary.get("accounts"):
        return list(summary["accounts"])
    return [legacy_account(summary)]


def revalidate(db: Session, extraction: StatementExtraction) -> Completeness:
    """Validate the stored (possibly corrected) data again and store the new results."""
    summary = dict(extraction.summary or {})
    conv = conventions_from(summary)
    stored_accounts = {a["key"]: a for a in summary_accounts(summary)}
    fields = list(db.scalars(select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)))
    txns = list(db.scalars(select(Transaction).where(Transaction.extraction_id == extraction.id)
                           .order_by(Transaction.row_index)))
    keys = list(stored_accounts) or ["A1"]
    for f in fields:
        if (f.account_key or "A1") not in keys:
            keys.append(f.account_key or "A1")
    for t in txns:
        if (t.account_key or "A1") not in keys:
            keys.append(t.account_key or "A1")

    accounts: list[AccountAnalysis] = []
    new_summaries: list[dict[str, Any]] = []
    by_txn_id = {t.id: t for t in txns}
    by_field = {}
    for key in keys:
        stored = dict(stored_accounts.get(key) or legacy_account({}))
        stored["key"] = key
        headers = {spec.name: FieldExtraction(spec) for spec in HEADER_FIELDS}
        for f in fields:
            if (f.account_key or "A1") == key and f.field_name in headers:
                by_field[(key, f.field_name)] = f
                candidate = _candidate_from_db(f)
                if candidate is not None:
                    headers[f.field_name].candidates.append(candidate)
        live = [t for t in txns if (t.account_key or "A1") == key and not t.deleted]
        rows = [row_from_db(t) for t in live]
        for index, row in enumerate(rows, start=1):
            row.index = index
        not_duplicate = {t.id for t in live if (t.evidence or {}).get("not_duplicate")}
        table = _table_from_summary(stored, rows, conv)
        account = _finish_account(key, headers, table, int(stored.get("unverified_values") or 0),
                                  stored.get("engine") or "local")
        if not_duplicate:
            _drop_dismissed_duplicates(account, not_duplicate)
        for check in stored.get("checks", []):
            if check.get("key") in ("engine", "engine_agreement"):
                account.validation.checks.append(Check(**check))
        accounts.append(account)
        new = account_summary(account)
        new["unresolved"] = stored.get("unresolved", [])  # stable indexes; dismissed lines stay visible
        new_summaries.append(new)

        # Write the recomputed state back.
        for row in rows:
            t = by_txn_id[row.row_id]  # type: ignore[index]
            # row_index stays the stable storage position; the shown index is computed.
            t.debit, t.credit, t.amount = _decimal_text(row.debit), _decimal_text(row.credit), _decimal_text(row.amount)
            t.direction_inferred = row.direction_inferred
            t.balance_check = row.balance_check
            t.confidence, t.status = row.confidence, row.status
            t.issues, t.issue_codes = row.issues, row.issue_codes
            t.reasons, t.reason_codes = row.reasons, row.reason_codes
            t.signals = row.signals
        for name, extraction_field in headers.items():
            f = by_field.get((key, name))
            if f is None:
                continue
            validation = account.validation.fields[name]
            f.validation_status, f.validation_message, f.validation_code = validation.status, validation.message, validation.code
            if f.corrected or f.value is None:
                confidence = account.header_confidence[name]
                f.confidence, f.confidence_status = confidence.confidence, confidence.status
                f.reasons, f.reason_codes = confidence.reasons, confidence.reason_codes
            if f.value is None:
                f.validation_status = NOT_FOUND

    pages_data = summary.get("pages") or {}
    pages = PageReport(detected=int(pages_data.get("detected") or 0), processed=list(pages_data.get("processed") or []),
                       failed=list(pages_data.get("failed") or []))
    chunk_data = summary.get("chunks") or {}
    chunks = ChunkReport(total=int(chunk_data.get("total") or 0), failed=int(chunk_data.get("failed") or 0),
                         filled_by_local=int(chunk_data.get("filled_by_local") or 0))
    engine = summary.get("engine") or {}
    report = EngineReport(requested=engine.get("requested") or "local", used=engine.get("used"),
                          disagreements=list(engine.get("disagreements") or []))
    completeness = completeness_service.assess(
        [AccountEvidence(a.key, a.headers, a.table, a.validation, a.unverified_values) for a in accounts],
        pages, chunks,
        engine_disagreements=_open_disagreements(accounts, report) if accounts else 0,
    )
    header_scores = [FieldConfidence(f.confidence, f.confidence_status) for f in fields if f.confidence is not None]
    extraction.overall_confidence = confidence_service.overall(header_scores, [r for a in accounts for r in a.table.rows])
    summary["accounts"] = new_summaries
    summary["completeness"] = completeness.as_dict()
    summary.setdefault("schema", 2)
    extraction.summary = summary
    flag_modified(extraction, "summary")
    extraction.completeness_status = completeness.status
    extraction.revalidated_at = _now()
    return completeness


def _drop_dismissed_duplicates(account: AccountAnalysis, not_duplicate: set[str]) -> None:
    """A reviewer confirmed these rows are genuine: drop their duplicate flags."""
    rows = {r.index: r for r in account.table.rows}
    kept: list[tuple[int, int]] = []
    for later, first in account.validation.duplicates:
        row = rows.get(later)
        if row is not None and row.row_id in not_duplicate:
            row.issue_codes = [e for e in row.issue_codes if e["code"] != "duplicate_candidate"]
            row.issues = [e["text"] for e in row.issue_codes]
            confidence_service.score_row(row, account.table.has_balance_column)
            continue
        kept.append((later, first))
    account.validation.duplicates = kept
    if not kept:
        account.validation.checks = [
            Check.of("duplicates", "No duplicated rows", PASS,
                     message("duplicates_reviewed", "Rows flagged as duplicates were confirmed by a reviewer."))
            if c.key == "duplicates" else c
            for c in account.validation.checks
        ]

