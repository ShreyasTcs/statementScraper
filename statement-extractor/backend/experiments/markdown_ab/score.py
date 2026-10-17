"""Scoring against the existing ground truth, and the cross-pipeline discrepancy alignment.

The rules below were fixed before any comparison run and are the same for every pipeline and the reference:

* Transactions match ground truth one-to-one only when date, debit/credit direction and amount are all equal. Amounts
  are compared as exact decimals (45.0 equals 45.00; no tolerance). Each labelled row is used at most once; rows with the
  same key are paired in printed order.
* A row with only an "amount" and no debit/credit side has an unknown direction and cannot match a labelled row.
* Running balance is scored only on strictly matched rows whose ground truth carries a balance.
* Statement fields are scored only where the ground truth asserts them (names compared on letters and digits, money
  as exact decimals, everything else as the exact normalised string). Fields the ground truth asserts as absent must
  be absent.
* Descriptions are not labelled in the existing ground truth, so description accuracy is reported as not measurable.
* No composite score: every metric reports eligible, labelled, correct, incorrect and unscored counts.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

SCORING_VERSION = "poc-1"
ALIASES = {"ifsc": "routing"}
NAME_FIELDS = {"account_holder", "bank_name", "branch", "account_type"}
MONEY_FIELDS = {"opening_balance", "closing_balance", "total_debits", "total_credits"}

DEFINITIONS = {
    "transactions": "One-to-one match on date + direction + exact amount. Recall = matched / labelled rows; "
                    "precision = matched / extracted rows; F1 = harmonic mean.",
    "amount_direction": "One-to-one match on direction + exact amount, ignoring the date (share of labelled rows found).",
    "amount_only": "One-to-one match on exact amount, ignoring date and direction. The gap to the line above counts "
                   "debit/credit direction errors.",
    "date": "Among rows matched on direction + amount: the extracted date equals the labelled date.",
    "running_balance": "Among strictly matched rows whose ground truth has a balance: the extracted balance is exactly equal.",
    "opening_balance": "Opening balance of the first account equals the labelled opening balance exactly.",
    "closing_balance": "Closing balance of the first account equals the labelled closing balance exactly.",
    "statement_fields": "Each statement-level field the ground truth asserts (value present) or asserts as absent.",
    "description": "Not measurable: the existing ground truth has no description labels.",
    "duplicates": "Extracted rows whose date + direction + amount key occurs more often than in the ground truth (at least once).",
}


def truth_path(statement: Path) -> Path:
    return statement.with_suffix(".expected.json")


def load_truth(statement: Path) -> dict[str, Any] | None:
    path = truth_path(statement)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _norm_text(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def predicted_rows(result: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = []
    for account in (result or {}).get("accounts", []):
        for row in account.get("rows", []):
            debit, credit, amount = _dec(row.get("debit")), _dec(row.get("credit")), _dec(row.get("amount"))
            if debit is not None:
                direction, value = "debit", debit
            elif credit is not None:
                direction, value = "credit", credit
            elif amount is not None:
                direction, value = "unknown", amount
            else:
                direction, value = None, None
            rows.append({**row, "account": account.get("key"), "_dir": direction, "_amount": value,
                         "_balance": _dec(row.get("balance"))})
    return rows


def truth_rows(truth: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for i, row in enumerate(truth.get("rows", []), start=1):
        debit, credit = _dec(row.get("debit")), _dec(row.get("credit"))
        out.append({"index": i, "date": row.get("date"), "_dir": "debit" if debit is not None else "credit",
                    "_amount": debit if debit is not None else credit, "_balance": _dec(row.get("balance")),
                    "has_balance": "balance" in row and row.get("balance") is not None})
    return out


def _pairs(truth: list[dict], pred: list[dict], key) -> list[tuple[int, int]]:
    """One-to-one pairs of equal keys, earliest first on both sides."""
    free: dict[Any, list[int]] = defaultdict(list)
    for j, row in enumerate(pred):
        k = key(row)
        if k is not None:
            free[k].append(j)
    pairs = []
    for i, row in enumerate(truth):
        k = key(row)
        if k is not None and free.get(k):
            pairs.append((i, free[k].pop(0)))
    return pairs


def _strict(row: dict) -> tuple | None:
    if row.get("date") is None or row["_dir"] in (None, "unknown") or row["_amount"] is None:
        return None
    return (row["date"], row["_dir"], row["_amount"].normalize())


def _dir_amount(row: dict) -> tuple | None:
    if row["_dir"] in (None, "unknown") or row["_amount"] is None:
        return None
    return (row["_dir"], row["_amount"].normalize())


def _amount(row: dict) -> Decimal | None:
    return row["_amount"].normalize() if row["_amount"] is not None else None


def _metric(name: str, eligible: int, labelled: int, correct: int, **extra: Any) -> dict[str, Any]:
    incorrect = labelled - correct
    value = round(correct / labelled, 4) if labelled else None
    return {"metric": name, "definition": DEFINITIONS[name], "eligible": eligible, "labelled": labelled,
            "correct": correct, "incorrect": incorrect, "unscored": eligible - labelled,
            "coverage": round(labelled / eligible, 4) if eligible else None, "value": value, **extra}


def _header_value(result: dict[str, Any], name: str) -> str | None:
    accounts = result.get("accounts") or []
    if not accounts:
        return None
    field = (accounts[0].get("headers") or {}).get(name)
    return field.get("normalized") if field else None


def _same_field(name: str, got: str | None, want: Any) -> bool:
    if got is None:
        return False
    if name in NAME_FIELDS:
        return _norm_text(got) == _norm_text(str(want))
    if name in MONEY_FIELDS:
        a, b = _dec(got), _dec(want)
        return a is not None and b is not None and a == b
    return got == str(want)


def score(result: dict[str, Any] | None, truth: dict[str, Any] | None) -> dict[str, Any]:
    """Metrics for one pipeline on one statement. A failed pipeline is scored as extracting nothing."""
    if truth is None:
        return {"scoring_version": SCORING_VERSION, "ground_truth": False, "metrics": []}
    failed = result is None or result.get("status") != "completed"
    pred = [] if failed else predicted_rows(result)
    gt = truth_rows(truth)
    metrics: list[dict[str, Any]] = []

    strict = _pairs(gt, pred, _strict)
    matched_pred = {j for _, j in strict}
    recall = len(strict) / len(gt) if gt else None
    precision = len(strict) / len(pred) if pred else None          # undefined when nothing was extracted
    if not gt:
        f1 = None
    elif precision and recall:
        f1 = 2 * precision * recall / (precision + recall)
    else:
        f1 = 0.0
    metrics.append(_metric("transactions", len(gt), len(gt), len(strict), extracted=len(pred),
                           missing=len(gt) - len(strict), extra=len(pred) - len(strict),
                           precision=None if precision is None else round(precision, 4),
                           recall=None if recall is None else round(recall, 4), f1=None if f1 is None else round(f1, 4)))

    loose = _pairs(gt, pred, _dir_amount)
    metrics.append(_metric("amount_direction", len(gt), len(gt), len(loose)))
    by_amount = _pairs(gt, pred, _amount)
    metrics.append(_metric("amount_only", len(gt), len(gt), len(by_amount),
                           direction_errors=max(0, len(by_amount) - len(loose))))
    dated = [(i, j) for i, j in loose if gt[i].get("date") is not None]
    metrics.append(_metric("date", len(loose), len(dated), sum(1 for i, j in dated if pred[j].get("date") == gt[i]["date"])))

    with_balance = [(i, j) for i, j in strict if gt[i]["has_balance"]]
    metrics.append(_metric("running_balance", len(strict), len(with_balance),
                           sum(1 for i, j in with_balance if pred[j]["_balance"] is not None and pred[j]["_balance"] == gt[i]["_balance"])))

    totals = ((result or {}).get("accounts") or [{}])[0].get("totals") or {} if not failed else {}
    header = truth.get("header") or {}
    for kind in ("opening", "closing"):
        want = truth.get(kind) if truth.get(kind) is not None else header.get(f"{kind}_balance")
        got = _dec(totals.get(f"{kind}_balance"))
        labelled = 1 if want is not None else 0
        correct = 1 if labelled and got is not None and got == _dec(want) else 0
        metrics.append(_metric(f"{kind}_balance", 1, labelled, correct))

    field_results = []
    for name, want in header.items():
        name = ALIASES.get(name, name)
        got = None if failed else _header_value(result, name)
        field_results.append({"field": name, "asserted": "present", "ok": _same_field(name, got, want)})
    for name in truth.get("absent", []):
        name = ALIASES.get(name, name)
        got = None if failed else _header_value(result, name)
        field_results.append({"field": name, "asserted": "absent", "ok": got is None})
    metrics.append(_metric("statement_fields", len(field_results), len(field_results),
                           sum(1 for f in field_results if f["ok"]), fields=field_results))

    metrics.append(_metric("description", len(gt), 0, 0))

    gt_count: dict[Any, int] = defaultdict(int)
    for row in gt:
        gt_count[_strict(row)] += 1
    pred_count: dict[Any, int] = defaultdict(int)
    for row in pred:
        k = _strict(row)
        if k is not None:
            pred_count[k] += 1
    duplicates = sum(max(0, n - max(gt_count.get(k, 0), 1)) for k, n in pred_count.items())
    metrics.append({"metric": "duplicates", "definition": DEFINITIONS["duplicates"], "eligible": len(pred),
                    "labelled": len(pred), "correct": None, "incorrect": duplicates, "unscored": 0,
                    "coverage": 1.0 if pred else None, "value": round(duplicates / len(pred), 4) if pred else None})
    return {"scoring_version": SCORING_VERSION, "ground_truth": True, "pipeline_failed": failed,
            "matched_rows": sorted(matched_pred), "metrics": metrics}


# --------------------------------------------------------------------------
# Discrepancy explorer: align the pipelines with each other (never with ground truth)
# --------------------------------------------------------------------------


def _desc(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def align(results: dict[str, dict[str, Any] | None], gt_matched: dict[str, set[int]]) -> dict[str, Any]:
    """Groups rows of all completed pipelines by date + direction + exact amount. Equal counts pair rows in order;
    unequal counts are shown as ambiguous rather than forced. Rows without a usable key are listed as incomplete."""
    names = [n for n, r in results.items() if r is not None and r.get("status") == "completed"]
    keyed: dict[str, dict[Any, list[dict]]] = {n: defaultdict(list) for n in names}
    incomplete: dict[str, list[dict]] = {n: [] for n in names}
    for name in names:
        for position, row in enumerate(predicted_rows(results[name])):
            row["_position"] = position
            row["_gt"] = position in gt_matched.get(name, set())
            k = _strict(row)
            if k is None:
                incomplete[name].append(row)
            else:
                keyed[name][k].append(row)
    keys = sorted({k for n in names for k in keyed[n]}, key=lambda k: (k[0], k[1], k[2]))
    groups = []
    summary: dict[str, int] = defaultdict(int)
    for k in keys:
        counts = {n: len(keyed[n].get(k, [])) for n in names}
        present = [n for n in names if counts[n]]
        if len(present) == len(names) and len(set(counts.values())) == 1:
            status = "matched"
        elif len(set(counts[n] for n in present)) == 1 and counts[present[0]] >= 1 and len(present) < len(names):
            status = "only in " + " + ".join(present)
        else:
            status = "ambiguous (different counts)"
        differences = []
        if status == "matched" or status.startswith("only in"):
            for position in range(counts[present[0]]):
                rows = {n: keyed[n][k][position] for n in present}
                for field in ("description", "balance", "value_date", "reference"):
                    values = {n: (_desc(r.get(field)) if field == "description" else r.get(field)) for n, r in rows.items()}
                    if len(set(map(str, values.values()))) > 1:
                        differences.append({"pair": position, "field": field,
                                            "values": {n: rows[n].get(field) for n in present}})
        summary[status] += 1
        groups.append({"date": k[0], "direction": k[1], "amount": format(k[2], "f"), "status": status,
                       "counts": counts, "differences": differences,
                       "rows": {n: [{"page": r.get("page"), "description": r.get("description"), "balance": r.get("balance"),
                                     "lines": r.get("source_lines"), "in_ground_truth": r["_gt"]}
                                    for r in keyed[n].get(k, [])] for n in names}})
    incomplete_out = {n: [{"page": r.get("page"), "date": r.get("date"), "debit": r.get("debit"), "credit": r.get("credit"),
                           "amount": r.get("amount"), "description": r.get("description"), "lines": r.get("source_lines"),
                           "problem": "no date" if r.get("date") is None else ("direction unknown" if r["_dir"] == "unknown" else "no amount")}
                          for r in rows] for n, rows in incomplete.items()}
    return {"pipelines": names, "groups": groups, "incomplete": incomplete_out, "summary": dict(summary),
            "rule": "Rows are grouped by date + debit/credit direction + exact amount. Equal counts in every pipeline "
                    "pair rows in printed order; different counts are shown as ambiguous instead of forcing a match."}
