"""Generate mock bank-statement PDFs plus ground truth.

    python tests/make_samples.py        # writes ../data/samples/

Three fictional banks, deliberately laid out differently so the extractor is
tested on structure, not on one template:

  statement_a_inr   split Withdrawal / Deposit columns, Indian digit grouping,
                    wrapped narration, header repeated on every page,
                    summary box (label above value), totals row, 3 pages
  statement_b_usd   one signed Amount column, MM/DD/YYYY, two-line column
                    headings, header printed only on page 1, several
                    "Label: value" pairs sharing a text line
  statement_c_aed   Amount + Dr/Cr indicator, "Cr" suffixed balances, an
                    "Opening Balance" table row, no IFSC / customer id /
                    opening-balance field (so "not found" is exercised)

Each PDF has a sibling .expected.json the test compares against.
"""
from __future__ import annotations

import json
import random
import textwrap
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import fitz  # PyMuPDF

OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "samples"

PAGE_W, PAGE_H = 595, 842
FONT, BOLD = "helv", "hebo"
SIZE = 7.5
LINE_H = 11.0
BOTTOM = 770


# --------------------------------------------------------------------------
# Money formatting
# --------------------------------------------------------------------------


def western(value: Decimal) -> str:
    return f"{value:,.2f}"


def indian(value: Decimal) -> str:
    whole, frac = f"{value:.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{whole}.{frac}"


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------


@dataclass
class Txn:
    day: date
    description: str
    reference: str
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal


def make_txns(
    rng: random.Random, start: date, end: date, count: int, opening: Decimal, templates: list[str]
) -> list[Txn]:
    span = (end - start).days
    days = sorted(start + timedelta(days=rng.randint(0, span)) for _ in range(count))
    balance = opening
    txns: list[Txn] = []
    for day in days:
        amount = Decimal(rng.randint(2_500, 4_500_000)) / 100
        credit = rng.random() < 0.3
        if not credit and amount >= balance:
            credit = True
        if credit:
            balance += amount
        else:
            balance -= amount
        ref = "".join(rng.choice("0123456789") for _ in range(rng.choice((6, 9, 12))))
        description = rng.choice(templates).format(ref=ref, n=rng.randint(100, 999))
        txns.append(
            Txn(day, description, ref if rng.random() < 0.8 else "", None if credit else amount, amount if credit else None, balance)
        )
    return txns


# --------------------------------------------------------------------------
# Drawing primitives
# --------------------------------------------------------------------------


class Canvas:
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

    def rect(self, x0: float, y0: float, x1: float, y1: float, fill=(0.9, 0.92, 0.96)) -> None:
        self.page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=None, fill=fill)

    def line(self, y: float, x0: float = 36, x1: float = 559) -> None:
        self.page.draw_line((x0, y), (x1, y), color=(0.6, 0.6, 0.6), width=0.4)

    def finish(self, path: Path, footer: str = "Page {i} of {n}") -> None:
        total = self.doc.page_count
        for index, page in enumerate(self.doc, start=1):
            label = footer.format(i=index, n=total)
            width = fitz.get_text_length(label, fontname=FONT, fontsize=7)
            page.insert_text(((PAGE_W - width) / 2, 820), label, fontname=FONT, fontsize=7)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(path)
        self.doc.close()


@dataclass
class Col:
    key: str
    heading: list[str]
    x: float
    right: bool = False


def draw_table(
    cv: Canvas,
    cols: list[Col],
    txns: list[Txn],
    y: float,
    *,
    cell,
    repeat_header: bool,
    wrap: int,
    continued_title: str | None = None,
    first_rows: list[dict] | None = None,
    top_of_next: float = 56.0,
) -> float:
    """Render header + rows, paginating. Returns the y after the last row."""

    def header(y0: float) -> float:
        lines = max(len(c.heading) for c in cols)
        cv.rect(34, y0 - 9, 561, y0 - 9 + 4 + 9.5 * lines)
        for col in cols:
            for n, piece in enumerate(col.heading):
                cv.text(col.x, y0 + 9.5 * n, piece, bold=True, right=col.right)
        return y0 + 9.5 * lines + 4

    y = header(y)
    for pre in first_rows or []:
        for col in cols:
            if col.key in pre:
                cv.text(col.x, y, pre[col.key], right=col.right)
        y += LINE_H

    for txn in txns:
        values = cell(txn)
        desc_lines = textwrap.wrap(values.get("description", ""), wrap) or [""]
        height = LINE_H * len(desc_lines)
        if y + height > BOTTOM:
            cv.new_page()
            y = top_of_next
            if continued_title:
                cv.text(36, y, continued_title, size=9, bold=True)
                y += 22
            if repeat_header:
                y = header(y)
        for col in cols:
            if col.key == "description":
                for n, piece in enumerate(desc_lines):
                    cv.text(col.x, y + LINE_H * n, piece, right=col.right)
            elif values.get(col.key):
                cv.text(col.x, y, values[col.key], right=col.right)
        y += height
    return y


def expected_rows(txns: list[Txn]) -> list[dict]:
    return [
        {
            "date": t.day.isoformat(),
            "debit": f"{t.debit:.2f}" if t.debit is not None else None,
            "credit": f"{t.credit:.2f}" if t.credit is not None else None,
            "balance": f"{t.balance:.2f}",
        }
        for t in txns
    ]


# --------------------------------------------------------------------------
# Statement A - INR, split columns
# --------------------------------------------------------------------------

A_TEMPLATES = [
    "UPI/{ref}/Swiggy Order food delivery",
    "NEFT-{ref}-ACME TECHNOLOGIES PVT LTD-SALARY",
    "ATM WDL Andheri West Mumbai {n}",
    "POS 4512XXXXXX1234 AMAZON PAY INDIA PRIVATE LIMITED",
    "ACH D- INDIAN CLEARING CORP-LOAN EMI {ref}",
    "IMPS-{ref}-RENT PAYMENT-SBIN0001234",
    "CHQ DEP {n} CLEARING",
    "UPI/{ref}/Uber India Systems",
    "INT.PD {n}",
]


def build_a(rng: random.Random) -> dict:
    opening = Decimal("125430.55")
    start, end = date(2025, 4, 1), date(2025, 4, 30)
    txns = make_txns(rng, start, end, 54, opening, A_TEMPLATES)
    closing = txns[-1].balance
    total_dr = sum((t.debit for t in txns if t.debit), Decimal(0))
    total_cr = sum((t.credit for t in txns if t.credit), Decimal(0))

    cv = Canvas()
    cv.text(36, 52, "MERIDIAN NATIONAL BANK LIMITED", size=16, bold=True)
    cv.text(36, 66, "Registered Office: 12 Marine Drive, Mumbai 400020", size=7)
    cv.text(36, 90, "STATEMENT OF ACCOUNT", size=10, bold=True)

    pairs_left = [
        ("Account Holder Name :", "RAHUL MOHAN SHARMA"),
        ("Customer ID :", "CIF48210937"),
        ("Branch :", "Andheri West"),
        ("Address :", "14 Lake View Road, Pune 411001"),
    ]
    pairs_right = [
        ("Account No :", "5010 4455 2201"),
        ("IFSC Code :", "MNBL0004521"),
        ("Account Type :", "Savings Account"),
        ("Statement Period :", "01/04/2025 to 30/04/2025"),
    ]
    y = 112
    for (l1, v1), (l2, v2) in zip(pairs_left, pairs_right):
        cv.text(36, y, l1)
        cv.text(135, y, v1, bold=True)
        cv.text(320, y, l2)
        cv.text(410, y, v2, bold=True)
        y += 13

    y += 8
    boxes = [
        ("Opening Balance", indian(opening)),
        ("Total Debits", indian(total_dr)),
        ("Total Credits", indian(total_cr)),
        ("Closing Balance", indian(closing)),
    ]
    cv.rect(34, y - 9, 561, y + 26, fill=(0.95, 0.95, 0.95))
    for n, (label, value) in enumerate(boxes):
        x = 40 + n * 130
        cv.text(x, y, label, size=7)
        cv.text(x, y + 14, value, bold=True)
    y += 48

    cols = [
        Col("date", ["Date"], 36),
        Col("description", ["Narration"], 82),
        Col("reference", ["Chq./Ref.No."], 256),
        Col("debit", ["Withdrawal Amt."], 392, right=True),
        Col("credit", ["Deposit Amt."], 462, right=True),
        Col("balance", ["Closing Balance"], 552, right=True),
    ]

    def cell(t: Txn) -> dict:
        return {
            "date": t.day.strftime("%d/%m/%Y"),
            "description": t.description,
            "reference": t.reference,
            "debit": indian(t.debit) if t.debit else "",
            "credit": indian(t.credit) if t.credit else "",
            "balance": indian(t.balance),
        }

    y = draw_table(cv, cols, txns, y, cell=cell, repeat_header=True, wrap=34)
    y += 6
    cv.line(y - 8)
    cv.text(82, y + 4, "Total", bold=True)
    cv.text(392, y + 4, indian(total_dr), bold=True, right=True)
    cv.text(462, y + 4, indian(total_cr), bold=True, right=True)
    cv.text(36, y + 34, "This is a computer generated statement and does not require a signature.", size=6.5)
    cv.text(36, y + 44, "Registered Office: 12 Marine Drive, Mumbai 400020", size=6.5)

    return {
        "name": "statement_a_inr",
        "canvas": cv,
        "expected": {
            "header": {
                "bank_name": "MERIDIAN NATIONAL BANK LIMITED",
                "account_holder": "RAHUL MOHAN SHARMA",
                "account_number": "501044552201",
                "ifsc": "MNBL0004521",
                "branch": "Andheri West",
                "customer_id": "CIF48210937",
                "account_type": "Savings Account",
                "period_from": "2025-04-01",
                "period_to": "2025-04-30",
                "opening_balance": f"{opening:.2f}",
                "closing_balance": f"{closing:.2f}",
                "total_debits": f"{total_dr:.2f}",
                "total_credits": f"{total_cr:.2f}",
            },
            "absent": ["currency"],
            "rows": expected_rows(txns),
        },
    }


# --------------------------------------------------------------------------
# Statement B - USD, one signed Amount column
# --------------------------------------------------------------------------

B_TEMPLATES = [
    "DEBIT CARD PURCHASE WHOLE FOODS MKT #{n}",
    "ACH CREDIT PAYROLL NORTHWIND LLC",
    "ZELLE PAYMENT TO ALEX MORGAN {ref}",
    "ONLINE TRANSFER FROM SAVINGS ...{n}",
    "CHECK #{n} DEPOSIT",
    "MONTHLY MAINTENANCE FEE",
    "ATM WITHDRAWAL 5TH AVE NEW YORK",
    "RECURRING PMT NETFLIX.COM",
]


def build_b(rng: random.Random) -> dict:
    opening = Decimal("4210.55")
    start, end = date(2025, 1, 1), date(2025, 1, 31)
    txns = make_txns(rng, start, end, 44, opening, B_TEMPLATES)
    closing = txns[-1].balance

    cv = Canvas()
    cv.text(36, 52, "Harbor Trust Bank, N.A.", size=15, bold=True)
    cv.text(36, 65, "200 Water Street, New York, NY 10038", size=7)
    cv.text(36, 92, "Checking Account Statement", size=10, bold=True)
    y = 112
    lines = [
        "Account Holder: JORDAN P. ELLIS",
        "Account Number: 000123456789        Routing Number: 021000021",
        "Statement Period: 01/01/2025 to 01/31/2025",
        "Previous Balance: $4,210.55",
        f"Ending Balance: ${western(closing)}",
    ]
    for line in lines:
        cv.text(36, y, line, size=8)
        y += 13
    y += 14

    cols = [
        Col("date", ["Posting", "Date"], 40),
        Col("description", ["Description"], 120),
        Col("amount", ["Transaction", "Amount ($)"], 470, right=True),
        Col("balance", ["Running Balance"], 555, right=True),
    ]

    def cell(t: Txn) -> dict:
        return {
            "date": t.day.strftime("%m/%d/%Y"),
            "description": t.description,
            "amount": f"-{western(t.debit)}" if t.debit else western(t.credit),
            "balance": western(t.balance),
        }

    draw_table(
        cv, cols, txns, y, cell=cell, repeat_header=False, wrap=52,
        continued_title="Checking Account Statement (continued)",
    )

    return {
        "name": "statement_b_usd",
        "canvas": cv,
        "expected": {
            "header": {
                "bank_name": "Harbor Trust Bank",
                "account_holder": "JORDAN P. ELLIS",
                "account_number": "000123456789",
                "ifsc": "021000021",
                "period_from": "2025-01-01",
                "period_to": "2025-01-31",
                "opening_balance": f"{opening:.2f}",
                "closing_balance": f"{closing:.2f}",
                "currency": "USD",
            },
            "absent": ["branch", "customer_id", "account_type", "total_debits", "total_credits"],
            "rows": expected_rows(txns),
        },
    }


# --------------------------------------------------------------------------
# Statement C - AED, Amount + Dr/Cr indicator
# --------------------------------------------------------------------------

C_TEMPLATES = [
    "CARREFOUR DEIRA CITY CENTRE DUBAI",
    "SALARY CREDIT GULF LOGISTICS FZE",
    "DEWA UTILITY PAYMENT {ref}",
    "ENOC STATION FUEL {n}",
    "TRANSFER TO F AL HASHIMI {ref}",
    "ATM CASH WITHDRAWAL DEIRA {n}",
    "CHEQUE DEPOSIT {n}",
    "ETISALAT BILL PAYMENT",
]


def build_c(rng: random.Random) -> dict:
    opening = Decimal("18750.00")
    start, end = date(2025, 2, 1), date(2025, 2, 28)
    txns = make_txns(rng, start, end, 32, opening, C_TEMPLATES)
    closing = txns[-1].balance

    cv = Canvas()
    cv.text(36, 52, "Gulf Crescent Bank PJSC", size=15, bold=True)
    cv.text(36, 66, "Account Statement", size=9)
    pairs = [
        ("Customer Name", "FATIMA AL-HASHIMI"),
        ("IBAN", "AE07 0331 2345 6789 0123 456"),
        ("Branch", "Deira Main Branch"),
        ("From:", "01-Feb-2025"),
        ("To:", "28-Feb-2025"),
    ]
    y = 96
    for label, value in pairs:
        cv.text(36, y, label)
        cv.text(150, y, value, bold=True)
        y += 13
    y += 16

    cols = [
        Col("date", ["Txn Date"], 36),
        Col("value_date", ["Value Date"], 100),
        Col("description", ["Particulars"], 165),
        Col("amount", ["Amount (AED)"], 430, right=True),
        Col("drcr", ["Dr/Cr"], 446),
        Col("balance", ["Balance"], 555, right=True),
    ]

    def cell(t: Txn) -> dict:
        return {
            "date": t.day.strftime("%d-%b-%Y"),
            "value_date": t.day.strftime("%d-%b-%Y"),
            "description": t.description,
            "amount": western(t.debit or t.credit),
            "drcr": "Dr" if t.debit else "Cr",
            "balance": f"{western(t.balance)} Cr",
        }

    opening_row = [{"description": "Opening Balance", "balance": f"{western(opening)} Cr"}]
    y = draw_table(
        cv, cols, txns, y, cell=cell, repeat_header=True, wrap=38,
        first_rows=opening_row,
    )
    y += 10
    cv.line(y - 6)
    cv.text(165, y + 4, "Closing Balance", bold=True)
    cv.text(555, y + 4, f"{western(closing)} Cr", bold=True, right=True)
    cv.text(36, y + 30, "Generated on 01-Mar-2025. Unless the constituent notifies the bank of any error within 30 days, "
                         "the statement is deemed correct.", size=6.5)

    return {
        "name": "statement_c_aed",
        "canvas": cv,
        "expected": {
            "header": {
                "bank_name": "Gulf Crescent Bank PJSC",
                "account_holder": "FATIMA AL-HASHIMI",
                "iban": "AE070331234567890123456",
                "branch": "Deira Main Branch",
                "period_from": "2025-02-01",
                "period_to": "2025-02-28",
                # Printed only as the first table row, not as a labelled field.
                "opening_balance": f"{opening:.2f}",
                "closing_balance": f"{closing:.2f}",
                "currency": "AED",
            },
            "absent": ["ifsc", "customer_id", "account_type", "total_debits", "total_credits"],
            "rows": expected_rows(txns),
        },
    }


# --------------------------------------------------------------------------


def main() -> None:
    for seed, builder in ((11, build_a), (23, build_b), (37, build_c)):
        sample = builder(random.Random(seed))
        path = OUT_DIR / f"{sample['name']}.pdf"
        sample["canvas"].finish(path)
        (OUT_DIR / f"{sample['name']}.expected.json").write_text(
            json.dumps(sample["expected"], indent=2), encoding="utf-8"
        )
        print(f"wrote {path.name}: {len(sample['expected']['rows'])} rows")


if __name__ == "__main__":
    main()
