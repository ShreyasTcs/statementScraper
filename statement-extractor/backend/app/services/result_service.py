"""Turn stored rows into API payloads and exports."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime
from typing import Any

from app.models import Document, ExtractionField, ProcessingRun, StatementExtraction, Transaction
from app.rules.statement import HEADER_FIELD_BY_NAME
from app.schemas.schemas import (
    AccountOut,
    BalanceRowOut,
    CheckOut,
    ChunkOut,
    ColumnOut,
    CompletenessOut,
    ConventionsOut,
    DocumentListItem,
    DocumentOut,
    EngineOut,
    EvidenceOut,
    FieldOut,
    MessageOut,
    ResultOut,
    RunOut,
    SourceRef,
    SummaryOut,
    TotalsOut,
    TransactionOut,
    UnresolvedOut,
)
from app.services.pipeline import summary_accounts


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def run_out(run: ProcessingRun) -> RunOut:
    error = None
    if run.status == "failed":
        error = MessageOut(code=run.error_code or "extraction_failed",
                           text=run.error_message or "We couldn't extract this statement.")
    return RunOut(
        id=run.id, status=run.status, stage=run.stage,  # type: ignore[arg-type]
        progress_current=run.progress_current, progress_total=run.progress_total,
        engine_requested=run.engine_requested, engine_used=run.engine_used, fallback_reason=run.fallback_reason,
        model=run.model, prompt_version=run.prompt_version, schema_version=run.schema_version,
        llm_calls=run.llm_calls, llm_input_tokens=run.llm_input_tokens, llm_output_tokens=run.llm_output_tokens,
        llm_latency_ms=run.llm_latency_ms, pages_processed=run.pages_processed, pages_failed=run.pages_failed,
        chunks=[ChunkOut(**c) for c in (run.chunks or [])], error=error,
        created_at=_iso(run.created_at), started_at=_iso(run.started_at), completed_at=_iso(run.completed_at),
    )


def document_out(document: Document, run: ProcessingRun | None) -> DocumentOut:
    return DocumentOut(
        id=document.id, filename=document.original_filename, file_type=document.file_type,
        page_count=document.page_count, file_size_bytes=document.file_size_bytes or 0,
        created_at=_iso(document.created_at), ocr_used=bool(run.ocr_used) if run else False,
        preview_url=f"/api/v1/documents/{document.id}/preview",
    )


def build_result(
    document: Document,
    run: ProcessingRun | None,
    extraction: StatementExtraction | None,
    fields: list[ExtractionField],
    transactions: list[Transaction],
    corrections: int = 0,
) -> ResultOut:
    summary_data = (extraction.summary if extraction else None) or {}
    notice = None
    if run is not None and run.status == "failed":
        notice = MessageOut(code=run.error_code or "extraction_failed",
                            text=run.error_message or "We couldn't extract this statement.")

    accounts: list[AccountOut] = []
    stored = summary_accounts(summary_data) if extraction else []
    keys = [a["key"] for a in stored]
    for item in list(fields) + list(transactions):
        key = item.account_key or "A1"
        if key not in keys:
            keys.append(key)
    by_key = {a["key"]: a for a in stored}
    for key in keys:
        acc = by_key.get(key, {})
        header = [_field_out(f) for f in fields if (f.account_key or "A1") == key]
        rows = [_txn_out(t, n) for t, n in display_indexes([t for t in transactions if (t.account_key or "A1") == key])]
        totals = acc.get("totals") or {}
        label = next((f.value for f in header if f.name in ("account_number", "iban") and f.value), None)
        accounts.append(AccountOut(
            key=key, label=label, engine=acc.get("engine"), currency=acc.get("currency") or totals.get("currency"),
            header=header,
            columns=[ColumnOut(**c) for c in acc.get("columns", [])],
            transactions=rows,
            checks=[CheckOut(**c) for c in acc.get("checks", [])],
            totals=TotalsOut(**{k: v for k, v in totals.items() if k in TotalsOut.model_fields and v is not None}),
            balance_rows=[BalanceRowOut(**{k: v for k, v in b.items() if k in BalanceRowOut.model_fields})
                          for b in acc.get("balance_rows", [])],
            unresolved=[UnresolvedOut(index=i, **{k: v for k, v in u.items() if k in UnresolvedOut.model_fields})
                        for i, u in enumerate(acc.get("unresolved", []))],
            duplicates=acc.get("duplicates", []),
            has_balance_column=bool(acc.get("has_balance_column", True)),
            empty_table_text=acc.get("empty_table_text"),
            notes=acc.get("notes", []),
            note_codes=[MessageOut(**n) for n in acc.get("note_codes", [])],
        ))

    completeness_data = summary_data.get("completeness") or {}
    if extraction is None:
        completeness = CompletenessOut(status="failed", reasons=[notice] if notice else [])
    elif completeness_data:
        completeness = CompletenessOut(
            status=completeness_data.get("status") or "needs_review",
            reasons=[MessageOut(**r) for r in completeness_data.get("reasons", [])],
            metrics=completeness_data.get("metrics") or {},
        )
    else:
        # Extracted before completeness existed: never presumed complete.
        completeness = CompletenessOut(status="needs_review", reasons=[MessageOut(
            code="legacy_not_assessed", text="Extracted by an earlier version; revalidate to assess completeness.")])

    engine_data = summary_data.get("engine") or {}
    engine = EngineOut(
        requested=engine_data.get("requested") or (run.engine_requested if run else None),
        used=engine_data.get("used") or (run.engine_used if run else None),
        fallback_reason=engine_data.get("fallback_reason") or (run.fallback_reason if run else None),
        selected=engine_data.get("selected") or {},
        disagreements=engine_data.get("disagreements") or [],
        disagreement_count=int(engine_data.get("disagreement_count") or 0),
        model=engine_data.get("model") or (run.model if run else None),
    )
    conventions = summary_data.get("conventions") or {}
    return ResultOut(
        document=document_out(document, run),
        run=run_out(run) if run else None,
        summary=_summary(accounts, extraction.overall_confidence if extraction else None, notice, completeness),
        completeness=completeness,
        engine=engine,
        accounts=accounts,
        conventions=ConventionsOut(**{k: v for k, v in conventions.items() if k in ConventionsOut.model_fields}),
        notice=notice,
        corrected=bool(extraction.corrected) if extraction else False,
        corrections=corrections,
        revalidated_at=_iso(extraction.revalidated_at) if extraction else None,
    )


def _evidence(data: dict | None) -> EvidenceOut | None:
    if not data:
        return None
    return EvidenceOut(**{k: v for k, v in data.items() if k in EvidenceOut.model_fields})


def _messages(entries: list | None, texts: list | None = None) -> list[MessageOut]:
    if entries:
        return [MessageOut(**e) for e in entries]
    return [MessageOut(code="legacy", text=t) for t in (texts or [])]


def _field_out(row: ExtractionField) -> FieldOut:
    spec = HEADER_FIELD_BY_NAME[row.field_name]
    found = row.value is not None
    first = (row.candidates or [{}])[0] if row.candidates else {}
    source = (
        SourceRef(page=row.source_page, bbox=row.source_bbox, text=first.get("text"), anchor=row.anchor_text)
        if found and row.source_page and row.source_bbox and any(row.source_bbox)
        else None
    )
    alternatives = [
        c["value"] for c in (row.candidates or [])[1:] if c.get("value") and c["value"] != row.value
    ]
    return FieldOut(
        id=row.id,
        account=row.account_key or "A1",
        name=row.field_name,
        label=spec.label,
        value=row.value,
        normalized=row.normalized_value,
        value_type=row.value_type,
        required=spec.required,
        found=found,
        confidence=row.confidence,
        status=row.confidence_status if found else "not_found",  # type: ignore[arg-type]
        validation=row.validation_status if found else "not_found",  # type: ignore[arg-type]
        validation_message=row.validation_message,
        validation_code=row.validation_code,
        reasons=row.reasons or [],
        reason_codes=_messages(row.reason_codes, row.reasons),
        source=source,
        alternatives=alternatives,
        provenance=row.provenance,
        evidence=_evidence(row.evidence),
        corrected=bool(row.corrected),
        original_value=row.original_value,
    )


def display_indexes(transactions: list[Transaction]) -> list[tuple[Transaction, int]]:
    """Position among the live rows of one account (1..n); deleted rows get 0."""
    out: list[tuple[Transaction, int]] = []
    counter = 0
    for t in sorted(transactions, key=lambda r: r.row_index):
        if t.deleted:
            out.append((t, 0))
        else:
            counter += 1
            out.append((t, counter))
    return out


def _txn_out(row: Transaction, index: int) -> TransactionOut:
    return TransactionOut(
        id=row.id,
        account=row.account_key or "A1",
        index=index,
        page=row.page,
        date=row.txn_date,
        value_date=row.value_date,
        date_inherited=row.date_inherited,
        description=row.description,
        reference=row.reference,
        debit=row.debit,
        credit=row.credit,
        amount=row.amount,
        balance=row.balance,
        currency=row.currency,
        direction_inferred=row.direction_inferred,
        balance_check=row.balance_check if not row.deleted else "unchecked",  # type: ignore[arg-type]
        confidence=row.confidence,
        status=row.status,  # type: ignore[arg-type]
        issues=row.issues or [],
        issue_codes=_messages(row.issue_codes, row.issues),
        reasons=row.reasons or [],
        reason_codes=_messages(row.reason_codes, row.reasons),
        bbox=row.bbox,
        source_engine=row.source_engine,
        provenance=row.provenance,
        evidence=_evidence(row.evidence),
        corrected=bool(row.corrected),
        deleted=bool(row.deleted),
        original=row.original,
    )


def _summary(accounts: list[AccountOut], overall: float | None, notice: MessageOut | None,
             completeness: CompletenessOut) -> SummaryOut:
    header = [f for a in accounts for f in a.header]
    live = [r for a in accounts for r in a.transactions if not r.deleted]
    missing = [f.label for f in header if not f.found]
    counts = {s: sum(1 for r in live if r.status == s) for s in ("high", "review", "needs_review")}
    reconciled = sum(1 for r in live if r.balance_check == "ok")
    mismatched = sum(1 for r in live if r.balance_check == "mismatch")
    n = len(live)

    if notice is not None:
        headline = MessageOut(code="summary_failed", text="Extraction failed")
        detail = notice
        tone = "issue"
    elif completeness.status == "complete" and n == 0:
        headline = MessageOut(code="summary_empty", text="No transactions in this period")
        detail = MessageOut(code="summary_empty_detail", text="The statement itself reports that there were no transactions.")
        tone = "ready"
    elif completeness.status == "complete":
        headline = MessageOut(code="summary_complete", text="Extraction complete")
        detail = MessageOut(code="summary_complete_detail", params={"rows": n},
                            text=f"{n} transactions extracted and the statement reconciles.")
        tone = "ready"
    elif completeness.status == "partial":
        headline = MessageOut(code="summary_partial", text="Only part of the document was processed")
        detail = completeness.reasons[0] if completeness.reasons else MessageOut(code="summary_partial", text="")
        tone = "issue"
    else:
        headline = MessageOut(code="summary_needs_review", text="Extracted - needs review")
        detail = completeness.reasons[0] if completeness.reasons else MessageOut(
            code="summary_needs_review_detail", params={"rows": n}, text=f"{n} transactions extracted.")
        tone = "review" if not mismatched else "issue"

    return SummaryOut(
        headline=headline, detail=detail, tone=tone,  # type: ignore[arg-type]
        accounts=max(len(accounts), 1),
        header_total=len(header), header_found=len(header) - len(missing), header_missing=missing,
        rows=n, rows_high=counts["high"], rows_review=counts["review"], rows_needs_review=counts["needs_review"],
        rows_reconciled=reconciled, rows_mismatched=mismatched,
        rows_corrected=sum(1 for r in live if r.corrected),
        rows_deleted=sum(1 for a in accounts for r in a.transactions if r.deleted),
        overall_confidence=overall,
    )


def list_item(document: Document, run: ProcessingRun | None, extraction: StatementExtraction | None,
              rows: int) -> DocumentListItem:
    return DocumentListItem(
        id=document.id, filename=document.original_filename, created_at=_iso(document.created_at),
        page_count=document.page_count, file_size_bytes=document.file_size_bytes or 0,
        status=run.status if run else None, stage=run.stage if run else None,
        engine_requested=run.engine_requested if run else None, engine_used=run.engine_used if run else None,
        completeness=extraction.completeness_status if extraction else ("failed" if run and run.status == "failed" else None),
        rows=rows, overall_confidence=extraction.overall_confidence if extraction else None,
        corrected=bool(extraction.corrected) if extraction else False,
        error_code=run.error_code if run and run.status == "failed" else None,
    )


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")
_PLAIN_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def csv_safe(value: Any) -> str:
    """Neutralise spreadsheet formulas (CSV injection); plain numbers stay numbers."""
    text = "" if value is None else str(value)
    stripped = text.lstrip(" ")
    if stripped and stripped[0] in _FORMULA_START and not _PLAIN_NUMBER.match(stripped):
        return "'" + text
    return text


def transactions_csv(transactions: list[Transaction]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["#", "Account", "Page", "Date", "Value date", "Description", "Reference", "Debit", "Credit",
         "Amount (direction unknown)", "Balance", "Currency", "Reconciles", "Confidence", "Source", "Corrected"]
    )  # amounts are plain numbers: no thousands separators, "." decimal
    accounts: dict[str, list[Transaction]] = {}
    for t in transactions:
        accounts.setdefault(t.account_key or "A1", []).append(t)
    for rows in accounts.values():
        for t, index in display_indexes(rows):
            if t.deleted:
                continue
            writer.writerow([csv_safe(v) for v in (
                index, t.account_key or "A1", t.page, t.txn_date or "", t.value_date or "", t.description or "",
                t.reference or "", t.debit or "", t.credit or "", t.amount or "", t.balance or "", t.currency or "",
                t.balance_check, "" if t.confidence is None else f"{t.confidence:.2f}", t.source_engine or "local",
                "yes" if t.corrected else "",
            )])
    return buffer.getvalue()


def result_json(result: ResultOut) -> str:
    data = result.model_dump(mode="json")
    for account in data["accounts"]:
        account["transactions"] = [t for t in account["transactions"] if not t["deleted"]]
    return json.dumps(data, ensure_ascii=False, indent=2)
