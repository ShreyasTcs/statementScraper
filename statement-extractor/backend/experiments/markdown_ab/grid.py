"""Table grid from page geometry ("lines and gaps"), and a table extractor that uses it.

Why: a cell that is empty in a statement has no text at all, so any step that reads a row from left to right, or that
places an amount by its distance to a heading, can shift an amount into the neighbouring column (debit <-> credit).
The column an amount belongs to is visible on the page, though: amounts of one column line up, and columns are
separated by white channels or ruling lines. This module finds those columns from the whole table at once, so a cell
that is empty in one row cannot move a value in another.

How, per page (computer vision on the page; nothing tuned to a bank or a layout):

* **Table area.** Where the application finds the table's heading row, the table is everything below it (dates and
  amounts may sit on different lines, as OCR often splits a row). Without a recognised heading, lines that carry a
  date and an amount (anywhere on the line, so right-to-left layouts work too) mark the table, from the first to the
  last of them. Opening/closing/total lines never shape the columns.
* **Gaps.** The boxes of the amounts on the table's lines are drawn as a mask image (one image row per text line) and
  projected onto the x axis with OpenCV. Columns are the runs that contain amounts; the white runs between them are
  the gaps. Only amounts are drawn, so a sparse column (two credits in thirty rows) still forms a column, and a long
  description that runs into an empty amount column cannot close a gap. All widths are relative to the page's own
  median character width.
* **Alignment.** A run that holds two tightly right-aligned groups of amounts is split between them (two columns
  printed close together).
* **Lines.** On native-text pages without rotation, the page is rendered and long vertical ruling lines are found with
  morphological opening (OpenCV); a ruling line inside a run splits it. On OCR'd pages the picture and the OCR
  coordinates are not in the same frame (the OCR straightens the image), so only the gaps are used there.

``GridTableExtractor`` is the application's ``TableExtractor`` with one decision replaced: which amount column an
amount belongs to. Header reading, row building, parsing, totals and validation stay the application's own. The
detected heading columns are matched to the grid's columns once per table (one-to-one, by the edges the values
actually line up on); an amount then goes to the column of the band it sits in. When the match is ambiguous, or the
amount is outside every band, the application's original decision is kept.
"""
from __future__ import annotations

import itertools
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import copy

import cv2
import numpy as np

from app.rules import lexicon
from app.rules.dates import is_date_token, parse_date
from app.rules.numbers import looks_like_money, parse_amount
from app.services import table_service as T
from app.services.document_model import Page, StructuredDocument
from app.services.table_service import Conventions, TableExtractor, TableResult, infer_conventions

SCALE = 2.0                      # mask pixels per PDF point
RENDER_ZOOM = 2.0                # page raster for ruling lines: 144 dpi
ROLE_COLOURS = {"debit": (60, 60, 220), "credit": (40, 160, 40), "balance": (170, 60, 150), "amount": (0, 140, 230)}


@dataclass
class Band:
    x0: float
    x1: float
    right_edge: float
    words: int
    split_by: str = "gap"            # gap | alignment | ruling line

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class PageGrid:
    page: int
    region: tuple[float, float]
    char_width: float
    bands: list[Band]
    rules_x: list[float] = field(default_factory=list)
    rules_used: bool = False
    table_lines: int = 0
    note: str = ""

    def band_of(self, x0: float, x1: float) -> int | None:
        cx = (x0 + x1) / 2
        for i, band in enumerate(self.bands):
            if band.x0 - 0.5 * self.char_width <= cx <= band.x1 + 0.5 * self.char_width:
                return i
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"page": self.page, "region": [round(v, 1) for v in self.region], "char_width": round(self.char_width, 2),
                "table_lines": self.table_lines, "rules_used": self.rules_used,
                "ruling_lines_x": [round(v, 1) for v in self.rules_x], "note": self.note,
                "bands": [{"x0": round(b.x0, 1), "x1": round(b.x1, 1), "right_edge": round(b.right_edge, 1),
                           "amounts": b.words, "split_by": b.split_by} for b in self.bands]}


def is_amount(word: Any, conv: Conventions) -> bool:
    return (looks_like_money(word.text, conv.number, conv.zero_decimals) and not is_date_token(word.text, conv.order)
            and not T._CURRENCY_ONLY.match(word.text))


def is_date(word: Any, conv: Conventions) -> bool:
    return is_date_token(word.text, conv.order) or bool(parse_date(word.text, conv.order, conv.year, whole=True))


def is_balance_or_total_line(line: Any, conv: Conventions) -> bool:
    """Opening / closing / carried-forward balance and totals lines, judged by the application's own row vocabulary on
    the line's label (the words that are neither dates nor amounts), the same test the table detector uses."""
    label = " ".join(w.text for w in line.words
                     if not is_date_token(w.text, conv.order) and not looks_like_money(w.text, conv.number, conv.zero_decimals))
    leading = {h.concept for h in lexicon.find("row", label, ocr=conv.ocr) if h.start <= 2}
    return bool(leading & {"opening", "closing", "balance", "totals"})


def _char_width(lines: list[Any]) -> float:
    widths = [(w.x1 - w.x0) / len(w.text) for line in lines for w in line.words if len(w.text) >= 2 and w.x1 > w.x0]
    return statistics.median(widths) if widths else 5.0


# --------------------------------------------------------------------------
# Ruling lines (native-text pages without rotation)
# --------------------------------------------------------------------------


def vertical_rules(source_path: str | None, page_number: int, region: tuple[float, float]) -> tuple[list[float], str]:
    """x positions (PDF points) of long vertical ruling lines crossing the table area."""
    if not source_path or not str(source_path).lower().endswith(".pdf"):
        return [], "no ruling-line search (not a PDF)"
    import fitz

    with fitz.open(source_path) as doc:
        page = doc[page_number - 1]
        if page.rotation:
            return [], "no ruling-line search (rotated page)"
        pix = page.get_pixmap(matrix=fitz.Matrix(RENDER_ZOOM, RENDER_ZOOM), colorspace=fitz.csGRAY, alpha=False)
    gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    top, bottom = int(region[0] * RENDER_ZOOM), int(region[1] * RENDER_ZOOM)
    if bottom - top < 20:
        return [], "table area too small for a ruling-line search"
    ink = (gray[top:bottom] < 160).astype(np.uint8) * 255
    length = max(20, int(0.5 * (bottom - top)))
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, length))
    opened = cv2.morphologyEx(ink, cv2.MORPH_OPEN, kernel)
    columns = cv2.reduce(opened, 0, cv2.REDUCE_MAX).ravel() > 0
    xs: list[float] = []
    run_start = None
    for x, on in enumerate(list(columns) + [False]):
        if on and run_start is None:
            run_start = x
        elif not on and run_start is not None:
            xs.append((run_start + x - 1) / 2 / RENDER_ZOOM)
            run_start = None
    return xs, f"{len(xs)} vertical ruling line(s) found"


# --------------------------------------------------------------------------
# The grid of one page
# --------------------------------------------------------------------------


def build_page_grid(page: Page, lines: list[Any], conv: Conventions, source_path: str | None = None,
                    native: bool = True, below: float | None = None) -> PageGrid | None:
    """``below``: the bottom of the table's heading row on this page, or -inf for a continuation page of a table whose
    heading was found earlier; None when no heading is known (then dated lines with an amount mark the table)."""
    if below is not None:
        txn = [l for l in lines if l.y0 >= below - 0.5 and any(is_amount(w, conv) for w in l.words)
               and not is_balance_or_total_line(l, conv)]
    else:
        txn = [l for l in lines if any(is_date(w, conv) for w in l.words) and any(is_amount(w, conv) for w in l.words)
               and not is_balance_or_total_line(l, conv)]
    if len(txn) < 2:
        return None
    top, bottom = min(l.y0 for l in txn), max(l.y1 for l in txn)
    region_lines = [l for l in lines if top - 0.5 <= (l.y0 + l.y1) / 2 <= bottom + 0.5]
    cw = _char_width(region_lines)
    width_px = int(page.width * SCALE) + 2
    mask = np.zeros((len(region_lines), width_px), dtype=np.uint8)
    amount_words: list[Any] = []
    for row, line in enumerate(region_lines):
        if is_balance_or_total_line(line, conv):
            continue                                     # not a transaction row: it does not shape the columns
        for w in line.words:
            if is_amount(w, conv):
                a, b = max(0, int(w.x0 * SCALE)), min(width_px, int(np.ceil(w.x1 * SCALE)) + 1)
                mask[row, a:b] = 255
                amount_words.append(w)
    if not amount_words:
        return None
    coverage = cv2.reduce(mask, 0, cv2.REDUCE_MAX).ravel() > 0
    runs: list[list[float]] = []
    start = None
    for x, on in enumerate(list(coverage) + [False]):
        if on and start is None:
            start = x
        elif not on and start is not None:
            runs.append([start / SCALE, (x - 1) / SCALE])
            start = None
    merged: list[list[float]] = []
    for run in runs:                                     # a word space inside a number is not a gap
        if merged and run[0] - merged[-1][1] < 1.0 * cw:
            merged[-1][1] = run[1]
        else:
            merged.append(run)

    rules, rule_note = (vertical_rules(source_path, page.page_number, (top, bottom)) if native
                        else ([], "no ruling-line search (OCR page: picture and text are not in one frame)"))
    bands: list[Band] = []
    for x0, x1 in merged:
        members = [w for w in amount_words if x0 - 0.01 <= (w.x0 + w.x1) / 2 <= x1 + 0.01]
        groups = _split(members, cw, [r for r in rules if x0 + cw < r < x1 - cw])
        for group, how in groups:
            edges = sorted(w.x1 for w in group)
            bands.append(Band(min(w.x0 for w in group), max(w.x1 for w in group), edges[len(edges) // 2], len(group), how))
    return PageGrid(page.page_number, (top, bottom), cw, bands, rules, bool(rules), len(txn), rule_note)


def _split(words: list[Any], cw: float, rules: list[float]) -> list[tuple[list[Any], str]]:
    """Split a run of amounts at ruling lines, then between tightly right-aligned groups."""
    if rules:
        parts, edges = [], [float("-inf")] + sorted(rules) + [float("inf")]
        for left, right in zip(edges, edges[1:]):
            part = [w for w in words if left <= (w.x0 + w.x1) / 2 < right]
            if part:
                parts.append((part, "ruling line"))
        if len(parts) > 1:
            return parts
    ordered = sorted(words, key=lambda w: w.x1)
    clusters: list[list[Any]] = []
    for w in ordered:
        if clusters and w.x1 - clusters[-1][-1].x1 <= 1.5 * cw:
            clusters[-1].append(w)
        else:
            clusters.append([w])
    tight = all((c[-1].x1 - c[0].x1) <= 1.5 * cw for c in clusters)
    separated = all(b[0].x1 - a[-1].x1 >= 3 * cw for a, b in zip(clusters, clusters[1:]))
    if len(clusters) > 1 and tight and separated:
        return [(c, "alignment") for c in clusters]
    return [(words, "gap")]


# --------------------------------------------------------------------------
# The extractor
# --------------------------------------------------------------------------


class GridTableExtractor(TableExtractor):
    """The application's TableExtractor, with amount columns decided by the page grid."""

    def __init__(self, source_path: str | None = None) -> None:
        super().__init__()
        self.source_path = source_path
        self.grids: dict[int, PageGrid | None] = {}
        self.mappings: dict[tuple[int, int], dict[int, Any]] = {}
        self.mapping_notes: list[dict[str, Any]] = []
        self.changes: list[dict[str, Any]] = []
        self.kept: int = 0
        self._heading_bottom: dict[int, float] = {}
        self._heading_seen = False

    def extract(self, doc: StructuredDocument, conv: Conventions | None = None) -> TableResult:
        ocr = any(b.source == "ocr" for _, b in doc.iter_blocks())
        conv = conv or infer_conventions(doc, ocr)
        conv.ocr = ocr
        self.grids, self._heading_bottom, self._heading_seen = {}, {}, False
        self.source_path = self.source_path or getattr(doc.content, "source_path", None)
        # Pass 1: the application's own column decisions, counted per grid column (nothing is changed).
        self._votes: dict[tuple[int, int], Counter] = defaultdict(Counter)
        self._voting = True
        super().extract(doc, copy.deepcopy(conv))
        self._voting = False
        self.consensus = self._consensus()
        # Pass 2: every amount follows its grid column's consensus.
        self.changes, self.kept, self.mappings, self.mapping_notes = [], 0, {}, []
        return super().extract(doc, conv)

    def _consensus(self) -> dict[int, dict[int, str]]:
        """Per page: grid column -> role, where at least two thirds of the column's amounts (and at least two) were
        given that role by the application itself."""
        out: dict[int, dict[int, str]] = defaultdict(dict)
        for (page, band), votes in self._votes.items():
            role, count = votes.most_common(1)[0]
            total = sum(votes.values())
            if count >= 2 and count * 3 >= total * 2:
                out[page][band] = role
        return dict(out)

    def _find_header(self, page: Page, lines: list[Any], conv: Conventions):
        found = super()._find_header(page, lines, conv)
        if found is not None:
            self._heading_bottom[page.page_number] = max(w.y1 for w in found[2])
            self._heading_seen = True
        return found

    def _grid(self, page: Page, conv: Conventions) -> PageGrid | None:
        """The page's grid, built on first use: below the heading row if one was found on this page, the whole page
        for a continuation page of a table with a heading, otherwise from dated lines."""
        if page.page_number not in self.grids:
            below = self._heading_bottom.get(page.page_number, float("-inf") if self._heading_seen else None)
            native = bool(page.blocks) and all(b.source != "ocr" for b in page.blocks)
            self.grids[page.page_number] = build_page_grid(page, T._prepare_lines(page, conv), conv, self.source_path,
                                                           native, below)
        return self.grids[page.page_number]

    # ---- matching the heading columns to the grid's columns, once per page and layout

    def _mapping(self, page: Page, layout: Any, grid: PageGrid) -> dict[int, Any]:
        key = (page.page_number, id(layout))
        if key in self.mappings:
            return self.mappings[key]
        columns = list(layout.numeric)
        bands = grid.bands
        limit = 0.2 * page.width

        def cost(col: Any, band: Band) -> float:
            return min(abs(band.right_edge - col.x1), abs(band.cx - col.cx), abs(band.x0 - col.x0))

        best, second = None, None
        k = min(len(columns), len(bands))
        for chosen_cols in itertools.combinations(range(len(columns)), k):
            for chosen_bands in itertools.permutations(range(len(bands)), k):
                pairs = list(zip(chosen_cols, chosen_bands))
                if any(cost(columns[c], bands[b]) > limit for c, b in pairs):
                    continue
                if any(bands[b1].cx > bands[b2].cx for (c1, b1), (c2, b2) in itertools.combinations(pairs, 2)
                       if columns[c1].cx < columns[c2].cx):
                    continue                                # columns keep their left-to-right order
                total = sum(cost(columns[c], bands[b]) for c, b in pairs)
                candidate = (len(pairs), -total, pairs)
                if best is None or candidate[:2] > best[:2]:
                    best, second = candidate, best
                elif second is None or candidate[:2] > second[:2]:
                    second = candidate
        mapping: dict[int, Any] = {}
        note = {"page": page.page_number, "columns": [c.key for c in columns], "bands": len(bands)}
        if best is not None:
            ambiguous = (second is not None and second[0] == best[0] and second[2] != best[2]
                         and abs(second[1] - best[1]) <= 0.05 * max(1.0, -best[1]) + 0.5 * grid.char_width)
            if ambiguous:
                note["result"] = "ambiguous: two matchings fit equally well; the original decision is kept"
            else:
                mapping = {b: columns[c] for c, b in best[2]}
                note["result"] = {columns[c].key: f"band {b}" for c, b in best[2]}
        else:
            note["result"] = "no matching within reach; the original decision is kept"
        self.mappings[key] = mapping
        self.mapping_notes.append(note)
        return mapping

    def _assign(self, word: Any, layout: Any, conv: Conventions, page: Page) -> tuple[Any, float]:
        col, fit = super()._assign(word, layout, conv, page)
        if not col.numeric or not is_amount(word, conv):
            return col, fit
        grid = self._grid(page, conv)
        if grid is None:
            return col, fit
        band = grid.band_of(word.x0, word.x1)
        if band is None:
            return col, fit
        if getattr(self, "_voting", False):
            self._votes[(page.page_number, band)][col.key] += 1
            return col, fit
        role = self.consensus.get(page.page_number, {}).get(band)
        by_heading = self._mapping(page, layout, grid).get(band)
        if role is None or (by_heading is not None and by_heading.key != role):
            return col, fit                     # no consensus, or the headings disagree: keep the original decision
        target = next((c for c in layout.numeric if c.key == role), None)
        if target is None or target is col:
            self.kept += 1
            return col, fit
        self.changes.append({"page": page.page_number, "from": col.key, "to": target.key, "band": band})
        return target, 1.0

    # ---- statements without a recognised heading

    def _infer_without_header(self, doc: StructuredDocument, conv: Conventions, raw_rows: list, result: TableResult,
                              printed: dict[str, str]) -> None:
        """As the application's own, but columns come from the grid (no minimum count, so a sparse column stays a
        column) and two amount columns are read as amount + balance only when the right one is filled on (almost)
        every row; when each row fills exactly one of them they are debit + credit."""
        for page in doc.pages:
            lines = T._prepare_lines(page, conv)
            dated = [line for line in lines
                     if line.words and parse_date(line.words[0].text, conv.order, conv.year, whole=True)
                     and sum(1 for w in line.words if looks_like_money(w.text, conv.number, conv.zero_decimals)) >= 1]
            if len(dated) < 3:
                continue
            grid = self._grid(page, conv)
            if grid is None or not grid.bands:
                continue
            bands = [b for b in grid.bands if any(grid.band_of(w.x0, w.x1) == grid.bands.index(b)
                                                  for l in dated for w in l.words if is_amount(w, conv))]
            if not bands:
                continue
            filled = [[any(is_amount(w, conv) and grid.band_of(w.x0, w.x1) == grid.bands.index(b) for w in l.words)
                       for b in bands] for l in dated]
            if len(bands) == 1:
                roles = ["amount"]
            elif len(bands) == 2:
                right_share = sum(f[1] for f in filled) / len(filled)
                exclusive = sum(1 for f in filled if f[0] != f[1]) / len(filled)
                roles = ["debit", "credit"] if exclusive >= 0.8 and right_share < 0.8 else ["amount", "balance"]
            else:
                roles = ["debit", "credit", "balance"]
            date_x = min(line.words[0].x0 for line in dated)
            first_amount_x0 = min(b.x0 for b in bands)
            cols = [T._Column("date", "date", "(inferred)", date_x, date_x + 40, False),
                    T._Column("description", "description", "(inferred)", date_x + 45, max(date_x + 60, first_amount_x0 - 10), False)]
            cols += [T._Column(role, role, "(inferred from the grid)", b.x0, b.right_edge, True) for role, b in zip(roles, bands)]
            layout = self._build_layout(cols, page)
            result.inferred_columns = True
            printed.update({c.key: c.printed for c in cols})
            self._scan_page(page, lines[lines.index(dated[0]):], layout, conv, raw_rows, result)
        if result.inferred_columns:
            result.add_note(T.message("columns_inferred", "No column headings were recognised; columns were inferred from the layout."))

    def summary(self) -> dict[str, Any]:
        moves: dict[str, int] = {}
        for c in self.changes:
            moves[f"{c['from']} -> {c['to']}"] = moves.get(f"{c['from']} -> {c['to']}", 0) + 1
        return {"pages": [g.as_dict() if g else None for g in self.grids.values()],
                "matchings": self.mapping_notes, "consensus": {str(k): v for k, v in getattr(self, "consensus", {}).items()},
                "amounts_moved": len(self.changes), "moves": moves, "amounts_confirmed": self.kept}


# --------------------------------------------------------------------------
# Geometry proposes, arithmetic decides
# --------------------------------------------------------------------------


COMPLETENESS_RANK = {"complete": 0, "partial": 1, "needs_review": 2, "failed": 3}


def arithmetic_key(analysis: Any) -> tuple[int, int, int]:
    """Lower is better: failed checks, rows whose running balance does not follow, completeness."""
    failed = sum(1 for a in analysis.accounts for c in a.validation.checks if c.status == "fail")
    mismatches = sum(1 for a in analysis.accounts for r in a.table.rows if r.balance_check == "mismatch")
    return failed, mismatches, COMPLETENESS_RANK.get(analysis.completeness.status, 9)


def analyze_local_with_grid(content: Any, pages: Any, source_path: str | None) -> tuple[Any, Any, GridTableExtractor, dict[str, Any]]:
    """The application's local engine twice: as it ships, and with the grid. The grid result is kept unless it makes
    the statement's own checks worse; ties go to the grid, because its geometry is evidence the checks cannot see.
    Returns (chosen analysis, grid-only analysis, the grid extractor, the decision)."""
    import copy

    from app.services import pipeline as P

    original = P.analyze(copy.deepcopy(content), engine="local", pages=pages)
    extractor = GridTableExtractor(source_path)
    saved = P.table_extractor
    P.table_extractor = extractor
    try:
        gridded = P.analyze(copy.deepcopy(content), engine="local", pages=pages)
    finally:
        P.table_extractor = saved
    k_original, k_grid = arithmetic_key(original), arithmetic_key(gridded)
    if not extractor.changes:
        return gridded, gridded, extractor, {"used": "grid", "reason": "the grid confirmed every amount column"}
    if k_grid <= k_original:
        return gridded, gridded, extractor, {
            "used": "grid", "reason": f"the grid moved {len(extractor.changes)} amount(s); the checks are no worse "
                                      f"(failed checks, balance mismatches, completeness: {k_original} -> {k_grid})"}
    return original, gridded, extractor, {
        "used": "original", "reason": f"the grid moved {len(extractor.changes)} amount(s) but the checks got worse "
                                      f"({k_original} -> {k_grid}); the original reading is kept"}


# --------------------------------------------------------------------------
# The grid as a check of a language model's column choice
# --------------------------------------------------------------------------


def _canon(text: str | None) -> str:
    return "".join((text or "").split()).replace("\u00a0", "").replace("\u202f", "")


class GridRowCheck:
    """A row hook for the LLM path: for every amount the model put in the debit or the credit column, find that printed
    amount on the page within the lines the model cited, see which grid column it sits in, and move it to that column
    if the model chose the other one. Rows whose amount cannot be found, or sits in no matched column, are left as the
    model read them. ``line_boxes`` maps the pipeline's line ids to (page, bbox) of the page text they come from."""

    def __init__(self, extractor: GridTableExtractor, line_boxes: dict[str, list[tuple[int, tuple[float, ...]]]]) -> None:
        self.extractor = extractor
        self.line_boxes = line_boxes
        self.log: list[dict[str, Any]] = []
        self.checked = 0
        #: The model's rows as it read them, and after this check, both before validation (for scoring each step).
        self.rows_as_read: list[dict[str, Any]] = []
        self.rows_after_check: list[dict[str, Any]] = []

    @staticmethod
    def _snapshot(mapped: list[Any]) -> list[dict[str, Any]]:
        def text(value: Any) -> str | None:
            return None if value is None else format(value, "f")
        return [{"date": r.date.isoformat() if r.date else None, "debit": text(r.debit), "credit": text(r.credit),
                 "amount": text(r.amount), "balance": text(r.balance), "page": r.page}
                for m in mapped for r in m.table.rows]

    def _role(self, page: int, band: int | None) -> str | None:
        """The grid column's role: the engine's consensus (two thirds, at least two amounts) or, for a column with too
        few amounts for a consensus, the engine's unanimous decision for it when the heading geometry agrees. Either
        way the heading geometry must not contradict it."""
        if band is None:
            return None
        heading = next((m for (p, _), m in self.extractor.mappings.items() if p == page), {}).get(band)
        role = self.extractor.consensus.get(page, {}).get(band)
        if role is None:
            votes = getattr(self.extractor, "_votes", {}).get((page, band))
            if votes and len(votes) == 1 and heading is not None and heading.key == next(iter(votes)):
                role = heading.key
        return role if heading is None or heading.key == role else None

    def __call__(self, mapped: list[Any], structured: StructuredDocument, conv: Conventions) -> None:
        self.rows_as_read = self._snapshot(mapped)
        words: dict[int, list[Any]] = {}
        for page in structured.pages:
            words[page.page_number] = [w for line in T._prepare_lines(page, conv) for w in line.words]
        for result in mapped:
            for row in result.table.rows:
                side = "debit" if row.debit is not None else "credit" if row.credit is not None else (
                    "amount" if row.amount is not None else None)
                if side is None:
                    continue
                value = row.debit if side == "debit" else row.credit if side == "credit" else row.amount
                boxes = [box for line_id in (row.evidence or {}).get("lines", []) for box in self.line_boxes.get(line_id, [])]
                if value is None or not boxes:
                    continue
                self.checked += 1
                found = []
                for page, (x0, y0, x1, y1) in boxes:
                    for w in words.get(page, []):
                        cy = (w.y0 + w.y1) / 2
                        if not (y0 - 1 <= cy <= y1 + 1 and w.x1 >= x0 - 1 and w.x0 <= x1 + 1) or not is_amount(w, conv):
                            continue
                        parsed = parse_amount(w.text, conv.number)
                        if parsed is not None and abs(parsed.value) == abs(value):
                            found.append((page, w))
                roles = set()
                pages_by_number = {p.page_number: p for p in structured.pages}
                for page, w in found:
                    grid = self.extractor._grid(pages_by_number[page], conv) if page in pages_by_number else None
                    band = grid.band_of(w.x0, w.x1) if grid else None
                    roles.add(self._role(page, band))
                if len(roles) != 1:
                    continue
                target = roles.pop()
                if target not in ("debit", "credit") or target == side:
                    continue
                if side == "amount":
                    if target == "debit":
                        row.debit = abs(value)
                    else:
                        row.credit = abs(value)
                    row.amount = None
                else:
                    row.debit, row.credit = row.credit, row.debit
                    row.raw["debit"], row.raw["credit"] = row.raw.get("credit"), row.raw.get("debit")
                row.direction_inferred = True
                row.evidence = {**(row.evidence or {}), "column_corrected_by_grid": {"from": side, "to": target}}
                self.log.append({"page": row.page, "from": side, "to": target})
        self.rows_after_check = self._snapshot(mapped)


def replay_steps(calls: list[dict[str, Any]], model: str) -> list[Any]:
    """The recorded answers of a pipeline, as steps for the application's ScriptedClient (no model is called)."""
    from app.services.llm_client import LLMError, LLMResponse

    steps: list[Any] = []
    for call in calls:
        if call.get("error_code"):
            steps.append(LLMError(call["error_code"], "recorded failure", retry_after=call.get("retry_after"), fatal=call["error_code"] in ("llm_model_unavailable", "llm_model_mismatch", "local_only_violation", "llm_auth", "llm_permission")))
        else:
            finish = "length" if call.get("done_reason") == "length" else "stop"
            steps.append(LLMResponse(call.get("content"), finish, model, int(call.get("prompt_eval_count") or 0),
                                     int(call.get("eval_count") or 0), 0, int(call.get("latency_ms") or 0)))
    return steps


# --------------------------------------------------------------------------
# Picture of the grid
# --------------------------------------------------------------------------


def draw_overlay(page: Page, grid: PageGrid | None, mapping: dict[int, Any] | None, source_path: str | None,
                 native: bool, out_path: Path) -> None:
    """PNG of the page (or, for OCR pages, of its word boxes) with the detected amount columns drawn on it."""
    zoom = 1.5
    w, h = int(page.width * zoom), int(page.height * zoom)
    image = None
    if native and source_path and str(source_path).lower().endswith(".pdf"):
        import fitz

        with fitz.open(source_path) as doc:
            p = doc[page.page_number - 1]
            if not p.rotation:
                pix = p.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                image = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1].copy()
    if image is None:
        image = np.full((h, w, 3), 255, dtype=np.uint8)
        for block in page.blocks:
            for _text, x0, x1 in block.word_spans():
                cv2.rectangle(image, (int(x0 * zoom), int(block.y0 * zoom)), (int(x1 * zoom), int(block.y1 * zoom)), (170, 170, 170), 1)
    if grid is not None:
        y0, y1 = int(grid.region[0] * zoom), int(grid.region[1] * zoom)
        layer = image.copy()
        for i, band in enumerate(grid.bands):
            col = (mapping or {}).get(i)
            colour = ROLE_COLOURS.get(col.key if col is not None else "", (150, 150, 150))
            cv2.rectangle(layer, (int(band.x0 * zoom), y0), (int(band.x1 * zoom), y1), colour, -1)
        image = cv2.addWeighted(layer, 0.25, image, 0.75, 0)
        for i, band in enumerate(grid.bands):
            col = (mapping or {}).get(i)
            colour = ROLE_COLOURS.get(col.key if col is not None else "", (120, 120, 120))
            cv2.rectangle(image, (int(band.x0 * zoom), y0), (int(band.x1 * zoom), y1), colour, 2)
            cv2.line(image, (int(band.right_edge * zoom), y0), (int(band.right_edge * zoom), y1), colour, 1)
            label = col.key if col is not None else "unmatched"
            cv2.putText(image, label, (int(band.x0 * zoom), max(12, y0 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)
        for x in grid.rules_x:
            cv2.line(image, (int(x * zoom), y0), (int(x * zoom), y1), (0, 0, 0), 2)
        cv2.rectangle(image, (2, y0), (w - 3, y1), (200, 120, 0), 1)
    ok, data = cv2.imencode(".png", image)
    if ok:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data.tobytes())
