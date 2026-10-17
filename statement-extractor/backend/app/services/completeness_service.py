"""Is the extraction *complete* - and can that be shown?

A statement is never complete just because the pipeline finished. The status is
decided from evidence:

``failed``
    nothing usable was produced (unreadable file, every page failed, engine error).
``partial``
    part of the document was not processed: pages that could not be read or
    language-model chunks that failed. Values may be missing.
``needs_review``
    everything was processed, but something is unproven or suspicious: the
    books do not balance, duplicate or unresolved rows, values that could not be
    verified in the document, directions that could not be determined, required
    details missing, or nothing (balance / totals) to prove the rows against.
``complete``
    every page was processed and the arithmetic proves the rows: the running
    balance reconciles row by row, or opening + credits − debits = closing, or
    the printed totals match - with no failed check and nothing unresolved.
    A statement that prints "no transactions" is complete when nothing
    contradicts it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.messages import Message, add_message, message
from app.services.extraction_service import FieldExtraction
from app.services.table_service import TableResult
from app.services.validation_service import FAIL, PASS, StatementValidation

COMPLETE, PARTIAL, NEEDS_REVIEW, FAILED = "complete", "partial", "needs_review", "failed"

REQUIRED_FIELDS = ("bank_name", "account_holder", "account_number", "period_from", "period_to")


@dataclass
class PageReport:
    detected: int = 0
    processed: list[int] = field(default_factory=list)
    #: {"page": n, "code": "ocr_failed" | "ocr_unavailable" | "page_unreadable"}
    failed: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ChunkReport:
    total: int = 0
    failed: int = 0
    #: Failed chunks whose pages were filled in by the local engine (hybrid).
    filled_by_local: int = 0


@dataclass
class AccountEvidence:
    key: str
    headers: dict[str, FieldExtraction]
    table: TableResult
    validation: StatementValidation
    #: Values a language model returned that were not found in the cited lines.
    unverified_values: int = 0
    #: Lines the model said it could not interpret.
    skipped_lines: int = 0


@dataclass
class Completeness:
    status: str
    reasons: list[str] = field(default_factory=list)
    reason_codes: list[dict] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def add(self, msg: Message) -> None:
        add_message(self.reasons, self.reason_codes, msg)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": self.reason_codes, "metrics": self.metrics}


def _identifier_found(headers: dict[str, FieldExtraction]) -> bool:
    return any(headers.get(n) and headers[n].best for n in ("account_number", "iban"))


def assess(
    accounts: list[AccountEvidence],
    pages: PageReport,
    chunks: ChunkReport | None = None,
    *,
    multiple_accounts_suspected: bool = False,
    engine_disagreements: int = 0,
) -> Completeness:
    chunks = chunks or ChunkReport()
    result = Completeness(status=COMPLETE)
    blocking_partial = False
    review = False

    # ------------------------------------------------------------ coverage
    if pages.failed:
        blocking_partial = True
        result.add(message("pages_failed", f"{len(pages.failed)} page(s) could not be read.",
                           pages=[p["page"] for p in pages.failed]))
    unfilled = chunks.failed - chunks.filled_by_local
    if unfilled > 0:
        blocking_partial = True
        result.add(message("chunks_failed", f"{unfilled} of {chunks.total} part(s) of the document could not be processed.",
                           failed=unfilled, total=chunks.total))
    if chunks.filled_by_local:
        review = True
        result.add(message("chunks_filled_by_local",
                           f"{chunks.filled_by_local} part(s) were filled in by the local engine after the language model failed.",
                           count=chunks.filled_by_local))

    # ------------------------------------------------------------ per account
    metrics_accounts: list[dict[str, Any]] = []
    for account in accounts:
        table, validation, t = account.table, account.validation, account.validation.totals
        rows = table.rows
        checks = {c.key: c for c in validation.checks}
        failed_checks = [c for c in validation.checks if c.status == FAIL]
        mismatched = sum(1 for r in rows if r.balance_check == "mismatch")
        reconciled = sum(1 for r in rows if r.balance_check == "ok")
        prefix = {"account": account.key}

        if failed_checks:
            review = True
            result.add(message("checks_failed", "Failed: " + "; ".join(c.label for c in failed_checks) + ".",
                               checks=[c.key for c in failed_checks], **prefix))
        if validation.duplicates:
            review = True
            result.add(message("duplicates_found", f"{len(validation.duplicates)} possible duplicate row(s).",
                               count=len(validation.duplicates), **prefix))
        if table.unresolved:
            review = True
            result.add(message("unresolved_lines",
                               f"{len(table.unresolved)} line(s) look like transactions but were not extracted.",
                               count=len(table.unresolved), **prefix))
        if account.skipped_lines:
            review = True
            result.add(message("llm_skipped_lines",
                               f"The language model could not interpret {account.skipped_lines} transaction-like line(s).",
                               count=account.skipped_lines, **prefix))
        if account.unverified_values:
            review = True
            result.add(message("unverified_values",
                               f"{account.unverified_values} value(s) could not be found in the document text.",
                               count=account.unverified_values, **prefix))
        if t.unresolved_rows:
            review = True
            result.add(message("direction_unresolved", f"{t.unresolved_rows} amount(s) have no known direction.",
                               count=t.unresolved_rows, **prefix))
        missing = [n for n in REQUIRED_FIELDS if not (account.headers.get(n) and account.headers[n].best)]
        if "account_number" in missing and _identifier_found(account.headers):
            missing.remove("account_number")  # an IBAN identifies the account
        if missing:
            review = True
            result.add(message("required_fields_missing", "Not found: " + ", ".join(missing) + ".",
                               fields=missing, **prefix))

        proven = _proven(rows, checks, t.mode)
        if rows and not proven and not failed_checks:
            review = True
            result.add(message("not_provable",
                               "Nothing in the statement (running balance, opening/closing balance or totals) proves the rows.",
                               **prefix))
        if not rows:
            if table.empty_table_text:
                if t.opening_balance is not None and t.closing_balance is not None and t.opening_balance != t.closing_balance:
                    review = True
                    result.add(message("empty_but_balance_moved",
                                       "The statement reports no transactions, but its opening and closing balances differ.",
                                       **prefix))
            else:
                review = True
                result.add(message("no_transactions_found", "No transactions were found.", **prefix))

        metrics_accounts.append({
            "account": account.key,
            "currency": t.currency,
            "rows": len(rows),
            "rows_reconciled": reconciled,
            "rows_mismatched": mismatched,
            "duplicates": len(validation.duplicates),
            "unresolved_lines": len(table.unresolved),
            "skipped_lines": account.skipped_lines,
            "unverified_values": account.unverified_values,
            "unresolved_directions": t.unresolved_rows,
            "reconciliation_mode": t.mode,
            "order": t.order,
            "opening_closing": checks["opening_closing"].status if "opening_closing" in checks else None,
            "running_balance": checks["balance_chain"].status if "balance_chain" in checks else None,
            "printed_totals": checks["printed_totals"].status if "printed_totals" in checks else None,
            "declared_debits": _s(t.printed_debits),
            "extracted_debits": _s(t.total_debits),
            "declared_credits": _s(t.printed_credits),
            "extracted_credits": _s(t.total_credits),
            "proven": proven,
        })

    if multiple_accounts_suspected:
        review = True
        result.add(message("multiple_accounts_suspected",
                           "The document seems to contain more than one account; the local engine reads it as one."))
    if engine_disagreements:
        review = True
        result.add(message("engines_disagree", f"The two engines disagree on {engine_disagreements} item(s).",
                           count=engine_disagreements))
    if not accounts:
        result.status = FAILED
        result.add(message("nothing_extracted", "Nothing could be extracted."))
    elif blocking_partial:
        result.status = PARTIAL
    elif review:
        result.status = NEEDS_REVIEW

    result.metrics = {
        "pages_detected": pages.detected,
        "pages_processed": len(pages.processed),
        "pages_failed": len(pages.failed),
        "chunks_total": chunks.total,
        "chunks_failed": chunks.failed,
        "accounts": metrics_accounts,
    }
    return result


def failed(code: str, text: str, pages: PageReport | None = None) -> Completeness:
    result = Completeness(status=FAILED)
    result.add(message(code, text))
    pages = pages or PageReport()
    result.metrics = {"pages_detected": pages.detected, "pages_processed": len(pages.processed),
                      "pages_failed": len(pages.failed), "chunks_total": 0, "chunks_failed": 0, "accounts": []}
    return result


def _proven(rows, checks: dict, mode: str) -> bool:
    if not rows:
        return False
    chain = checks.get("balance_chain")
    if mode == "running_balance" and chain is not None and chain.status == PASS:
        return True
    oc = checks.get("opening_closing")
    if oc is not None and oc.status == PASS:
        return True
    totals = checks.get("printed_totals")
    return totals is not None and totals.status == PASS


def _s(value) -> str | None:
    return None if value is None else format(value, "f")
