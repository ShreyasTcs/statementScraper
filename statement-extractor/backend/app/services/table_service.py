"""Transaction-table reconstruction, word by word.

    1. conventions   number format, day/month order and year, inferred per document
    2. header        a run of heading lines -> columns (any language, any line count)
    3. assignment    every word goes to a column: amounts by x-distance to the
                     numeric headings, everything else by left edge
    4. rows          a row starts at a date (or, with no date column, at an amount);
                     the lines under it are wrapped narration
    5. non-rows      opening / closing balances, totals, page numbers, legal text
                     are recognised and kept apart, never turned into transactions
    6. parse         dates, amounts (sign before or after, Dr/Cr, S/H), direction

Splitting at word level matters: banks fuse cells into one text run
("14.09.26 14.09.26 Transfer domestic", "ENTRATE DESCRIZIONE"), and the
fused text is only separable by where each word sits.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.core.logging import get_logger
from app.core.messages import Message, add_message, message
from app.rules import lexicon
from app.rules.dates import find_dates, infer_order, is_date_token, parse_date
from app.rules.numbers import (
    DEFAULT_CONVENTION,
    NumberConvention,
    ParsedAmount,
    infer_convention,
    looks_like_money,
    parse_amount,
)
from app.services.document_model import Page, StructuredDocument

logger = get_logger(__name__)

BBox = tuple[float, float, float, float]

_COLUMN_LABELS = {
    "date": "Date", "value_date": "Value date", "description": "Description",
    "reference": "Reference", "debit": "Debit", "credit": "Credit", "amount": "Amount",
    "balance": "Balance", "drcr": "Dr / Cr",
}
_REQUIRED_COLUMNS = ("description",)
_CURRENCY_ONLY = re.compile(r"^(?:[A-Z]{3}|[€$£¥₹¤�]|rs\.?|kr\.?)$", re.I)
_INDICATOR_ONLY = re.compile(r"^(?:cr|dr|db|d|c|s|h|[+\-−])$", re.I)


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass
class Conventions:
    number: NumberConvention = DEFAULT_CONVENTION
    order: str = "dmy"
    year: int | None = None
    ocr: bool = False
    zero_decimals: bool = False


@dataclass
class TableRow:
    index: int
    page: int
    bbox: BBox
    raw: dict[str, str]
    date: date | None = None
    value_date: date | None = None
    date_inherited: bool = False
    description: str = ""
    reference: str = ""
    debit: Decimal | None = None
    credit: Decimal | None = None
    #: A single "Amount" column whose direction is not printed.
    amount: Decimal | None = None
    balance: Decimal | None = None
    fit: float = 1.0
    issues: list[str] = field(default_factory=list)
    extras: dict[str, str] = field(default_factory=dict)
    ocr_repaired: bool = False
    #: Machine-readable twins of ``issues`` / ``reasons`` (code, params, text).
    issue_codes: list[dict] = field(default_factory=list)
    reason_codes: list[dict] = field(default_factory=list)

    #: Which account / currency the row belongs to (one statement can hold several).
    account_key: str = "A1"
    currency: str | None = None
    #: local | groq | user
    source_engine: str = "local"
    #: explicit | inferred | derived | unknown | user
    provenance: str = "explicit"
    #: Source line ids, page, excerpt and verification result.
    evidence: dict | None = None
    #: Database id when the row was rebuilt from storage (revalidation).
    row_id: str | None = None
    corrected: bool = False

    # Filled in by validation / confidence.
    direction_inferred: bool = False
    balance_check: str = "unchecked"  # ok | mismatch | unchecked
    confidence: float | None = None
    status: str = "needs_review"
    signals: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def add_issue(self, msg: Message) -> None:
        add_message(self.issues, self.issue_codes, msg)

    def add_reason(self, msg: Message) -> None:
        add_message(self.reasons, self.reason_codes, msg)


@dataclass
class BalanceRow:
    """An opening / closing / carried-forward balance printed inside the table."""

    kind: str  # opening | closing | balance
    page: int
    text: str
    bbox: BBox
    amount: Decimal
    row_date: date | None = None
    repaired: bool = False


@dataclass
class TotalsRow:
    page: int
    text: str
    bbox: BBox
    debit: Decimal | None = None
    credit: Decimal | None = None
    amount: Decimal | None = None
    direction: str | None = None


@dataclass
class UnresolvedLine:
    """A line that looks like it could hold a transaction but became no row."""

    page: int
    text: str
    bbox: BBox
    #: after_table_end | no_open_row | detached_line
    reason: str
    amounts: list[str] = field(default_factory=list)


@dataclass
class DetectedColumn:
    key: str
    label: str
    required: bool
    found: bool
    printed_as: str | None = None


@dataclass
class TableResult:
    rows: list[TableRow] = field(default_factory=list)
    balance_rows: list[BalanceRow] = field(default_factory=list)
    totals_rows: list[TotalsRow] = field(default_factory=list)
    columns: list[DetectedColumn] = field(default_factory=list)
    claimed_block_ids: set[str] = field(default_factory=set)
    header_pages: list[int] = field(default_factory=list)
    conventions: Conventions = field(default_factory=Conventions)
    notes: list[str] = field(default_factory=list)
    #: True when columns were inferred from content because no heading was recognised.
    inferred_columns: bool = False
    empty_table_text: str | None = None
    note_codes: list[dict] = field(default_factory=list)
    #: Transaction-like lines that were not turned into rows - never dropped silently.
    unresolved: list[UnresolvedLine] = field(default_factory=list)
    #: Pages the table was scanned on.
    pages_scanned: list[int] = field(default_factory=list)

    def add_note(self, msg: Message) -> None:
        add_message(self.notes, self.note_codes, msg)

    @property
    def found(self) -> bool:
        return bool(self.header_pages) or self.inferred_columns

    @property
    def has_balance_column(self) -> bool:
        return any(c.key == "balance" and c.found for c in self.columns)

    @property
    def dayfirst(self) -> bool:
        return self.conventions.order == "dmy"


# --------------------------------------------------------------------------
# Internal structures
# --------------------------------------------------------------------------


@dataclass
class _Word:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    block: str
    conf: float | None = None
    source: str = "pdf_text"

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1.0)


@dataclass
class _Line:
    words: list[_Word]

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)

    @property
    def height(self) -> float:
        return max(w.h for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in sorted(self.words, key=lambda w: w.x0))


@dataclass
class _Column:
    key: str
    concept: str
    printed: str
    x0: float
    x1: float
    numeric: bool
    #: False for a heading nobody could read (OCR noise, an unknown language).
    recognised: bool = True

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class _Layout:
    columns: list[_Column]
    #: left-edge intervals for text columns
    text_bounds: list[tuple[float, float, _Column]] = field(default_factory=list)

    @property
    def numeric(self) -> list[_Column]:
        return [c for c in self.columns if c.numeric]

    @property
    def date_columns(self) -> list[_Column]:
        return [c for c in self.columns if c.concept in ("date", "value_date")]

    def has(self, key: str) -> bool:
        return any(c.key == key for c in self.columns)


@dataclass
class _Row:
    page: int
    cells: dict[str, list[_Word]] = field(default_factory=dict)
    fits: list[float] = field(default_factory=list)
    inherited_date: bool = False
    pending_date: date | None = None

    def add(self, key: str, word: _Word, fit: float) -> None:
        self.cells.setdefault(key, []).append(word)
        self.fits.append(fit)

    def words(self) -> list[_Word]:
        return [w for ws in self.cells.values() for w in ws]

    def text(self, key: str) -> str:
        ws = sorted(self.cells.get(key, []), key=lambda w: (round(w.y0 / 3), w.x0))
        return " ".join(w.text for w in ws).strip()

    def has_movement(self, conv: Conventions) -> bool:
        return any(
            looks_like_money(self.text(k), conv.number, conv.zero_decimals)
            for k in ("debit", "credit", "amount")
            if k in self.cells
        )


# --------------------------------------------------------------------------
# Conventions
# --------------------------------------------------------------------------


def infer_conventions(doc: StructuredDocument, ocr: bool = False) -> Conventions:
    """Decide number format, date order and year from the whole document."""
    texts: list[str] = []
    for page in doc.pages:
        for block in page.blocks:
            for word, _, _ in block.word_spans():
                texts.append(word)
    amounts = [t for t in texts if re.search(r"\d[.,]\d", t) and not is_date_token(t)]
    runs = [block.text for _, block in doc.iter_blocks() if re.search(r"\d{1,3}[ \u00a0\u202f]\d{3}", block.text)]
    number = infer_convention(amounts + runs)
    order = infer_order(texts)

    years: Counter[int] = Counter()
    for _, block in doc.iter_blocks():
        for value, _, _ in find_dates(block.text, order):
            years[value.year] += 1
    year = years.most_common(1)[0][0] if years else None

    # Currencies with no minor unit (JPY, KRW, ...) print "12,345" with no decimals at all.
    # One kind of separator only ("120,407", "190'099"); 1,234.567 mixes two and is a 3-decimal amount.
    def bare(t: str) -> str:
        return re.sub(r"^[-+(]|[-+)]$", "", t)

    grouped = [t for t in texts if re.fullmatch(r"\d{1,3}(?:[.,'\u2019]\d{2,3})+", bare(t))
               and len(set(re.findall(r"[.,'\u2019]", t))) == 1 and not is_date_token(bare(t))
               and not re.fullmatch(r"\d{1,3}[.,]\d{2}", bare(t))]
    spaced = sum(1 for _, block in doc.iter_blocks() for m in re.finditer(r"(?<![\d.,])\d{1,3}(?: \d{3})+(?![\d.,])", block.text))
    decimals = [t for t in texts if re.fullmatch(r"\d[\d.,']*[.,]\d{2}", t) and not is_date_token(t)]
    zero_decimals = len(grouped) + spaced >= 6 and len(decimals) <= max(1, (len(grouped) + spaced) // 20)
    return Conventions(number=number, order=order, year=year, ocr=ocr, zero_decimals=zero_decimals)


# --------------------------------------------------------------------------
# Extractor
# --------------------------------------------------------------------------


class TableExtractor:
    # ------------------------------------------------------------------ API

    def extract(self, doc: StructuredDocument, conv: Conventions | None = None) -> TableResult:
        ocr = any(b.source == "ocr" for _, b in doc.iter_blocks())
        conv = conv or infer_conventions(doc, ocr)
        conv.ocr = ocr
        result = TableResult(conventions=conv)

        layout: _Layout | None = None
        printed: dict[str, str] = {}
        raw_rows: list[_Row] = []

        for page in doc.pages:
            lines = _prepare_lines(page, conv)
            found = self._find_header(page, lines, conv)
            start = 0
            if found is not None:
                layout, last_index, header_words = found
                printed.update({c.key: c.printed for c in layout.columns})
                result.header_pages.append(page.page_number)
                result.claimed_block_ids.update(w.block for w in header_words)
                start = last_index + 1
            if layout is None:
                continue
            completed = self._complete_layout(layout, lines[start:], conv, page)
            if completed is not layout:
                layout = completed
                printed.update({c.key: c.printed for c in layout.columns})
            result.pages_scanned.append(page.page_number)
            self._scan_page(page, lines[start:], layout, conv, raw_rows, result)

        if not result.found:
            self._infer_without_header(doc, conv, raw_rows, result, printed)

        if not result.found:
            result.add_note(message("table_not_found", "No transaction table header was recognised."))
            result.columns = self._column_report({}, False)
            self._note_empty(doc, result)
            return result

        result.columns = self._column_report(printed, True)
        result.rows = self._parse(raw_rows, conv)
        for raw in raw_rows:
            result.claimed_block_ids.update(w.block for w in raw.words())
        self._note_empty(doc, result)
        return result

    @staticmethod
    def _note_empty(doc: StructuredDocument, result: TableResult) -> None:
        """Banks print 'No transactions for this period' instead of a table body."""
        if result.rows:
            return
        for _, block in doc.iter_blocks():
            if re.search(
                r"no (?:transactions?|movements?|entries)|nessun (?:movimento|operazione)|keine (?:buchungen|umsatze)|"
                r"aucun (?:mouvement|operation)|sin movimientos|ingen (?:transaksjoner|bevegelser)|ei tapahtumia",
                lexicon.fold(block.text),
            ):
                result.empty_table_text = block.text.strip()
                result.add_note(message("statement_reports_empty", f"The statement reports: “{block.text.strip()}”.",
                                        text=block.text.strip()))
                return

    # --------------------------------------------------------------- header

    def _find_header(self, page: Page, lines: list[_Line], conv: Conventions):
        i = 0
        while i < len(lines):
            run = self._clean_run(lines, i, conv)
            if len(run) == 0:
                i += 1
                continue
            attempt = self._header_from_run(page, lines, run, conv)
            if attempt is not None:
                return attempt
            i = run[-1] + 1
        return None

    def _clean_run(self, lines: list[_Line], start: int, conv: Conventions) -> list[int]:
        """Consecutive close-set lines holding no dates or amounts."""
        run: list[int] = []
        j = start
        while j < len(lines) and len(run) < 5:
            line = lines[j]
            if self._is_data_like(line, conv):
                break
            # "Opening balance" / "Total" rows are table content, never headings.
            if lexicon.find("row", " ".join(w.text for w in line.words if not looks_like_money(w.text, conv.number, conv.zero_decimals)), ocr=conv.ocr):
                break
            if run and line.y0 - lines[run[-1]].y1 > 1.7 * line.height:
                break
            run.append(j)
            j += 1
        return run

    @staticmethod
    def _is_data_like(line: _Line, conv: Conventions) -> bool:
        for w in line.words:
            if is_date_token(w.text, conv.order):
                return True
            if looks_like_money(w.text, conv.number, conv.zero_decimals) and not _CURRENCY_ONLY.match(w.text):
                return True
        return False

    def _header_from_run(self, page: Page, lines: list[_Line], run: list[int], conv: Conventions):
        # Drop leading lines that are not headings (long prose above the table).
        for skip in range(len(run)):
            window = run[skip:]
            columns = self._columns_from_lines([lines[k] for k in window], page, conv)
            if columns is not None:
                words = [w for k in window for w in self._heading_words(lines[k], conv)]
                # The body starts after the last line that actually held a heading.
                contributing = [k for k in window if self._heading_words(lines[k], conv)]
                return self._build_layout(columns, page), contributing[-1], words
        return None

    @staticmethod
    def _heading_words(line: _Line, conv: Conventions) -> list[_Word]:
        """Words of the line that can be part of a column heading.

        A text run counts if it names a column or is short ("Trans.", "date");
        prose ("No transactions for this period", "Interest, fees and charges")
        does not, so it cannot corrupt the stacked headings above it.
        """
        by_block: dict[str, list[_Word]] = {}
        for w in line.words:
            by_block.setdefault(w.block, []).append(w)
        keep: list[_Word] = []
        for ws in by_block.values():
            text = " ".join(w.text for w in ws)
            concept = lexicon.classify("column", text, ocr=conv.ocr)
            named = concept not in (None, "ignore")
            if len(ws) <= 4 and (named or len(ws) <= 2 and len(text) <= 14):
                keep.extend(ws)
        return keep

    def _columns_from_lines(self, window: list[_Line], page: Page, conv: Conventions) -> list[_Column] | None:
        heading_words = [w for line in window for w in self._heading_words(line, conv)]
        cells = _split_fused(heading_words, conv.ocr)
        stacks = _stack(cells)
        columns: list[_Column] = []
        used: set[str] = set()
        extra = 0
        for stack in stacks:
            text = " ".join(c.text for c in sorted(stack, key=lambda c: c.y0))
            concept = lexicon.classify("column", text, ocr=conv.ocr)
            x0, x1 = min(c.x0 for c in stack), max(c.x1 for c in stack)
            if concept is None or lexicon.tokens(text)[:1] in (["total"], ["totale"], ["summe"]):
                if len(text) <= 40 and not is_date_token(text):
                    columns.append(_Column(f"extra_{extra}", "ignore", text, x0, x1, False, recognised=False))
                    extra += 1
                continue
            key = concept if concept not in used and concept != "ignore" else f"extra_{extra}"
            if key.startswith("extra_"):
                extra += 1
            else:
                used.add(concept)
            numeric = concept in lexicon.NUMERIC_COLUMNS and not key.startswith("extra_")
            columns.append(_Column(key, concept, text, x0, x1, numeric))

        keys = {c.key for c in columns}
        has_money = any(c.numeric for c in columns)
        has_anchor = bool(keys & {"date", "description", "reference"})
        named = [c for c in columns if not c.key.startswith("extra_")]
        if not (has_money and has_anchor and len(named) >= 3):
            if not (has_money and {"date", "description"} <= keys and len(named) >= 3):
                return None
        return columns

    @staticmethod
    def _build_layout(columns: list[_Column], page: Page) -> _Layout:
        layout = _Layout(columns=sorted(columns, key=lambda c: c.x0))
        # Dates and amounts are placed by what they are; only prose uses these bounds.
        text_cols = [c for c in layout.columns if not c.numeric and c.concept not in ("date", "value_date")]
        numeric_left = min((c.x0 for c in layout.numeric), default=float("inf"))
        for index, col in enumerate(text_cols):
            left = float("-inf") if index == 0 else col.x0 - 8
            right = text_cols[index + 1].x0 - 8 if index + 1 < len(text_cols) else float("inf")
            if col.key in ("description", "reference") or right == float("inf"):
                right = min(right, numeric_left - 30) if numeric_left != float("inf") and numeric_left > col.x0 else right
            layout.text_bounds.append((left, right, col))
        return layout

    def _complete_layout(self, layout: _Layout, lines: list[_Line], conv: Conventions, page: Page) -> _Layout:
        """Recover an amount column whose heading was misread.

        Amounts that line up on one right edge, well away from every known
        column, are a column; its role follows from the gap it fills (a missing
        debit sits left of the credit column, a missing balance at the far right).
        """
        known = layout.numeric + [c for c in layout.columns if c.concept == "ignore" and c.recognised]
        if not layout.numeric:
            return layout
        orphans: list[_Word] = []
        for line in lines:
            for word in _merge_money(line.words, conv):
                if is_date_token(word.text, conv.order) or _CURRENCY_ONLY.match(word.text):
                    continue
                if not looks_like_money(word.text, conv.number, conv.zero_decimals):
                    continue
                distance = min(min(abs(word.x1 - c.x1), abs(word.x0 - c.x0), abs(word.cx - c.cx)) for c in known)
                if distance > 0.05 * page.width:
                    orphans.append(word)
        clusters: list[list[_Word]] = []
        for word in sorted(orphans, key=lambda w: w.x1):
            if clusters and word.x1 - clusters[-1][-1].x1 <= 0.015 * page.width:
                clusters[-1].append(word)
            else:
                clusters.append([word])

        columns = list(layout.columns)
        have = {c.key for c in columns}
        added = False
        for cluster in clusters:
            if len(cluster) < 3:
                continue
            x1 = sorted(w.x1 for w in cluster)[len(cluster) // 2]
            x0 = min(w.x0 for w in cluster)
            right_edges = [c.x1 for c in layout.numeric]
            role: str | None = None
            credit = next((c for c in layout.numeric if c.key == "credit"), None)
            debit = next((c for c in layout.numeric if c.key == "debit"), None)
            if "amount" in have:
                continue  # a single Amount column: nothing is missing
            if credit is not None and "debit" not in have and x1 < credit.x1:
                role = "debit"
            elif debit is not None and "credit" not in have and x1 > debit.x1 and ("balance" in have and x1 < max(right_edges)):
                role = "credit"
            if role is None:
                continue
            # The unreadable heading that sat over these amounts is replaced, not kept beside them.
            columns = [c for c in columns if c.recognised or not (x0 - 0.05 * page.width <= c.cx <= x1 + 0.05 * page.width)]
            columns.append(_Column(role, role, "(inferred from the amounts)", x0, x1, True))
            have.add(role)
            added = True
        return self._build_layout(columns, page) if added else layout

    def _column_report(self, printed: dict[str, str], found: bool) -> list[DetectedColumn]:
        return [
            DetectedColumn(
                key=key,
                label=label,
                required=key in _REQUIRED_COLUMNS,
                found=found and key in printed,
                printed_as=printed.get(key),
            )
            for key, label in _COLUMN_LABELS.items()
        ]

    # ----------------------------------------------------------- assignment

    def _assign(self, word: _Word, layout: _Layout, conv: Conventions, page: Page) -> tuple[_Column, float]:
        """Column for one word, and how well it fits."""
        is_date = is_date_token(word.text, conv.order)
        if not is_date and parse_date(word.text, conv.order, conv.year, whole=True):
            # "03/04" has no year, so it only counts as a date where a date column is:
            # elsewhere "1/1" is a page counter.
            is_date = bool(layout.date_columns) and min(
                min(abs(word.x0 - c.x0), abs(word.cx - c.cx)) for c in layout.date_columns
            ) <= 0.1 * page.width
        if is_date and layout.date_columns:
            # Headings are often centred over their column while values hug one
            # edge, so judge by whichever of centre / left edge is closer.
            col = min(layout.date_columns, key=lambda c: min(abs(word.cx - c.cx), abs(word.x0 - c.x0)))
            return col, 1.0
        amountish = (
            looks_like_money(word.text, conv.number, conv.zero_decimals)
            and not is_date
            and not _CURRENCY_ONLY.match(word.text)
        )
        if (
            not amountish and conv.zero_decimals and layout.numeric and not is_date
            and re.fullmatch(r"-?\d{1,3}", word.text)
            and min(min(abs(word.x1 - c.x1), abs(word.x0 - c.x0)) for c in layout.numeric) <= 0.06 * page.width
        ):
            amountish = True  # e.g. a JPY balance of 29: no decimals to tell it from a quantity
        if amountish:
            candidates = layout.numeric + [c for c in layout.columns if c.concept == "ignore" and c.recognised]
            if candidates:
                def distance(col: _Column) -> float:
                    return min(abs(word.x1 - col.x1), abs(word.x0 - col.x0), abs(word.cx - col.cx))

                best = min(candidates, key=distance)
                if distance(best) <= 0.28 * page.width and word.x1 >= best.x0 - 0.12 * page.width:
                    return best, max(0.0, 1.0 - distance(best) / (0.28 * page.width))
        for left, right, col in layout.text_bounds:
            if left <= word.x0 < right:
                return col, 1.0
        if layout.text_bounds:
            return layout.text_bounds[-1][2], 0.3
        return layout.columns[-1], 0.3

    # ----------------------------------------------------------------- body

    def _scan_page(
        self,
        page: Page,
        lines: list[_Line],
        layout: _Layout,
        conv: Conventions,
        raw_rows: list[_Row],
        result: TableResult,
    ) -> None:
        current: _Row | None = None
        ended = False
        previous_bottom: float | None = None
        pending_date: date | None = None
        has_date_col = layout.has("date")

        for line in lines:
            words = _merge_money(line.words, conv)
            cells: dict[str, list[tuple[_Word, float]]] = {}
            for word in words:
                col, fit = self._assign(word, layout, conv, page)
                cells.setdefault(col.key, []).append((word, fit))
            text = line.text
            bbox = _union(words)

            def cell_text(key: str) -> str:
                return " ".join(w.text for w, _ in sorted(cells.get(key, []), key=lambda p: p[0].x0))

            if lexicon.PAGE_NUMBER_RE.match(lexicon.fold(text)):
                continue
            if self._is_header_line(line, conv):
                continue

            dates_in_date_col = [
                w for key in ("date",) for w, _ in cells.get(key, []) if is_date_token(w.text, conv.order)
                or parse_date(w.text, conv.order, conv.year, whole=True)
            ]
            has_date = bool(dates_in_date_col)
            money = {
                k: parse_amount(cell_text(k), conv.number)
                for k in ("debit", "credit", "amount", "balance")
                if k in cells
            }
            money = {k: v for k, v in money.items() if v is not None}
            movement = {k: v for k, v in money.items() if k != "balance"}
            all_money = [p for w, _ in (x for pair in cells.values() for x in pair)
                         if (p := parse_amount(w.text, conv.number)) and looks_like_money(w.text, conv.number, conv.zero_decimals)
                         and not is_date_token(w.text, conv.order)]

            # Labels such as "SALDO FINALE" are judged without the dates and
            # amounts around them, so "31/08/26 1.234.567,89 SALDO FINALE" leads with its label.
            label_text = " ".join(
                w.text for w in line.words
                if not is_date_token(w.text, conv.order)
                and not looks_like_money(w.text, conv.number, conv.zero_decimals)
            )
            row_hits = lexicon.find("row", label_text, ocr=conv.ocr)
            concepts = {h.concept for h in row_hits}
            leading = [h for h in row_hits if h.start <= 2]
            leading_concepts = {h.concept for h in leading}

            if ended:
                if has_date and movement:
                    # Dated and carrying an amount, yet after the table's end marker:
                    # it may be a transaction. Report it rather than lose it.
                    result.unresolved.append(UnresolvedLine(
                        page.page_number, text, bbox, "after_table_end", [format(v.value, "f") for v in movement.values()]
                    ))
                continue

            # Opening / closing / carried-forward balances printed as rows.
            if leading_concepts & {"opening", "closing", "balance"} and all_money and not (has_date and movement and not leading_concepts & {"opening", "closing"}):
                kind = "opening" if "opening" in leading_concepts else "closing" if "closing" in leading_concepts else "balance"
                amount = all_money[-1]
                row_date = next(iter(d for w in line.words for d, _, _ in find_dates(w.text, conv.order, conv.year)), None)
                result.balance_rows.append(
                    BalanceRow(kind, page.page_number, text, bbox, amount.signed, row_date, amount.repaired)
                )
                previous_bottom = line.y1
                if kind == "closing" and "c f" not in {h.phrase for h in leading}:
                    ended = True
                continue

            if "totals" in leading_concepts and not has_date:
                result.totals_rows.append(
                    TotalsRow(
                        page.page_number, text, bbox,
                        debit=_value(money.get("debit")), credit=_value(money.get("credit")),
                        amount=_value(money.get("amount")),
                        direction=money["amount"].direction if "amount" in money else None,
                    )
                )
                ended = True
                continue

            if ("end" in concepts and not has_date) or (not has_date and not all_money and "closing" in concepts):
                ended = True
                continue

            # "Entry date 03.09.26": a date that applies to the entries below it.
            if not has_date and not all_money and not movement:
                group_date = next(iter(d for w in line.words for d, _, _ in find_dates(w.text, conv.order, conv.year)), None)
                if group_date is not None and _is_date_group_line(text):
                    pending_date = group_date
                    continue

            starts_row = False
            if has_date_col:
                starts_row = has_date
                if not starts_row and bool(movement) and current is not None and current.has_movement(conv):
                    starts_row = True
            else:
                starts_row = bool(movement)

            if not starts_row:
                if current is None:
                    if movement:
                        result.unresolved.append(UnresolvedLine(
                            page.page_number, text, bbox, "no_open_row", [format(v.value, "f") for v in movement.values()]
                        ))
                    continue
                if previous_bottom is not None and line.y0 - previous_bottom > 2.4 * line.height:
                    # Far below the last row: stray text, not a wrapped line. Later
                    # balance / totals rows are still recognised above.
                    if movement:
                        result.unresolved.append(UnresolvedLine(
                            page.page_number, text, bbox, "detached_line", [format(v.value, "f") for v in movement.values()]
                        ))
                    current = None
                    continue

            if starts_row:
                current = _Row(page=page.page_number, inherited_date=not has_date, pending_date=pending_date)
                raw_rows.append(current)
            if current is None:
                continue
            for key, pairs in cells.items():
                for word, fit in pairs:
                    current.add(key, word, fit)
            previous_bottom = line.y1

    def _is_header_line(self, line: _Line, conv: Conventions) -> bool:
        concepts = {lexicon.classify("column", w.text, ocr=conv.ocr) for w in line.words}
        concepts.discard(None)
        concepts.discard("ignore")
        return len(concepts) >= 3 and bool(concepts & {"date", "description", "reference"})

    # -------------------------------------------------------- no-header path

    def _infer_without_header(
        self, doc: StructuredDocument, conv: Conventions, raw_rows: list[_Row],
        result: TableResult, printed: dict[str, str],
    ) -> None:
        """Last resort: find date-led lines and read the columns from content."""
        for page in doc.pages:
            lines = _prepare_lines(page, conv)
            dated = [
                line for line in lines
                if line.words and parse_date(line.words[0].text, conv.order, conv.year, whole=True)
                and sum(1 for w in line.words if looks_like_money(w.text, conv.number, conv.zero_decimals)) >= 1
            ]
            if len(dated) < 3:
                continue
            money_x1 = sorted(w.x1 for line in dated for w in line.words
                              if looks_like_money(w.text, conv.number, conv.zero_decimals)
                              and not is_date_token(w.text, conv.order))
            clusters: list[list[float]] = []
            for x in money_x1:
                if clusters and x - clusters[-1][-1] <= 18:
                    clusters[-1].append(x)
                else:
                    clusters.append([x])
            edges = [sum(c) / len(c) for c in clusters if len(c) >= max(2, len(dated) // 4)]
            if not edges:
                continue
            roles = ["amount"] if len(edges) == 1 else ["amount", "balance"] if len(edges) == 2 else ["debit", "credit", "balance"][:len(edges)]
            first_amount_x0 = min(w.x0 for line in dated for w in line.words
                                  if looks_like_money(w.text, conv.number, conv.zero_decimals)
                                  and not is_date_token(w.text, conv.order))
            date_x = min(line.words[0].x0 for line in dated)
            cols = [_Column("date", "date", "(inferred)", date_x, date_x + 40, False),
                    _Column("description", "description", "(inferred)", date_x + 45, max(date_x + 60, first_amount_x0 - 10), False)]
            cols += [_Column(role, role, "(inferred)", edge - 50, edge, True) for role, edge in zip(roles, edges)]
            layout = self._build_layout(cols, page)
            result.inferred_columns = True
            printed.update({c.key: c.printed for c in cols})
            # Everything above the first dated line is the statement's head, not the table.
            self._scan_page(page, lines[lines.index(dated[0]):], layout, conv, raw_rows, result)
        if result.inferred_columns:
            result.add_note(message("columns_inferred", "No column headings were recognised; columns were inferred from the layout."))

    # ---------------------------------------------------------------- parse

    def _parse(self, raw_rows: list[_Row], conv: Conventions) -> list[TableRow]:
        rows: list[TableRow] = []
        last_date: date | None = None
        for raw in raw_rows:
            words = raw.words()
            row = TableRow(
                index=len(rows) + 1,
                page=raw.page,
                bbox=_union(words),
                raw={k: raw.text(k) for k in raw.cells if not k.startswith("extra_")},
                fit=round(sum(raw.fits) / len(raw.fits), 3) if raw.fits else 1.0,
                extras={k: raw.text(k) for k in raw.cells if k.startswith("extra_") and raw.text(k)},
            )
            row.description = _clean(raw.text("description"))
            row.reference = _clean(raw.text("reference"))

            date_text = raw.text("date")
            if date_text:
                row.date = parse_date(date_text, conv.order, conv.year, whole=True) or parse_date(date_text, conv.order, conv.year)
                if row.date is None:
                    row.add_issue(message("date_unreadable", f"Unreadable date '{date_text}'", text=date_text))
            if row.date is None and not date_text:
                if raw.pending_date is not None:
                    row.date, row.date_inherited = raw.pending_date, True
                elif last_date is not None:
                    row.date, row.date_inherited = last_date, True
            if row.date is not None:
                last_date = row.date
            if raw.text("value_date"):
                row.value_date = parse_date(raw.text("value_date"), conv.order, conv.year)

            drcr = raw.text("drcr")
            if not drcr:
                # The indicator column's heading may be in a language we do not know;
                # its *values* ("D", "C", "Af", "Bij", "S", "H") still give it away.
                drcr = next((t for k, t in row.extras.items() if _indicator_from(t)), "")
            for key in ("debit", "credit", "amount", "balance"):
                text = raw.text(key)
                if not text or text.strip() in {"-", "--", "—", "nil", "n/a"}:
                    continue
                parsed = parse_amount(text, conv.number)
                if parsed is None:
                    row.add_issue(message("amount_unreadable", f"Unreadable {key} '{text}'", column=key, text=text))
                    continue
                if parsed.repaired:
                    row.ocr_repaired = True
                    row.add_issue(message("amount_ocr_repaired", f"Amount '{text}' was repaired after OCR", text=text))
                self._apply_amount(row, key, parsed, drcr)
            rows.append(row)
        return rows

    @staticmethod
    def _apply_amount(row: TableRow, key: str, parsed: ParsedAmount, drcr: str) -> None:
        if key == "balance":
            row.balance = parsed.signed
            return
        if parsed.value == 0:
            return
        if key == "debit":
            row.debit = parsed.value
        elif key == "credit":
            row.credit = parsed.value
        else:  # single Amount column: sign, suffix or Dr/Cr column say which way
            direction = parsed.direction or _indicator_from(drcr)
            if direction == "dr":
                row.debit = parsed.value
            elif direction == "cr":
                row.credit = parsed.value
            else:
                row.amount = parsed.value


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _is_date_group_line(text: str) -> bool:
    folded = lexicon.fold(text)
    return bool(re.search(r"\b(entry date|booking date|value date|posting date|date|datum|data|dato|pvm)\b", folded))


_DEBIT_WORDS = {"dr", "d", "debit", "db", "s", "soll", "af", "debe", "debito", "uscita", "uit", "ut", "retrait", "g"}
_CREDIT_WORDS = {"cr", "c", "credit", "h", "haben", "bij", "haber", "credito", "entrata", "in", "inn", "k", "depot", "p"}


def _indicator_from(text: str) -> str | None:
    lowered = lexicon.fold(text).strip().strip(".")
    if lowered in _DEBIT_WORDS:
        return "dr"
    if lowered in _CREDIT_WORDS:
        return "cr"
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _value(amount: ParsedAmount | None) -> Decimal | None:
    return amount.value if amount else None


def _union(words: list[_Word]) -> BBox:
    return (
        min(w.x0 for w in words), min(w.y0 for w in words),
        max(w.x1 for w in words), max(w.y1 for w in words),
    )


def _page_words(page: Page) -> list[_Word]:
    out: list[_Word] = []
    for block in page.blocks:
        for text, x0, x1 in block.word_spans():
            out.append(_Word(text, x0, x1, block.y0, block.y1, block.id, block.confidence, block.source))
    return out


def _prepare_lines(page: Page, conv: Conventions) -> list[_Line]:
    """Lines of the page with multi-word dates joined into single words."""
    lines = _cluster_lines(_page_words(page))
    for line in lines:
        line.words = _merge_money(_merge_dates(line.words, conv), conv)
    return lines


def _merge_dates(words: list[_Word], conv: Conventions) -> list[_Word]:
    """"1 mars 2025", "Mar 1, 2025", "01 Mar" arrive as separate words; a date must be one."""
    out: list[_Word] = []
    i = 0
    while i < len(words):
        merged = False
        for span in (3, 2):
            chunk = words[i:i + span]
            if len(chunk) < span:
                continue
            # words of one date sit close together
            if any(b.x0 - a.x1 > max(8.0, 1.6 * a.h) for a, b in zip(chunk, chunk[1:])):
                continue
            joined = " ".join(w.text for w in chunk)
            if not re.search(r"[^\W\d_]{2}", joined):  # a month name, accented or not (août, déc.)
                continue
            if parse_date(joined, conv.order, conv.year, whole=True):
                out.append(_Word(joined, chunk[0].x0, chunk[-1].x1, min(w.y0 for w in chunk), max(w.y1 for w in chunk),
                                 chunk[0].block, chunk[0].conf, chunk[0].source))
                i += span
                merged = True
                break
        if not merged:
            out.append(words[i])
            i += 1
    return out


def _cluster_lines(words: list[_Word]) -> list[_Line]:
    """Group words that sit on the same visual line, top to bottom."""
    ordered = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    lines: list[list[_Word]] = []
    for word in ordered:
        centre = (word.y0 + word.y1) / 2
        if lines:
            last = lines[-1]
            last_centre = sum((w.y0 + w.y1) / 2 for w in last) / len(last)
            tolerance = 0.5 * min(word.h, max(w.h for w in last)) + 0.5
            if abs(centre - last_centre) <= tolerance:
                last.append(word)
                continue
        lines.append([word])
    return [_Line(sorted(group, key=lambda w: w.x0)) for group in lines]


def _merge_money(words: list[_Word], conv: Conventions) -> list[_Word]:
    """Re-join an amount that extraction split: `EUR` `1,234.56`, `1 234,56`, `12,34` `-`."""
    out: list[_Word] = []
    for word in sorted(words, key=lambda w: w.x0):
        if out:
            prev = out[-1]
            gap = word.x0 - prev.x1
            tight = gap <= max(6.0, 0.9 * prev.h)
            prev_money = looks_like_money(prev.text, conv.number, conv.zero_decimals)
            word_money = looks_like_money(word.text, conv.number, conv.zero_decimals)
            glue = False
            if tight and prev_money and _INDICATOR_ONLY.match(word.text):
                glue = True
            elif tight and _CURRENCY_ONLY.match(prev.text) and word_money:
                glue = True
            elif tight and prev_money and _CURRENCY_ONLY.match(word.text) and not is_date_token(prev.text, conv.order):
                glue = True
            elif (
                tight and re.fullmatch(r"\d{1,3}", prev.text) and gap <= 5
                and re.fullmatch(r"\d{3}(?:[.,]\d{1,4})?[+\-)]?", word.text)
                and (conv.number.decimal == "," or conv.zero_decimals or " " == conv.number.group)
            ):
                glue = True
            if glue:
                out[-1] = _Word(
                    f"{prev.text} {word.text}".strip(), prev.x0, word.x1,
                    min(prev.y0, word.y0), max(prev.y1, word.y1), prev.block, prev.conf, prev.source,
                )
                continue
        out.append(word)
    return out


@dataclass
class _Cell:
    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    block: str


def _split_fused(words: list[_Word], ocr: bool) -> list[_Cell]:
    """Heading cells, with fused ones ("ENTRATE DESCRIZIONE") cut apart.

    Words of one text run are grouped back into a cell; the cell is then cut at
    every boundary where a *different* column concept starts.
    """
    by_block: dict[str, list[_Word]] = {}
    for w in words:
        by_block.setdefault(w.block, []).append(w)
    cells: list[_Cell] = []
    for ws in by_block.values():
        ws.sort(key=lambda w: w.x0)
        pieces: list[list[_Word]] = [ws]
        if len(ws) >= 2:
            pieces = _cut_by_phrases(ws, ocr)
        for piece in pieces:
            cells.append(
                _Cell(
                    " ".join(w.text for w in piece),
                    min(w.x0 for w in piece), max(w.x1 for w in piece),
                    min(w.y0 for w in piece), max(w.y1 for w in piece), piece[0].block,
                )
            )
    return cells


def _cut_by_phrases(ws: list[_Word], ocr: bool) -> list[list[_Word]]:
    """Cut a run of heading words where one column's phrase ends and another's begins.

    "ENTRATE DESCRIZIONE" holds two phrases and is cut; "Withdrawal Amt." is one
    phrase and is not, even though its words alone would name two columns.
    """
    text = " ".join(w.text for w in ws)
    hits = [h for h in lexicon.find("column", text, ocr=ocr) if h.concept != "ignore"]
    if len({h.concept for h in hits}) < 2:
        return [ws]
    offsets, cursor = [], 0
    for w in ws:
        offsets.append((cursor, cursor + len(w.text)))
        cursor += len(w.text) + 1
    pieces: list[list[_Word]] = []
    owner: str | None = None
    for w, (a, b) in zip(ws, offsets):
        hit = next((h for h in hits if h.char_start < b and a < h.char_end), None)
        concept = hit.concept if hit else None
        if pieces and (concept is None or concept == owner):
            pieces[-1].append(w)
        else:
            pieces.append([w])
            owner = concept
    return pieces


def _stack(cells: list[_Cell]) -> list[list[_Cell]]:
    """Group heading cells that sit above one another ("Date" over "Trans.")."""
    stacks: list[list[_Cell]] = []
    for cell in sorted(cells, key=lambda c: (c.x0, c.y0)):
        for stack in stacks:
            # Compare with each member, not the stack's union, so a wide
            # heading cannot swallow its neighbour.
            if any(
                min(cell.x1, c.x1) - max(cell.x0, c.x0) >= 0.3 * min(cell.x1 - cell.x0, c.x1 - c.x0)
                or abs(cell.x0 - c.x0) <= 3
                for c in stack
            ):
                stack.append(cell)
                break
        else:
            stacks.append([cell])
    return stacks


table_extractor = TableExtractor()
