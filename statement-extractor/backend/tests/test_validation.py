"""Local engine + deterministic validation + completeness, on synthetic PDFs."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import statement_factory as sf

from app.core.messages import message
from app.services import pipeline
from app.services.pdf_service import TextReader


def analyze(path):
    return pipeline.analyze(TextReader().read(path, document_id=path.stem))


def codes(analysis):
    return [r["code"] for r in analysis.completeness.reason_codes]


def failing(analysis):
    return {c.key for c in analysis.validation.checks if c.status == "fail"}


def test_clean_statement_is_complete_and_matches_ground_truth(statement):
    path, truth = statement(sf.Spec())
    a = analyze(path)
    assert a.completeness.status == "complete"
    assert len(a.table.rows) == len(truth["rows"])
    for row, want in zip(a.table.rows, truth["rows"]):
        assert row.date.isoformat() == want["date"]
        assert (row.debit and f"{row.debit:.2f}") == want["debit"]
        assert (row.credit and f"{row.credit:.2f}") == want["credit"]
        assert f"{row.balance:.2f}" == want["balance"]
        assert row.balance_check == "ok"
    assert a.validation.totals.order == "oldest_first"
    assert not failing(a)


def test_newest_first_statement_reconciles_in_reverse(statement):
    path, truth = statement(sf.Spec(newest_first=True))
    a = analyze(path)
    t = a.validation.totals
    assert t.order == "newest_first"
    assert f"{t.opening_balance:.2f}" == truth["opening"]
    assert f"{t.closing_balance:.2f}" == truth["closing"]
    assert not failing(a)
    assert a.completeness.status == "complete"
    order_check = next(c for c in a.validation.checks if c.key == "date_order")
    assert order_check.detail_code == "order_newest_first"


def test_duplicated_row_is_flagged_not_removed(statement):
    path, truth = statement(sf.Spec(duplicate_index=3))
    a = analyze(path)
    assert len(a.table.rows) == len(truth["rows"]) + 1  # kept, never silently dropped
    assert a.validation.duplicates
    later, first = a.validation.duplicates[0]
    row = next(r for r in a.table.rows if r.index == later)
    assert any(e["code"] == "duplicate_candidate" and e["params"]["other_row"] == first for e in row.issue_codes)
    assert a.completeness.status == "needs_review"
    assert "duplicates_found" in codes(a)


def test_repeated_identical_payments_proven_by_balance_are_not_duplicates(statement):
    spec = sf.Spec()
    txns = sf.default_txns(spec)
    # Two genuine identical coffee purchases on the same day, each with its own balance.
    twin = sf.Txn(txns[5].day, txns[5].description, txns[5].debit, None, txns[5].balance - txns[5].debit)
    txns.insert(6, twin)
    balance = twin.balance
    for t in txns[7:]:
        balance = balance + (t.credit or 0) - (t.debit or 0)
        t.balance = balance
    path, _ = statement(spec, txns)
    a = analyze(path)
    assert not a.validation.duplicates
    assert a.completeness.status == "complete"


def test_omitted_row_breaks_reconciliation(statement):
    path, _ = statement(sf.Spec(omit_index=4))
    a = analyze(path)
    assert {"balance_chain", "opening_closing"} <= failing(a)
    assert a.completeness.status == "needs_review"


def test_statement_without_running_balance_is_proven_by_totals(statement):
    path, _ = statement(sf.Spec(running_balance=False))
    a = analyze(path)
    assert a.validation.totals.mode == "statement_totals"
    assert a.completeness.status == "complete"


def test_wrong_printed_closing_balance_fails(statement):
    path, _ = statement(sf.Spec(running_balance=False, closing_printed=Decimal("9999.99")))
    a = analyze(path)
    assert "opening_closing" in failing(a)
    check = next(c for c in a.validation.checks if c.key == "opening_closing")
    assert check.detail_code == "oc_fail"
    assert check.params["closing"] == "9999.99"
    assert a.completeness.status == "needs_review"


def test_empty_statement_is_complete_only_when_balances_agree(statement):
    path, _ = statement(sf.Spec(empty=True))
    a = analyze(path)
    assert not a.table.rows
    assert a.table.empty_table_text
    assert a.completeness.status == "complete"

    path, _ = statement(sf.Spec(empty=True, closing_printed=Decimal("1600.00")))
    a = analyze(path)
    assert a.completeness.status == "needs_review"
    assert "empty_but_balance_moved" in codes(a)


def test_multi_page_statement_keeps_every_row(statement):
    spec = sf.Spec(end=date(2026, 12, 31))  # 60 rows two days apart fit inside the period
    path, truth = statement(spec, sf.default_txns(spec, count=60))
    a = analyze(path)
    assert len({r.page for r in a.table.rows}) >= 2
    assert len(a.table.rows) == 60
    assert a.completeness.status == "complete", [r["code"] for r in a.completeness.reason_codes]


def test_messages_carry_codes_and_exact_params():
    msg = message("balance_delta_mismatch", "text", delta=Decimal("-12.50"), text="kept as a param")
    data = msg.as_dict()
    assert data["code"] == "balance_delta_mismatch"
    assert data["params"] == {"delta": "-12.50", "text": "kept as a param"}


def test_page_failure_makes_result_partial(statement):
    path, _ = statement(sf.Spec())
    content = TextReader().read(path, document_id="x")
    pages = pipeline.PageReport(detected=2, processed=[1], failed=[{"page": 2, "code": "ocr_failed"}])
    a = pipeline.analyze(content, pages=pages)
    assert a.completeness.status == "partial"
    assert "pages_failed" in codes(a)


def test_confidence_reflects_reconciliation(statement):
    path, _ = statement(sf.Spec(omit_index=4))
    a = analyze(path)
    mismatched = [r for r in a.table.rows if r.balance_check == "mismatch"]
    assert mismatched and all(r.confidence <= 0.55 for r in mismatched)
    ok = [r for r in a.table.rows if r.balance_check == "ok"]
    assert ok and all(r.confidence > 0.9 for r in ok)
