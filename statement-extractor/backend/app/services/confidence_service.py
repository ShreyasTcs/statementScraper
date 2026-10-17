"""Heuristic confidence scoring.

No ML. Each score is a weighted sum of signals that can be observed and
explained in plain language; the weights are configuration, not magic.

* Header fields: was a label found, does the value parse, is it close to the
  label, is it the only strong candidate, was it read from real PDF text.
* Transaction rows: valid date, a clear amount, a balance, whether the row
  reconciles with the running balance, and how good the evidence is (real PDF
  text, OCR, or values a language model returned that were - or were not -
  found again in the cited source lines).

A score never claims more than the evidence supports: a row that does not
reconcile, carries an issue, or has a value that could not be verified in the
document is capped below the "high" band whatever its other signals say.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.config import settings
from app.core.messages import Message, add_message, message
from app.services.extraction_service import FieldExtraction
from app.services.table_service import TableRow
from app.services.validation_service import (
    INVALID,
    NOT_FOUND,
    REVIEW,
    FieldValidation,
)

HIGH = "high"
NEEDS_REVIEW = "needs_review"

HEADER_WEIGHTS: dict[str, float] = {
    "candidate_found": 0.20,
    "anchor_match": 0.22,
    "format_valid": 0.18,
    "spatial_proximity": 0.15,
    "unique_candidate": 0.15,
    "source_quality": 0.10,
}

HEADER_REASONS: dict[str, tuple[str, str]] = {
    "anchor_match": ("label_found", "Clear field label found"),
    "format_valid": ("format_valid", "Value format is valid"),
    "spatial_proximity": ("near_label", "Value is next to its label"),
    "unique_candidate": ("unique_candidate", "Only one strong candidate"),
    "source_quality": ("pdf_text", "Read directly from the PDF text"),
}

ROW_WEIGHTS: dict[str, float] = {
    "date": 0.15,
    "movement": 0.20,
    "balance": 0.10,
    "reconciles": 0.30,
    "description": 0.05,
    "alignment": 0.05,
    "evidence": 0.15,
}


@dataclass
class FieldConfidence:
    confidence: float | None
    status: str
    signals: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    reason_codes: list[dict] = field(default_factory=list)

    def add_reason(self, msg: Message) -> None:
        add_message(self.reasons, self.reason_codes, msg)


def status_for(confidence: float) -> str:
    if confidence >= settings.confidence_high:
        return HIGH
    if confidence >= settings.confidence_review:
        return REVIEW
    return NEEDS_REVIEW


def evidence_signal(row: TableRow) -> float:
    """How trustworthy the source of this row's values is (0..1)."""
    evidence = row.evidence or {}
    if row.source_engine == "user" or evidence.get("engine") == "user":
        return 1.0
    verified = evidence.get("verified")
    if isinstance(verified, dict) and verified:
        checked = list(verified.values())
        share = sum(1 for v in checked if v) / len(checked)
        return share if all(checked) else min(share, 0.4)
    if row.ocr_repaired:
        return 0.5
    if evidence.get("text_source") == "ocr":
        return 0.75
    return 1.0


def has_unverified(row: TableRow) -> bool:
    verified = (row.evidence or {}).get("verified")
    return isinstance(verified, dict) and not all(verified.values())


class ConfidenceService:
    # ---------------------------------------------------------------- header

    def score_header(self, extraction: FieldExtraction, validation: FieldValidation) -> FieldConfidence:
        best = extraction.best
        if best is None or validation.status == NOT_FOUND:
            return FieldConfidence(None, NOT_FOUND)

        if best.strategy == "user":
            result = FieldConfidence(1.0, HIGH if validation.status != INVALID else NEEDS_REVIEW, {"user": 1.0})
            result.add_reason(message("user_corrected", "Entered or confirmed by a reviewer"))
            if validation.status == INVALID:
                result.confidence = 0.45
            return result

        raw = best.signals
        signals = {
            "candidate_found": 1.0,
            "anchor_match": float(raw.get("anchor_weight", 0.0)),
            "format_valid": 0.0 if validation.status == INVALID else 1.0,
            "spatial_proximity": float(raw.get("spatial_proximity", 0.0)),
            "unique_candidate": 0.0 if extraction.is_ambiguous else 1.0,
            "source_quality": float(raw.get("source_quality", 0.0)),
        }
        confidence = sum(HEADER_WEIGHTS[k] * v for k, v in signals.items())
        if validation.status == INVALID:
            confidence = min(confidence, 0.45)
        elif validation.status == REVIEW:
            confidence = min(confidence, 0.85)
        if raw.get("verified") == 0.0:
            confidence = min(confidence, 0.6)
        confidence = round(max(0.0, min(confidence, 1.0)), 4)

        result = FieldConfidence(confidence, status_for(confidence), {k: round(v, 3) for k, v in signals.items()})
        for key, (code, text) in HEADER_REASONS.items():
            if signals[key] < 0.6:
                continue
            if key == "anchor_match" and best.strategy in ("masthead", "majority_vote", "groq"):
                continue
            result.add_reason(message(code, text))
        if best.strategy == "masthead":
            result.add_reason(message("masthead", "Found in the page-1 masthead"))
        if best.strategy == "majority_vote":
            result.add_reason(message("majority_currency", "Most common currency in the document"))
        if best.strategy == "groq":
            if raw.get("verified") == 0.0:
                result.add_reason(message("llm_unverified", "Returned by the language model but not found in the cited text"))
            else:
                result.add_reason(message("llm_verified", "Located by the language model and found in the cited text"))
        return result

    # ------------------------------------------------------------------ rows

    def score_row(self, row: TableRow, has_balance_column: bool = True) -> None:
        has_movement = row.debit is not None or row.credit is not None
        signals = {
            "date": 0.0 if row.date is None else (0.6 if row.date_inherited else 1.0),
            "movement": (0.8 if row.direction_inferred else 1.0)
            if has_movement and not (row.debit is not None and row.credit is not None)
            else (0.3 if row.amount is not None else 0.0),
            # Tables with no balance column are not penalised for lacking one.
            "balance": 1.0 if (row.balance is not None or not has_balance_column) else 0.0,
            "reconciles": {"ok": 1.0, "unchecked": 0.6, "mismatch": 0.0}.get(row.balance_check, 0.6),
            "description": 1.0 if row.description else 0.0,
            "alignment": row.fit,
            "evidence": evidence_signal(row),
        }
        confidence = sum(ROW_WEIGHTS[k] * v for k, v in signals.items())
        if row.balance_check == "mismatch":
            confidence = min(confidence, 0.55)
        if has_unverified(row):
            confidence = min(confidence, 0.6)
        if row.issues:
            confidence = min(confidence, 0.65)
        row.confidence = round(max(0.0, min(confidence, 1.0)), 4)
        row.status = status_for(row.confidence)
        row.signals = {k: round(v, 3) for k, v in signals.items()}

        row.reasons, row.reason_codes = [], []
        if row.balance_check == "ok":
            if has_balance_column:
                row.add_reason(message("balance_reconciles", "Balance reconciles with the previous row"))
            else:
                row.add_reason(message("totals_reconcile", "The statement's opening, closing and totals reconcile"))
        elif row.balance_check == "unchecked":
            row.add_reason(message("not_checkable", "Could not be checked against a balance or totals"))
        if row.direction_inferred:
            row.add_reason(message("direction_inferred", "Debit / credit direction inferred from the balance"))
        if row.date_inherited:
            row.add_reason(message("date_inherited", "Date carried over from the row above"))
        if row.source_engine == "groq" and not has_unverified(row):
            row.add_reason(message("llm_verified", "Values returned by the language model were found in the cited text"))
        if row.source_engine == "user":
            row.add_reason(message("user_entered", "Entered by a reviewer"))
        elif row.corrected:
            row.add_reason(message("user_corrected", "Corrected by a reviewer"))
        for entry in row.issue_codes:
            add_message(row.reasons, row.reason_codes, Message(entry["code"], entry["text"], entry.get("params") or {}))

    def overall(
        self, header: list[FieldConfidence], rows: list[TableRow]
    ) -> float | None:
        header_scores = [h.confidence for h in header if h.confidence is not None]
        row_scores = [r.confidence for r in rows if r.confidence is not None]
        parts: list[tuple[float, float]] = []
        if header_scores:
            parts.append((sum(header_scores) / len(header_scores), 0.3))
        if row_scores:
            parts.append((sum(row_scores) / len(row_scores), 0.7))
        if not parts:
            return None
        total = sum(w for _, w in parts)
        return round(sum(v * w for v, w in parts) / total, 4)


confidence_service = ConfidenceService()
