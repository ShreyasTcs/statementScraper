"""Deterministic validation - arithmetic is the judge.

A statement can be proven in two ways, and most banks give at least one:

* **running balance**: every row prints the balance after it, so each row is
  checked against the one before (`previous + credit - debit == balance`) in
  the statement's *chronological* order - which is the reverse of the printed
  order on newest-first statements;
* **statement level**: opening balance, closing balance and (often) printed
  totals are given, so `opening + credits - debits == closing` and the totals
  must match.

Statement-level proof also *recovers* direction. When a single signed or
unsigned "Amount" column does not say debit or credit, the unique choice of
directions that makes the books balance is taken (subset-sum); if more than one
choice works the rows are left ambiguous and flagged rather than guessed.

Nothing here invents a transaction to make totals match: a mismatch stays a
failed check. All money is ``Decimal``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.logging import get_logger
from app.core.messages import Message, message, money_text
from app.rules import currency as currency_rules
from app.rules.iban import mod97_valid
from app.services.extraction_service import FieldExtraction
from app.services.table_service import BalanceRow, TableResult, TableRow

logger = get_logger(__name__)

VALID = "valid"
INVALID = "invalid"
NOT_FOUND = "not_found"
REVIEW = "review"

PASS, FAIL, WARN, SKIPPED, INFO = "pass", "fail", "warn", "skipped", "info"

OLDEST_FIRST, NEWEST_FIRST = "oldest_first", "newest_first"

_ZERO = Decimal(0)
_MAX_UNKNOWN = 34


@dataclass
class FieldValidation:
    status: str
    message: str | None = None
    code: str | None = None


@dataclass
class Check:
    key: str
    label: str
    status: str
    detail: str
    #: Stable code for ``detail`` and the values it mentions (for translation).
    detail_code: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def of(cls, key: str, label: str, status: str, msg: Message) -> "Check":
        return cls(key, label, status, msg.text, msg.code, msg.as_dict()["params"])


@dataclass
class StatementTotals:
    opening_balance: Decimal | None = None
    opening_source: str | None = None  # header | table_row | derived
    opening_date: date | None = None
    closing_balance: Decimal | None = None
    closing_source: str | None = None  # header | table_row | last_balance
    closing_date: date | None = None
    total_debits: Decimal = _ZERO
    total_credits: Decimal = _ZERO
    printed_debits: Decimal | None = None
    printed_credits: Decimal | None = None
    period_from: date | None = None
    period_to: date | None = None
    unresolved_rows: int = 0
    currency: str | None = None
    #: running_balance | statement_totals | none
    mode: str = "none"
    #: oldest_first | newest_first - the order the rows are printed in.
    order: str = OLDEST_FIRST


@dataclass
class StatementValidation:
    fields: dict[str, FieldValidation] = field(default_factory=dict)
    checks: list[Check] = field(default_factory=list)
    totals: StatementTotals = field(default_factory=StatementTotals)
    #: (row index, row index it duplicates)
    duplicates: list[tuple[int, int]] = field(default_factory=list)


class ValidationService:
    def validate(self, headers: dict[str, FieldExtraction], table: TableResult) -> StatementValidation:
        result = StatementValidation()
        for name, extraction in headers.items():
            result.fields[name] = self._validate_field(extraction)
        self._cross_check_period(headers, result)

        t = result.totals
        t.period_from, t.period_to = _date(headers.get("period_from")), _date(headers.get("period_to"))
        cur = headers.get("currency")
        t.currency = cur.best.normalized if cur and cur.best else None
        tolerance = Decimal(1) / (Decimal(10) ** max(currency_rules.precision(t.currency), 2)) * Decimal("1.1")

        rows = table.rows
        has_running = any(r.balance is not None for r in rows)
        t.order = self._detect_order(rows, tolerance, has_running)
        # Chronological sequence: what the arithmetic must follow.
        sequence = rows if t.order == OLDEST_FIRST else list(reversed(rows))

        self._resolve_balances(headers, table, t)
        if has_running:
            self._running_balance(sequence, t, tolerance)
        self._sum_rows(rows, t)
        if t.closing_balance is None:
            # No printed closing balance: the latest running balance is the closing one.
            last = next((r.balance for r in reversed(sequence) if r.balance is not None), None)
            if last is not None:
                t.closing_balance, t.closing_source = last, "last_balance"
        self._resolve_printed_totals(headers, table, t)
        self._statement_level(rows, t, tolerance)
        self._sum_rows(rows, t)  # directions may have been recovered
        t.mode = "running_balance" if has_running else (
            "statement_totals" if (t.opening_balance is not None and t.closing_balance is not None) or t.printed_debits is not None else "none"
        )
        result.duplicates = self._duplicates(rows)
        result.checks = self._checks(table, t, tolerance, has_running, result.duplicates)
        self._mark_rows(rows, result.checks, has_running)
        return result

    # --------------------------------------------------------------- header

    @staticmethod
    def _validate_field(extraction: FieldExtraction) -> FieldValidation:
        best = extraction.best
        if best is None:
            return FieldValidation(NOT_FOUND, "Not found in document", "field_not_found")
        kind = extraction.spec.value_type
        if kind == "iban" and not mod97_valid(best.normalized):
            return FieldValidation(INVALID, "IBAN checksum is wrong", "iban_checksum")
        if kind == "account_number" and not 6 <= len(best.normalized) <= 34:
            return FieldValidation(REVIEW, "Unusual account number length", "account_number_length")
        if kind == "money":
            try:
                Decimal(best.normalized)
            except InvalidOperation:
                return FieldValidation(INVALID, "Not a valid amount", "amount_invalid")
        if kind == "date" and _iso(best.normalized) is None:
            return FieldValidation(INVALID, "Not a valid date", "date_invalid")
        if kind == "currency" and best.normalized not in currency_rules.iso_codes():
            return FieldValidation(INVALID, "Not an ISO 4217 currency code", "currency_invalid")
        if best.strategy == "user":
            return FieldValidation(VALID, None, None)
        if best.strategy in ("derived", "domain"):
            return FieldValidation(REVIEW, "Inferred, not printed in the document", "field_inferred")
        if best.signals.get("verified") == 0.0:
            return FieldValidation(REVIEW, "Could not be found in the document text", "field_unverified")
        if best.meta.get("normalization_failed"):
            return FieldValidation(REVIEW, "Kept as printed; it could not be read as this kind of value", "field_unnormalized")
        return FieldValidation(VALID)

    @staticmethod
    def _cross_check_period(headers, result: StatementValidation) -> None:
        start, end = _date(headers.get("period_from")), _date(headers.get("period_to"))
        if start and end and start > end:
            for name in ("period_from", "period_to"):
                result.fields[name] = FieldValidation(INVALID, "Period start is after its end", "period_inverted")

    # ---------------------------------------------------------------- order

    @staticmethod
    def _detect_order(rows: list[TableRow], tolerance: Decimal, has_running: bool) -> str:
        """Newest-first statements print their latest transaction at the top.

        Dates decide when they can (several backward steps, almost no forward
        ones); otherwise the running balance decides - the order in which it
        reconciles with fewer breaks wins. Ties keep the printed order.
        """
        dated = [r.date for r in rows if r.date and not r.date_inherited]
        forward = sum(1 for a, b in zip(dated, dated[1:]) if b > a)
        backward = sum(1 for a, b in zip(dated, dated[1:]) if b < a)
        if backward >= 2 and backward > 3 * forward:
            return NEWEST_FIRST
        if forward >= 2 and forward > 3 * backward:
            return OLDEST_FIRST
        if has_running:
            if _dry_chain(list(reversed(rows)), tolerance) < _dry_chain(rows, tolerance):
                return NEWEST_FIRST
        return OLDEST_FIRST

    # -------------------------------------------------------------- balances

    @staticmethod
    def _resolve_balances(headers, table: TableResult, t: StatementTotals) -> None:
        printed_open, printed_close = _money(headers.get("opening_balance")), _money(headers.get("closing_balance"))
        rows = table.rows
        brs = sorted(table.balance_rows, key=lambda b: (b.page, b.bbox[1]))
        first_key = (rows[0].page, rows[0].bbox[1]) if rows else None
        last_key = (rows[-1].page, rows[-1].bbox[1]) if rows else None

        def above(b: BalanceRow) -> bool:
            return first_key is None or (b.page, b.bbox[1]) < first_key

        def below(b: BalanceRow) -> bool:
            return last_key is None or (b.page, b.bbox[1]) > last_key

        # On a newest-first statement the closing balance is printed above the rows.
        before, after = (above, below) if t.order == OLDEST_FIRST else (below, above)
        ordered = brs if t.order == OLDEST_FIRST else list(reversed(brs))
        opening_row = next((b for b in ordered if b.kind == "opening"), None) or next((b for b in ordered if before(b)), None)
        closing_row = next((b for b in reversed(ordered) if b.kind == "closing" and after(b)), None) or next(
            (b for b in reversed(ordered) if after(b) and b is not opening_row), None
        )
        if not rows and len(brs) >= 2:
            opening_row, closing_row = ordered[0], ordered[-1]

        if opening_row is not None:
            opening_row.kind = "opening"
        if closing_row is not None and closing_row is not opening_row:
            closing_row.kind = "closing"

        if printed_open is not None:
            t.opening_balance, t.opening_source = printed_open, "header"
            o = headers["opening_balance"].best
            t.opening_date = _iso(o.meta.get("as_of")) if o else None
        elif opening_row is not None:
            t.opening_balance, t.opening_source, t.opening_date = opening_row.amount, "table_row", opening_row.row_date
        if printed_close is not None:
            t.closing_balance, t.closing_source = printed_close, "header"
            c = headers["closing_balance"].best
            t.closing_date = _iso(c.meta.get("as_of")) if c else None
        elif closing_row is not None:
            t.closing_balance, t.closing_source, t.closing_date = closing_row.amount, "table_row", closing_row.row_date

    @staticmethod
    def _learn_unsigned_direction(sequence: list[TableRow], tolerance: Decimal) -> str | None:
        """In a signed Amount column, are unsigned values credits (the usual case) or debits?

        Learned from rows whose direction the running balance settles. Only an
        unbroken record of three or more is trusted.
        """
        counts = {"dr": 0, "cr": 0}
        for before, row in zip(sequence, sequence[1:]):
            if row.amount is None or row.debit is not None or row.credit is not None:
                continue
            if before.balance is None or row.balance is None:
                continue
            delta = row.balance - before.balance
            if abs(abs(delta) - row.amount) <= tolerance:
                counts["cr" if delta > 0 else "dr"] += 1
        if counts["cr"] >= 3 and counts["dr"] == 0:
            return "cr"
        if counts["dr"] >= 3 and counts["cr"] == 0:
            return "dr"
        return None

    @staticmethod
    def _running_balance(sequence: list[TableRow], t: StatementTotals, tolerance: Decimal) -> None:
        """Walk the rows oldest to newest, checking each balance against the previous one."""
        convention = ValidationService._learn_unsigned_direction(sequence, tolerance)
        if convention:
            for row in sequence:
                if row.amount is not None and row.debit is None and row.credit is None:
                    if convention == "cr":
                        row.credit = row.amount
                    else:
                        row.debit = row.amount
                    row.amount = None
                    row.direction_inferred = True
        previous = t.opening_balance
        for position, row in enumerate(sequence):
            if previous is None and position == 0 and row.balance is not None and (row.debit is not None or row.credit is not None):
                derived = row.balance - ((row.credit or _ZERO) - (row.debit or _ZERO))
                t.opening_balance, t.opening_source = derived, "derived"
                previous = row.balance
                row.balance_check = "unchecked"
                continue

            if row.amount is not None and row.debit is None and row.credit is None:
                _resolve_direction(row, previous, tolerance)

            expected = (row.credit or _ZERO) - (row.debit or _ZERO)
            if previous is not None and row.balance is not None and row.balance_check != "ok":
                delta = row.balance - previous
                if abs(delta - expected) <= tolerance:
                    row.balance_check = "ok"
                else:
                    row.balance_check = "mismatch"
                    if expected != 0 and abs(delta + expected) <= tolerance and row.source_engine == "groq" and not row.corrected:
                        # The column was the model's reading, not printed proof: the balance decides.
                        row.debit, row.credit = row.credit, row.debit
                        row.direction_inferred = True
                        row.balance_check = "ok"
                        row.evidence = {**(row.evidence or {}), "direction_corrected": True}
                    elif expected != 0 and abs(delta + expected) <= tolerance:
                        row.add_issue(message("debit_credit_swapped", "Debit and credit look swapped"))
                    else:
                        row.add_issue(message(
                            "balance_delta_mismatch",
                            f"Balance changed by {money_text(delta)} but the amount is {money_text(expected)}",
                            delta=delta, expected=expected,
                        ))

            if row.balance is not None:
                previous = row.balance  # trust the printed balance: one bad row must not cascade
            elif previous is not None:
                previous = previous + expected

    @staticmethod
    def _sum_rows(rows: list[TableRow], t: StatementTotals) -> None:
        t.total_debits = sum((r.debit for r in rows if r.debit is not None), _ZERO)
        t.total_credits = sum((r.credit for r in rows if r.credit is not None), _ZERO)
        t.unresolved_rows = sum(1 for r in rows if r.amount is not None and r.debit is None and r.credit is None)

    @staticmethod
    def _resolve_printed_totals(headers, table: TableResult, t: StatementTotals) -> None:
        t.printed_debits, t.printed_credits = _money(headers.get("total_debits")), _money(headers.get("total_credits"))
        for line in table.totals_rows:
            if t.printed_debits is None and line.debit is not None:
                t.printed_debits = line.debit
            if t.printed_credits is None and line.credit is not None:
                t.printed_credits = line.credit
            if line.amount is not None and line.direction == "dr" and t.printed_debits is None:
                t.printed_debits = line.amount
            if line.amount is not None and line.direction == "cr" and t.printed_credits is None:
                t.printed_credits = line.amount

    # -------------------------------------------------------- statement level

    @staticmethod
    def _statement_level(rows: list[TableRow], t: StatementTotals, tolerance: Decimal) -> None:
        """Recover unprinted directions from the books, only when the answer is unique."""
        unknown = [r for r in rows if r.amount is not None and r.debit is None and r.credit is None]
        if not unknown:
            return
        if len(unknown) > _MAX_UNKNOWN:
            for r in unknown:
                r.add_issue(message("direction_too_many", "Too many undirected amounts to infer direction"))
            return

        known_dr = sum((r.debit for r in rows if r.debit is not None), _ZERO)
        known_cr = sum((r.credit for r in rows if r.credit is not None), _ZERO)
        total_unknown = sum((r.amount for r in unknown), _ZERO)
        targets: list[Decimal] = []  # sum the *credit* subset must reach
        if t.opening_balance is not None and t.closing_balance is not None:
            net = t.closing_balance - t.opening_balance
            twice = net - known_cr + known_dr + total_unknown
            targets.append(twice / 2)
        if t.printed_credits is not None:
            targets.append(t.printed_credits - known_cr)
        if t.printed_debits is not None:
            targets.append(total_unknown - (t.printed_debits - known_dr))
        if not targets:
            for r in unknown:
                r.add_issue(message("direction_not_inferable", "Debit / credit direction is not printed and could not be inferred"))
            return

        scale = Decimal(1000)
        items = [int((r.amount * scale).to_integral_value()) for r in unknown]
        want = int((targets[0] * scale).to_integral_value())
        solutions = _subset_solutions(items, want, limit=2) if (targets[0] * scale) == want else []
        # Extra targets must agree, otherwise the books do not balance at all.
        for extra in targets[1:]:
            if abs(extra - targets[0]) > tolerance:
                solutions = []
        if len(solutions) == 1:
            chosen = solutions[0]
            for index, r in enumerate(unknown):
                if index in chosen:
                    r.credit = r.amount
                else:
                    r.debit = r.amount
                r.amount = None
                r.direction_inferred = True
        elif len(solutions) > 1:
            for r in unknown:
                r.add_issue(message("direction_ambiguous", "Debit / credit direction is ambiguous - several combinations balance the statement"))
        else:
            for r in unknown:
                r.add_issue(message("direction_no_solution", "No combination of debits and credits makes the statement balance"))

    # ------------------------------------------------------------ duplicates

    @staticmethod
    def _duplicates(rows: list[TableRow]) -> list[tuple[int, int]]:
        """Rows that repeat another row exactly - same date, amount, direction and text.

        A genuinely repeated transaction (two identical card payments) is told
        apart by the running balance: if both rows reconcile with different
        balances they are both real. Without that proof the later one is only
        *flagged*, never removed.
        """
        seen: dict[tuple, TableRow] = {}
        pairs: list[tuple[int, int]] = []
        for row in rows:
            amount = row.debit if row.debit is not None else row.credit if row.credit is not None else row.amount
            if amount is None:
                continue
            direction = "dr" if row.debit is not None else "cr" if row.credit is not None else "?"
            key = (row.account_key, row.date, amount, direction, _norm_text(row.description), _norm_text(row.reference))
            first = seen.get(key)
            if first is None:
                seen[key] = row
                continue
            proven_distinct = (
                first.balance is not None and row.balance is not None and first.balance != row.balance
                and first.balance_check == "ok" and row.balance_check == "ok"
            )
            if proven_distinct:
                continue
            pairs.append((row.index, first.index))
            row.add_issue(message("duplicate_candidate", f"May duplicate row {first.index}", other_row=first.index))
        return pairs

    # ---------------------------------------------------------------- checks

    def _checks(
        self, table: TableResult, t: StatementTotals, tol: Decimal, has_running: bool,
        duplicates: list[tuple[int, int]],
    ) -> list[Check]:
        rows = table.rows
        checks: list[Check] = []

        missing = [c.label for c in table.columns if c.required and not c.found]
        if table.found and not missing:
            checks.append(Check.of("columns", "Required table columns", PASS,
                                   message("columns_ok", "All required columns were recognised.")))
        elif table.found:
            checks.append(Check.of("columns", "Required table columns", WARN,
                                   message("columns_missing", f"Not recognised: {', '.join(missing)}.", columns=missing)))
        elif table.empty_table_text:
            checks.append(Check.of("columns", "Transaction table", INFO,
                                   message("statement_reports_empty", table.empty_table_text, text=table.empty_table_text)))
        elif not rows:
            checks.append(Check.of("columns", "Transaction table", FAIL,
                                   message("table_not_found", "No transaction table found.")))

        if has_running:
            ok = [r for r in rows if r.balance_check == "ok"]
            bad = [r for r in rows if r.balance_check == "mismatch"]
            checked = len(ok) + len(bad)
            if checked == 0:
                checks.append(Check.of("balance_chain", "Running balance reconciles", SKIPPED,
                                       message("chain_unchecked", "No row had a balance to check.")))
            else:
                text = f"{len(ok)} of {checked} checked rows reconcile with the running balance."
                bad_rows = [r.index for r in bad]
                if bad:
                    text += " Mismatch on row " + ", ".join(str(i) for i in bad_rows[:8]) + ("…" if len(bad) > 8 else "") + "."
                checks.append(Check.of("balance_chain", "Running balance reconciles", FAIL if bad else PASS,
                                       message("chain_result", text, ok=len(ok), checked=checked, mismatch_rows=bad_rows[:20])))

        checks.append(self._opening_closing(rows, t, tol))

        if t.unresolved_rows:
            checks.append(Check.of("direction", "Debit / credit direction", WARN, message(
                "direction_unresolved", f"{t.unresolved_rows} amount(s) have no printed direction and could not be inferred.",
                count=t.unresolved_rows)))
        elif any(r.direction_inferred for r in rows):
            n = sum(1 for r in rows if r.direction_inferred)
            checks.append(Check.of("direction", "Debit / credit direction", INFO, message(
                "direction_recovered",
                f"{n} row(s) had no printed direction; it was recovered because only one combination balances the statement.",
                count=n)))

        checks.append(self._printed_totals(t, tol))
        if rows:
            checks.append(self._period(rows, t))
            checks.append(self._date_order(rows, t.order))
            if duplicates:
                checks.append(Check.of("duplicates", "No duplicated rows", WARN, message(
                    "duplicates_found",
                    f"{len(duplicates)} row(s) repeat an earlier row exactly: "
                    + ", ".join(f"{a} = {b}" for a, b in duplicates[:6]) + ".",
                    count=len(duplicates), pairs=[list(p) for p in duplicates[:20]])))
            else:
                checks.append(Check.of("duplicates", "No duplicated rows", PASS,
                                       message("duplicates_none", "No row repeats another exactly.")))
            gaps = {
                "date": sum(1 for r in rows if r.date is None),
                "amount": sum(1 for r in rows if r.debit is None and r.credit is None and r.amount is None),
            }
            if has_running:
                gaps["balance"] = sum(1 for r in rows if r.balance is None)
            problems = [f"{n} without {k}" for k, n in gaps.items() if n]
            if problems:
                checks.append(Check.of("completeness", "Every row has the essentials", WARN, message(
                    "essentials_missing", "Rows: " + "; ".join(problems) + ".",
                    no_date=gaps["date"], no_amount=gaps["amount"], no_balance=gaps.get("balance", 0))))
            else:
                checks.append(Check.of("completeness", "Every row has the essentials", PASS,
                                       message("essentials_ok", f"All {len(rows)} rows are complete.", count=len(rows))))
        return checks

    @staticmethod
    def _opening_closing(rows: list[TableRow], t: StatementTotals, tol: Decimal) -> Check:
        key, label = "opening_closing", "Opening + credits − debits = closing"
        if t.opening_balance is None or t.closing_balance is None:
            return Check.of(key, label, SKIPPED, message("oc_unavailable", "Opening or closing balance is not available."))
        if t.unresolved_rows:
            return Check.of(key, label, SKIPPED,
                            message("oc_undirected", "Some amounts have no direction, so the sum cannot be formed."))
        expected = t.opening_balance + t.total_credits - t.total_debits
        diff = expected - t.closing_balance
        sources = f"opening from {t.opening_source.replace('_', ' ')}, closing from {t.closing_source.replace('_', ' ')}"
        common = dict(opening=t.opening_balance, credits=t.total_credits, debits=t.total_debits, expected=expected,
                      closing=t.closing_balance, opening_source=t.opening_source, closing_source=t.closing_source)
        if t.opening_source == "derived" and t.closing_source == "last_balance":
            return Check.of(key, label, INFO, message(
                "oc_from_table", f"Both balances were taken from the table itself ({sources}).", **common))
        if abs(diff) <= tol:
            return Check.of(key, label, PASS, message(
                "oc_pass",
                f"{money_text(t.opening_balance)} + {money_text(t.total_credits)} − {money_text(t.total_debits)} = "
                f"{money_text(expected)} ({sources}).", **common))
        hints = _hints(rows, diff, tol)
        hint_text = (" Possible cause: " + "; ".join(h["text"] for h in hints[:3]) + ".") if hints else ""
        return Check.of(key, label, FAIL, message(
            "oc_fail",
            f"Extracted rows give {money_text(expected)} but the statement shows {money_text(t.closing_balance)} "
            f"(off by {money_text(abs(diff))}; {sources}).{hint_text}",
            difference=abs(diff), hints=[{"row": h["row"], "kind": h["kind"]} for h in hints[:3]], **common))

    @staticmethod
    def _printed_totals(t: StatementTotals, tol: Decimal) -> Check:
        key, label = "printed_totals", "Totals match the statement's printed totals"
        parts: list[str] = []
        failed = False
        params: dict[str, Any] = {}
        for name, printed, computed in (("debits", t.printed_debits, t.total_debits), ("credits", t.printed_credits, t.total_credits)):
            if printed is None:
                continue
            params[f"printed_{name}"], params[f"extracted_{name}"] = printed, computed
            if abs(printed - computed) <= tol:
                parts.append(f"{name} {money_text(computed)} ✓")
                params[f"{name}_ok"] = True
            else:
                failed = True
                params[f"{name}_ok"] = False
                parts.append(f"{name}: printed {money_text(printed)}, extracted {money_text(computed)}")
        if not parts:
            return Check.of(key, label, SKIPPED, message("totals_none", "The statement does not print totals that could be found."))
        return Check.of(key, label, FAIL if failed else PASS, message("totals_result", "; ".join(parts) + ".", **params))

    @staticmethod
    def _period(rows: list[TableRow], t: StatementTotals) -> Check:
        key, label = "period", "Transactions fall inside the statement period"
        if t.period_from is None or t.period_to is None:
            return Check.of(key, label, SKIPPED, message("period_unknown", "The statement period was not found."))
        outside = [r for r in rows if r.date and not (t.period_from <= r.date <= t.period_to)]
        if outside:
            return Check.of(key, label, FAIL, message(
                "period_outside",
                f"{len(outside)} row(s) are dated outside {t.period_from} – {t.period_to} (e.g. row {outside[0].index}).",
                count=len(outside), start=t.period_from, end=t.period_to, row=outside[0].index))
        return Check.of(key, label, PASS, message(
            "period_ok", f"All dated rows lie within {t.period_from} – {t.period_to}.", start=t.period_from, end=t.period_to))

    @staticmethod
    def _date_order(rows: list[TableRow], order: str) -> Check:
        key, label = "date_order", "Dates are in order"
        dated = [r for r in rows if r.date and not r.date_inherited]
        if order == NEWEST_FIRST:
            breaks = [b.index for a, b in zip(dated, dated[1:]) if b.date > a.date]
            if not breaks:
                return Check.of(key, label, PASS, message(
                    "order_newest_first", "Rows are printed newest first; balances were checked oldest to newest."))
        else:
            breaks = [b.index for a, b in zip(dated, dated[1:]) if b.date < a.date]
            if not breaks:
                return Check.of(key, label, PASS, message("order_chronological", "Rows are in chronological order."))
        return Check.of(key, label, WARN, message(
            "order_mixed", f"Date order breaks at row {', '.join(map(str, breaks[:6]))}.", rows=breaks[:20]))

    @staticmethod
    def _mark_rows(rows: list[TableRow], checks: list[Check], has_running: bool) -> None:
        """Without per-row balances the statement-level proof covers every row."""
        if has_running:
            return
        proof = next((c for c in checks if c.key == "opening_closing"), None)
        totals = next((c for c in checks if c.key == "printed_totals"), None)
        passed = (proof is not None and proof.status == PASS) or (totals is not None and totals.status == PASS and (proof is None or proof.status in (SKIPPED, INFO)))
        failed = (proof is not None and proof.status == FAIL) or (totals is not None and totals.status == FAIL)
        for row in rows:
            if passed and not failed:
                row.balance_check = "ok"
            elif failed and row.balance_check != "ok":
                row.balance_check = "unchecked"


# --------------------------------------------------------------------------


def _norm_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold()).strip()


def _dry_chain(sequence: list[TableRow], tolerance: Decimal) -> int:
    """Breaks in the running balance if the rows happened in this order (no side effects)."""
    breaks = 0
    previous: Decimal | None = None
    for row in sequence:
        if row.balance is None:
            continue
        if previous is not None:
            delta = row.balance - previous
            if row.debit is not None or row.credit is not None:
                expected = (row.credit or _ZERO) - (row.debit or _ZERO)
                if abs(delta - expected) > tolerance:
                    breaks += 1
            elif row.amount is not None and abs(abs(delta) - row.amount) > tolerance:
                breaks += 1
        previous = row.balance
    return breaks


def _subset_solutions(items: list[int], target: int, limit: int = 2) -> list[set[int]]:
    """Index sets whose items add up to `target` (at most `limit` of them)."""
    if len(items) > 20:
        return _subset_meet_in_middle(items, target, limit)
    reach: list[dict[int, int]] = [{0: 1}]
    for value in items:
        previous = reach[-1]
        nxt = dict(previous)
        for s, count in previous.items():
            nxt[s + value] = min(nxt.get(s + value, 0) + count, limit)
        reach.append(nxt)
    if target not in reach[-1]:
        return []
    solutions: list[set[int]] = []

    def walk(i: int, remaining: int, chosen: set[int]) -> None:
        if len(solutions) >= limit:
            return
        if i == 0:
            if remaining == 0:
                solutions.append(set(chosen))
            return
        value = items[i - 1]
        if remaining in reach[i - 1]:
            walk(i - 1, remaining, chosen)
        if (remaining - value) in reach[i - 1]:
            chosen.add(i - 1)
            walk(i - 1, remaining - value, chosen)
            chosen.discard(i - 1)

    walk(len(items), target, set())
    return solutions


def _subset_meet_in_middle(items: list[int], target: int, limit: int) -> list[set[int]]:
    """Subset-sum for up to ~34 items: enumerate each half, then join on the sum."""
    half = len(items) // 2
    left, right = items[:half], items[half:]

    def sums(values: list[int]) -> dict[int, list[int]]:
        table: dict[int, list[int]] = {0: [0]}
        for index, value in enumerate(values):
            nxt = {s: list(masks) for s, masks in table.items()}
            for s, masks in table.items():
                bucket = nxt.setdefault(s + value, [])
                for mask in masks[:limit]:
                    if len(bucket) < limit:
                        bucket.append(mask | (1 << index))
            table = nxt
        return table

    left_sums, right_sums = sums(left), sums(right)
    found: list[set[int]] = []
    for s, lmasks in left_sums.items():
        for rmask in right_sums.get(target - s, []):
            for lmask in lmasks:
                found.append(
                    {i for i in range(len(left)) if lmask >> i & 1} | {half + i for i in range(len(right)) if rmask >> i & 1}
                )
                if len(found) >= limit:
                    return found
    return found


def _hints(rows: list[TableRow], diff: Decimal, tol: Decimal) -> list[dict[str, Any]]:
    """Point at the likeliest cause of a statement-level mismatch."""
    hints: list[dict[str, Any]] = []
    for r in rows:
        amount = r.debit if r.debit is not None else r.credit
        if amount is None:
            continue
        if abs(abs(diff) - 2 * amount) <= tol:
            hints.append({"row": r.index, "kind": "swapped", "text": f"row {r.index} may have its debit/credit swapped"})
        elif abs(abs(diff) - amount) <= tol:
            hints.append({"row": r.index, "kind": "missing_or_duplicated",
                          "text": f"row {r.index} ({money_text(amount)}) may be missing, duplicated or mis-read"})
    return hints


def _resolve_direction(row: TableRow, previous: Decimal | None, tolerance: Decimal) -> None:
    """A lone "Amount" column: let the running balance say which way money moved."""
    if previous is None or row.balance is None or row.amount is None:
        return
    delta = row.balance - previous
    if abs(abs(delta) - row.amount) <= tolerance:
        if delta > 0:
            row.credit = row.amount
        else:
            row.debit = row.amount
        row.amount = None
        row.direction_inferred = True
    else:
        row.add_issue(message("amount_balance_mismatch", "Amount does not match the change in balance"))


def _iso(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _money(extraction: FieldExtraction | None) -> Decimal | None:
    if extraction is None or extraction.best is None:
        return None
    try:
        return Decimal(extraction.best.normalized)
    except InvalidOperation:
        return None


def _date(extraction: FieldExtraction | None) -> date | None:
    if extraction is None or extraction.best is None:
        return None
    return _iso(extraction.best.normalized)


validation_service = ValidationService()
