"""Synthetic bank statements for the automated tests.

Everything here is invented (bank, holder, numbers). Each PDF is written to a
temporary directory together with its ground truth, so tests never depend on
real statements or on generated files lying around in ``data/``.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import fitz

PAGE_W, PAGE_H = 595, 842
FONT, BOLD = "helv", "hebo"
SIZE, LINE_H, BOTTOM = 7.5, 11.0, 770


@dataclass
class Txn:
    day: date
    description: str
    debit: Decimal | None = None
    credit: Decimal | None = None
    balance: Decimal | None = None


@dataclass
class Spec:
    bank: str = "Northwind Savings Bank"
    holder: str = "Jane Example"
    account: str = "1234 5678 90"
    start: date = date(2026, 8, 1)
    end: date = date(2026, 8, 31)
    opening: Decimal = Decimal("1500.00")
    currency: str = "EUR"
    running_balance: bool = True
    newest_first: bool = False
    show_totals: bool = True
    #: Printed closing balance; None = the true closing balance.
    closing_printed: Decimal | None = None
    #: Index of a transaction to print twice (a duplicated line).
    duplicate_index: int | None = None
    #: Index of a transaction to leave off the printed statement (an omission).
    omit_index: int | None = None
    #: Free text lines printed under the header (e.g. an injection attempt).
    extra_lines: list[str] = field(default_factory=list)
    #: Print "No transactions in this period" instead of a table body.
    empty: bool = False


def money(value: Decimal) -> str:
    sign = "-" if value < 0 else ""
    whole, _, frac = f"{abs(value):.2f}".partition(".")
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{sign}{','.join(groups)}.{frac}"


def default_txns(spec: Spec, count: int = 12) -> list[Txn]:
    descriptions = [
        "Salary August ACME Industries", "Card payment Grocery Market 0042", "Rent transfer Lakeside Properties",
        "Electricity bill City Power", "Transfer from savings", "Card payment Coffee Corner",
        "Insurance premium Shield Mutual", "Refund Online Store order 7781", "ATM withdrawal Main Street",
        "Mobile phone Connect Telecom", "Interest credit", "Restaurant Blue Harbour",
    ]
    amounts = [
        ("cr", "3200.00"), ("dr", "84.35"), ("dr", "1150.00"), ("dr", "96.20"), ("cr", "250.00"), ("dr", "4.80"),
        ("dr", "61.75"), ("cr", "39.99"), ("dr", "200.00"), ("dr", "29.90"), ("cr", "1.12"), ("dr", "57.40"),
    ]
    balance = spec.opening
    out: list[Txn] = []
    for i in range(count):
        kind, value = amounts[i % len(amounts)]
        amount = Decimal(value)
        balance = balance + amount if kind == "cr" else balance - amount
        out.append(Txn(spec.start + timedelta(days=1 + i * 2), descriptions[i % len(descriptions)],
                       amount if kind == "dr" else None, amount if kind == "cr" else None, balance))
    return out


class _Canvas:
    def __init__(self) -> None:
        self.doc = fitz.open()
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)

    def text(self, x: float, y: float, s: str, size: float = SIZE, bold: bool = False, right: bool = False) -> None:
        font = BOLD if bold else FONT
        if right:
            x -= fitz.get_text_length(s, fontname=font, fontsize=size)
        self.page.insert_text((x, y), s, fontname=font, fontsize=size)

    def save(self, path: Path) -> None:
        total = self.doc.page_count
        for index, page in enumerate(self.doc, start=1):
            label = f"Page {index} of {total}"
            width = fitz.get_text_length(label, fontname=FONT, fontsize=7)
            page.insert_text(((PAGE_W - width) / 2, 820), label, fontname=FONT, fontsize=7)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(path)
        self.doc.close()


def build(path: Path, spec: Spec | None = None, txns: list[Txn] | None = None) -> dict:
    """Write a statement PDF and return its ground truth."""
    spec = spec or Spec()
    txns = [] if spec.empty else (txns if txns is not None else default_txns(spec))
    printed = list(txns)
    if spec.omit_index is not None:
        printed.pop(spec.omit_index)
    if spec.duplicate_index is not None:
        printed.insert(spec.duplicate_index + 1, printed[spec.duplicate_index])
    if spec.newest_first:
        printed = list(reversed(printed))

    closing = txns[-1].balance if txns and txns[-1].balance is not None else spec.opening
    debits = sum((t.debit for t in txns if t.debit), Decimal(0))
    credits = sum((t.credit for t in txns if t.credit), Decimal(0))

    cv = _Canvas()
    cv.text(36, 52, spec.bank, size=15, bold=True)
    cv.text(36, 66, "Account Statement", size=9)
    pairs = [("Account Holder", spec.holder), ("Account Number", spec.account),
             ("Statement Period", f"{spec.start:%d/%m/%Y} to {spec.end:%d/%m/%Y}"),
             ("Opening Balance", f"{money(spec.opening)} {spec.currency}"),
             ("Closing Balance", f"{money(spec.closing_printed if spec.closing_printed is not None else closing)} {spec.currency}")]
    if spec.show_totals:
        pairs += [("Total Debits", f"{money(debits)} {spec.currency}"), ("Total Credits", f"{money(credits)} {spec.currency}")]
    y = 100.0
    for label, value in pairs:
        cv.text(36, y, f"{label}: {value}", size=8)
        y += 13
    for line in spec.extra_lines:
        cv.text(36, y, line, size=8)
        y += 13
    y += 18

    cols = [("date", "Date", 36, False), ("desc", "Description", 102, False), ("debit", "Debit", 398, True),
            ("credit", "Credit", 464, True)]
    if spec.running_balance:
        cols.append(("balance", "Balance", 556, True))

    def header_row(y0: float) -> float:
        for _, heading, x, right in cols:
            cv.text(x, y0, heading, bold=True, right=right)
        return y0 + 13.5

    y = header_row(y)
    if spec.empty:
        cv.text(102, y, "No transactions in this period")
        y += LINE_H
    for t in printed:
        lines = textwrap.wrap(t.description, 38, break_long_words=False) or [""]
        height = LINE_H * len(lines)
        if y + height > BOTTOM:
            cv.new_page()
            y = header_row(62.0)
        cv.text(36, y, f"{t.day:%d/%m/%Y}")
        for n, piece in enumerate(lines):
            cv.text(102, y + LINE_H * n, piece)
        if t.debit:
            cv.text(398, y, money(t.debit), right=True)
        if t.credit:
            cv.text(464, y, money(t.credit), right=True)
        if spec.running_balance and t.balance is not None:
            cv.text(556, y, money(t.balance), right=True)
        y += height
    cv.save(path)

    return {
        "header": {"account_holder": spec.holder, "account_number": spec.account.replace(" ", ""),
                   "period_from": spec.start.isoformat(), "period_to": spec.end.isoformat(),
                   "opening_balance": f"{spec.opening:.2f}", "closing_balance": f"{closing:.2f}"},
        "rows": [{"date": t.day.isoformat(), "description": t.description,
                  "debit": f"{t.debit:.2f}" if t.debit else None, "credit": f"{t.credit:.2f}" if t.credit else None,
                  "balance": f"{t.balance:.2f}" if t.balance is not None else None} for t in txns],
        "opening": f"{spec.opening:.2f}",
        "closing": f"{closing:.2f}",
        "debits": f"{debits:.2f}",
        "credits": f"{credits:.2f}",
    }
