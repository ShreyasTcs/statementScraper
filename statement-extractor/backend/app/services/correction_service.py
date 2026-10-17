"""Reviewer corrections.

Rules:

* the value extraction produced is never lost: a transaction's extracted values
  are frozen in ``Transaction.original`` and a field's in ``original_value`` /
  ``original_normalized`` before its first correction;
* every change - edit, confirmation, new row, deletion, restore, dismissal -
  writes a ``Correction`` record (who, when, attribute, old and new value);
* deletion is soft: a deleted row is kept, excluded from validation and
  exports, and can be restored;
* input is normalised with the statement's own conventions (decimal mark, date
  order), so a reviewer can type "1.234,56" on a German statement. ISO dates and
  plain decimals ("1234.56") are always accepted.

After any change the caller revalidates, so checks and completeness are never
stale.
"""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.models import Correction, ExtractionField, StatementExtraction, Transaction
from app.rules import currency as currency_rules
from app.rules.dates import parse_date
from app.rules.iban import find_bic, find_ibans
from app.rules.numbers import format_decimal, parse_amount
from app.rules.statement import HEADER_FIELD_BY_NAME, HeaderField
from app.schemas.schemas import FieldUpdate, TransactionCreate, TransactionUpdate
from app.services.pipeline import conventions_from, summary_accounts
from app.services.table_service import Conventions

TXN_ATTRIBUTES = ("date", "value_date", "description", "reference", "debit", "credit", "amount", "balance", "currency")
_COLUMN = {"date": "txn_date", "value_date": "value_date", "description": "description", "reference": "reference",
           "debit": "debit", "credit": "credit", "amount": "amount", "balance": "balance", "currency": "currency"}
_PLAIN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
#: Extraction issues that a correction of these attributes resolves.
_RESOLVED_BY = {
    "date": {"date_unreadable"},
    "debit": {"amount_unreadable", "amount_ocr_repaired"},
    "credit": {"amount_unreadable", "amount_ocr_repaired"},
    "amount": {"amount_unreadable", "amount_ocr_repaired"},
    "balance": {"amount_unreadable", "amount_ocr_repaired"},
    "currency": {"currency_differs"},
}


class CorrectionError(Exception):
    def __init__(self, code: str, message: str, **params: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.params = params


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------


def normalize_money(value: str, conv: Conventions, *, signed: bool, attribute: str) -> str:
    text = value.strip()
    if _PLAIN.match(text):
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise CorrectionError("invalid_amount", f"'{value}' is not an amount.", attribute=attribute) from None
    else:
        parsed = parse_amount(text, conv.number)
        if parsed is None:
            raise CorrectionError("invalid_amount", f"'{value}' is not an amount.", attribute=attribute)
        number = parsed.signed if signed else parsed.value
    if not signed and number < 0:
        raise CorrectionError("amount_negative", "Debit and credit amounts are entered without a sign.", attribute=attribute)
    if abs(number) >= Decimal("1e15") or -number.as_tuple().exponent > 4:  # type: ignore[operator]
        raise CorrectionError("invalid_amount", f"'{value}' is not a plausible amount.", attribute=attribute)
    return format_decimal(number) or "0.00"


def normalize_date(value: str, conv: Conventions, attribute: str) -> str:
    text = value.strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        pass
    parsed = parse_date(text, conv.order, conv.year, whole=True)
    if parsed is None:
        raise CorrectionError("invalid_date", f"'{value}' is not a date.", attribute=attribute)
    return parsed.isoformat()


def normalize_currency(value: str) -> str:
    code = value.strip().upper()
    if code not in currency_rules.iso_codes():
        raise CorrectionError("invalid_currency", f"'{value}' is not an ISO 4217 currency code.", attribute="currency")
    return code


def _text(value: str, limit: int, attribute: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) > limit:
        raise CorrectionError("value_too_long", f"At most {limit} characters.", attribute=attribute, limit=limit)
    return text


def normalize_field(spec: HeaderField, value: str, conv: Conventions) -> tuple[str, str]:
    """(display, normalized) for a statement detail typed by a reviewer."""
    kind = spec.value_type
    raw = value.strip()
    if kind == "money":
        signed = spec.name not in ("total_debits", "total_credits")
        return raw, normalize_money(raw, conv, signed=signed, attribute=spec.name)
    if kind == "date":
        return raw, normalize_date(raw, conv, spec.name)
    if kind == "currency":
        code = normalize_currency(raw)
        return code, code
    if kind == "iban":
        compact = re.sub(r"[\s-]", "", raw).upper()
        found = find_ibans(compact)
        if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{8,30}", compact):
            raise CorrectionError("invalid_iban", "This does not look like an IBAN.", attribute=spec.name)
        return raw, found[0].value if found else compact
    if kind == "bic":
        code = find_bic(raw.upper().replace(" ", ""))
        if not code:
            raise CorrectionError("invalid_bic", "A BIC has 8 or 11 letters and digits.", attribute=spec.name)
        return code, code
    if kind == "account_number":
        compact = re.sub(r"[\s.\-/]", "", raw)
        if not re.fullmatch(r"[0-9A-Za-z*•xX]{4,34}", compact):
            raise CorrectionError("invalid_account_number", "Use 4-34 letters or digits.", attribute=spec.name)
        return raw, compact
    text = _text(raw, 200 if kind == "text" else 64, spec.name)
    return text, text


def _txn_value(attribute: str, value: str | None, conv: Conventions) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if attribute in ("date", "value_date"):
        return normalize_date(value, conv, attribute)
    if attribute in ("debit", "credit", "amount"):
        return normalize_money(value, conv, signed=False, attribute=attribute)
    if attribute == "balance":
        return normalize_money(value, conv, signed=True, attribute=attribute)
    if attribute == "currency":
        return normalize_currency(value)
    if attribute == "reference":
        return _text(value, 128, attribute)
    return _text(value, 1000, attribute)


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------


def _record(db: Session, extraction: StatementExtraction, entity_type: str, entity_id: str, action: str,
            user: str, attribute: str | None = None, old: Any = None, new: Any = None) -> None:
    db.add(Correction(
        extraction_id=extraction.id, entity_type=entity_type, entity_id=entity_id, action=action,
        attribute=attribute, old_value=None if old is None else str(old), new_value=None if new is None else str(new),
        user=user,
    ))


def _snapshot(t: Transaction) -> dict[str, Any]:
    return {"date": t.txn_date, "value_date": t.value_date, "description": t.description, "reference": t.reference,
            "debit": t.debit, "credit": t.credit, "amount": t.amount, "balance": t.balance, "currency": t.currency,
            "row_index": t.row_index}


# --------------------------------------------------------------------------
# Fields
# --------------------------------------------------------------------------


def update_field(db: Session, extraction: StatementExtraction, f: ExtractionField, body: FieldUpdate, user: str) -> None:
    conv = conventions_from(extraction.summary or {})
    spec = HEADER_FIELD_BY_NAME[f.field_name]
    if not f.corrected:
        f.original_value, f.original_normalized = f.value, f.normalized_value

    if body.confirm and body.value is None:
        if f.value is None:
            raise CorrectionError("nothing_to_confirm", "There is no value to confirm.")
        _record(db, extraction, "field", f.id, "confirm", user, "value", f.normalized_value, f.normalized_value)
    else:
        if body.value is None or not body.value.strip():
            display = normalized = None
        else:
            display, normalized = normalize_field(spec, body.value, conv)
        if normalized == f.normalized_value and display == f.value:
            return
        _record(db, extraction, "field", f.id, "update", user, "value", f.normalized_value, normalized)
        f.value, f.normalized_value = display, normalized
    f.corrected = True
    f.provenance = "user"
    evidence = dict(f.evidence or {})
    evidence.update({"engine": "user", "confirmed_by": user})
    f.evidence = evidence
    extraction.corrected = True


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------


def _drop_issues(t: Transaction, codes: set[str], attributes: set[str]) -> None:
    kept: list[dict] = []
    for entry in t.issue_codes or []:
        code = entry.get("code")
        if code in codes:
            if code == "amount_unreadable" and (entry.get("params") or {}).get("column") not in attributes:
                kept.append(entry)
            continue
        if code == "llm_unverified":
            remaining = [a for a in (entry.get("params") or {}).get("attributes", []) if a not in attributes]
            if remaining:
                kept.append({**entry, "params": {"attributes": remaining},
                             "text": "Not found in the cited document text: " + ", ".join(remaining)})
            continue
        kept.append(entry)
    t.issue_codes = kept
    t.issues = [e.get("text", "") for e in kept]


def update_transaction(db: Session, extraction: StatementExtraction, t: Transaction, body: TransactionUpdate,
                       user: str) -> None:
    if t.deleted:
        raise CorrectionError("row_deleted", "Restore the row before editing it.")
    conv = conventions_from(extraction.summary or {})
    provided = body.model_fields_set
    changes: dict[str, str | None] = {}
    for attribute in TXN_ATTRIBUTES:
        if attribute not in provided:
            continue
        new = _txn_value(attribute, getattr(body, attribute), conv)
        if new != getattr(t, _COLUMN[attribute]):
            changes[attribute] = new

    final = {a: changes.get(a, getattr(t, _COLUMN[a])) for a in ("debit", "credit", "amount")}
    if ("debit" in changes or "credit" in changes) and changes.get("amount") is None and final["amount"] is not None:
        if final["debit"] is not None or final["credit"] is not None:
            changes["amount"] = None  # the direction is now known
            final["amount"] = None
    if final["debit"] is not None and final["credit"] is not None:
        raise CorrectionError("both_directions", "A row is either a debit or a credit, not both.")
    if "amount" in changes and changes["amount"] is not None and (final["debit"] is not None or final["credit"] is not None):
        raise CorrectionError("both_directions", "Clear debit and credit before entering an amount without direction.")

    touched = bool(changes) or body.confirmed or body.not_duplicate is not None
    if not touched:
        return
    if t.original is None:
        t.original = _snapshot(t)

    evidence = dict(t.evidence or {})
    verified = dict(evidence.get("verified") or {}) if isinstance(evidence.get("verified"), dict) else {}
    for attribute, new in changes.items():
        _record(db, extraction, "transaction", t.id, "update", user, attribute, getattr(t, _COLUMN[attribute]), new)
        setattr(t, _COLUMN[attribute], new)
        if attribute in verified:
            verified[attribute] = True
        if attribute == "date":
            t.date_inherited = False
    if changes:
        if {"debit", "credit", "amount"} & set(changes):
            t.direction_inferred = False
        resolved = set().union(*(_RESOLVED_BY.get(a, set()) for a in changes)) if changes else set()
        _drop_issues(t, resolved, set(changes))
    if body.confirmed:
        _record(db, extraction, "transaction", t.id, "confirm", user)
        verified = {k: True for k in verified}
        evidence["confirmed_by"] = user
        _drop_issues(t, {"llm_unverified", "llm_citation_invalid", "account_unassigned", "amount_ocr_repaired"},
                     set(TXN_ATTRIBUTES))
    if body.not_duplicate is not None:
        _record(db, extraction, "transaction", t.id, "not_duplicate" if body.not_duplicate else "maybe_duplicate", user,
                "not_duplicate", evidence.get("not_duplicate"), body.not_duplicate)
        evidence["not_duplicate"] = bool(body.not_duplicate)
    if verified:
        evidence["verified"] = verified
    t.evidence = evidence
    flag_modified(t, "evidence")
    t.corrected = True
    extraction.corrected = True


def create_transaction(db: Session, extraction: StatementExtraction, body: TransactionCreate, user: str) -> Transaction:
    conv = conventions_from(extraction.summary or {})
    values = {a: _txn_value(a, getattr(body, a), conv) for a in TXN_ATTRIBUTES}
    if values["debit"] is not None and values["credit"] is not None:
        raise CorrectionError("both_directions", "A row is either a debit or a credit, not both.")
    if values["amount"] is not None and (values["debit"] is not None or values["credit"] is not None):
        values["amount"] = None
    if values["debit"] is None and values["credit"] is None and values["amount"] is None:
        raise CorrectionError("row_needs_amount", "Enter a debit, a credit or an amount.")
    if values["date"] is None and not values["description"]:
        raise CorrectionError("row_needs_date_or_description", "Enter a date or a description.")
    keys = {a["key"] for a in summary_accounts(extraction.summary or {})} | {"A1"}
    account = body.account if body.account in keys else None
    if account is None:
        raise CorrectionError("unknown_account", "This statement has no such account.", account=body.account)

    rows = list(db.scalars(select(Transaction).where(Transaction.extraction_id == extraction.id)
                           .order_by(Transaction.row_index)))
    same = [r for r in rows if (r.account_key or "A1") == account]
    live = [r for r in same if not r.deleted]
    after = body.after_index or 0
    # after_index is the displayed position among live rows; translate it to the storage key.
    anchor = live[after - 1] if 0 < after <= len(live) else (live[-1] if after > len(live) and live else None)
    position = (anchor.row_index if anchor else 0) + 1
    db.execute(update(Transaction)
               .where(Transaction.extraction_id == extraction.id, Transaction.row_index >= position)
               .values(row_index=Transaction.row_index + 1))
    page = body.page or (anchor.page if anchor else (live[0].page if live else 1))
    t = Transaction(
        extraction_id=extraction.id, account_key=account, row_index=position, page=page, bbox=None,
        txn_date=values["date"], value_date=values["value_date"], description=values["description"],
        reference=values["reference"], debit=values["debit"], credit=values["credit"], amount=values["amount"],
        balance=values["balance"], currency=values["currency"], source_engine="user", provenance="user",
        corrected=True, deleted=False, balance_check="unchecked", status="needs_review",
        evidence={"engine": "user", "confirmed_by": user}, issue_codes=[], issues=[], original=None,
    )
    db.add(t)
    db.flush()
    _record(db, extraction, "transaction", t.id, "create", user, None, None,
            json.dumps({k: v for k, v in values.items() if v is not None}, ensure_ascii=False))
    extraction.corrected = True
    return t


def delete_transaction(db: Session, extraction: StatementExtraction, t: Transaction, user: str) -> None:
    if t.deleted:
        return
    if t.original is None and t.source_engine != "user":
        t.original = _snapshot(t)
    t.deleted = True
    _record(db, extraction, "transaction", t.id, "delete", user)
    extraction.corrected = True


def restore_transaction(db: Session, extraction: StatementExtraction, t: Transaction, user: str) -> None:
    if not t.deleted:
        return
    t.deleted = False
    _record(db, extraction, "transaction", t.id, "restore", user)


# --------------------------------------------------------------------------
# Unresolved lines
# --------------------------------------------------------------------------


def set_unresolved_dismissed(db: Session, extraction: StatementExtraction, account: str, index: int,
                             dismissed: bool, user: str) -> None:
    summary = dict(extraction.summary or {})
    accounts = summary_accounts(summary)
    target = next((a for a in accounts if a["key"] == account), None)
    if target is None or not 0 <= index < len(target.get("unresolved", [])):
        raise CorrectionError("unresolved_not_found", "No such unresolved line.")
    lines = [dict(u) for u in target["unresolved"]]
    if bool(lines[index].get("dismissed")) == dismissed:
        return
    lines[index]["dismissed"] = dismissed
    target = {**target, "unresolved": lines}
    summary["accounts"] = [target if a["key"] == account else a for a in accounts]
    extraction.summary = summary
    flag_modified(extraction, "summary")
    _record(db, extraction, "unresolved", f"{account}:{index}", "dismiss" if dismissed else "undismiss", user)
    extraction.corrected = True
