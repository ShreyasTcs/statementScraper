"""The statement as Markdown, the input the Groq engine reads by default (``GROQ_INPUT=markdown``).

Built from the existing reading result and table detection; nothing is OCR'd again and no value is normalised:

* the page text is the same ``DocumentContent`` the layout lines come from (PyMuPDF text layer / RapidOCR);
* the transaction table comes from the local table detector (``table_extractor.extract``) run on that content;
* every table cell is the printed text the detector assigned to that column (``TableRow.raw`` / ``extras``);
* every line that is not a transaction row is kept verbatim (only leading indentation is removed, because Markdown
  would turn indented text into code); balance and totals lines the detector recognised stay outside the table;
* a line inside the table area that did not become a row goes to "Other text (not placed in the table)".

Each Markdown line becomes a numbered source line (``pN.lM``), so the extractor can cite it and the verification can
find the printed values again. ``printed_lines`` maps every Markdown line to the layout lines it was made from: two
Markdown lines that share a layout line hold the same printed text.

A token check makes sure nothing is lost: every whitespace-separated token of every layout line must appear in the
Markdown of its page (as a multiset). A row whose printed text is not fully in its cells is also kept verbatim under
"Other text". If anything is still missing, ``faithful`` is false and the caller falls back to the layout lines.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from app.services.document_model import DocumentContent, StructuredDocument
from app.services.page_text import SourceLine, document_lines
from app.services.table_service import Conventions, TableResult, TableRow, table_extractor

COLUMN_ORDER = ["date", "value_date", "description", "reference", "debit", "credit", "amount", "drcr", "balance"]
DEFAULT_LABELS = {
    "date": "Date", "value_date": "Value date", "description": "Description", "reference": "Reference",
    "debit": "Debit", "credit": "Credit", "amount": "Amount", "drcr": "Dr/Cr", "balance": "Balance",
}
#: What a printed column heading means, for the reader notes.
COLUMN_MEANING = {
    "date": "transaction date", "value_date": "value date", "description": "description", "reference": "reference",
    "debit": "debit (money out)", "credit": "credit (money in)", "amount": "single amount column (signed or marked)",
    "drcr": "debit/credit marker", "balance": "running balance",
}
BALANCE_MEANING = {"opening": "the opening balance", "closing": "the closing balance", "balance": "a balance"}
OTHER_HEADING = "### Other text (not placed in the table)"
BACKSLASH = chr(92)


@dataclass
class ReaderNotes:
    """What the layout reader worked out, shown to the model as hints (the statement text decides)."""
    general: list[str] = field(default_factory=list)
    by_line: dict[str, str] = field(default_factory=dict)

    def for_lines(self, line_ids: list[str]) -> list[str]:
        return self.general + [f"{i}: {self.by_line[i]}" for i in line_ids if i in self.by_line]


@dataclass
class MarkdownView:
    markdown: str
    lines: list[SourceLine]
    printed_lines: dict[str, tuple[str, ...]]
    notes: ReaderNotes
    warnings: list[dict[str, Any]]
    stats: dict[str, Any]
    faithful: bool


@dataclass
class _Element:
    kind: str            # row | balance | totals
    page: int
    bbox: tuple[float, float, float, float]
    index: int           # position in the detector's list
    row: TableRow | None = None
    detail: str = ""     # balance kind: opening | closing | balance


@dataclass
class _PageOut:
    page: int
    items: list[dict[str, Any]] = field(default_factory=list)   # {"text", "kind", "direct", "bbox", "detail"}


def _tokens(text: str) -> list[str]:
    return [t for t in text.split() if t]


def _cell(text: str | None) -> tuple[str, bool]:
    value = " ".join((text or "").split())
    return value.replace("|", BACKSLASH + "|"), "|" in value


def _item_tokens(item: dict[str, Any]) -> list[str]:
    """Printed tokens of one Markdown item: table cells are split on unescaped '|', the header rule carries none."""
    if item["kind"] == "table_separator":
        return []
    if item["kind"] in ("table_row", "table_header"):
        inner = item["text"].strip()[1:-1].replace(BACKSLASH + "|", chr(0))
        return [t.replace(chr(0), "|") for cell in inner.split("|") for t in cell.split()]
    return item["text"].split()


def _overlap(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return max(0.0, min(a[3], b[3]) - max(a[1], b[1]))


def _assign(line: SourceLine, elements: list[_Element]) -> _Element | None:
    """The table element a layout line belongs to: same page, at least half of the line's height inside it."""
    height = max(line.bbox[3] - line.bbox[1], 0.1)
    best, best_overlap = None, 0.0
    for element in elements:
        if element.page != line.page:
            continue
        overlap = _overlap(line.bbox, element.bbox)
        if overlap / height >= 0.5 and overlap > best_overlap:
            best, best_overlap = element, overlap
    return best


def _columns(table: TableResult) -> tuple[list[str], dict[str, str], list[str]]:
    used: list[str] = []
    for row in table.rows:
        for key, value in list(row.raw.items()) + list(row.extras.items()):
            if value and value.strip() and key not in used:
                used.append(key)
    ordered = [k for k in COLUMN_ORDER if k in used] + sorted(k for k in used if k not in COLUMN_ORDER)
    printed = {} if table.inferred_columns else {
        c.key: c.printed_as for c in table.columns if c.found and c.printed_as and not c.printed_as.startswith("(")}
    labels, unprinted = {}, []
    for key in ordered:
        if key in printed:
            labels[key] = " ".join(printed[key].split())
        else:
            labels[key] = DEFAULT_LABELS.get(key, "Other column " + key.replace("extra_", ""))
            unprinted.append(key)
    return ordered, labels, unprinted


def _notes(conv: Conventions, keys: list[str], labels: dict[str, str], unprinted: list[str],
           balance_lines: dict[str, str], totals_lines: list[str]) -> ReaderNotes:
    notes = ReaderNotes()
    if conv.number.confident:
        group = f' and "{conv.number.group}" to group thousands' if conv.number.group else ""
        notes.general.append(f'Amounts use "{conv.number.decimal}" as the decimal separator{group}.')
    meanings = [f'"{labels[k]}" = {COLUMN_MEANING[k]}' for k in keys if k not in unprinted and k in COLUMN_MEANING]
    if meanings:
        notes.general.append("Table column headings: " + "; ".join(meanings) + ".")
    for line_id, kind in balance_lines.items():
        notes.by_line[line_id] = f"read as {BALANCE_MEANING.get(kind, 'a balance')}, not a transaction."
    for line_id in totals_lines:
        notes.by_line[line_id] = "read as printed totals, not a transaction."
    return notes


def build_markdown(content: DocumentContent, structured: StructuredDocument, conv: Conventions,
                   table: TableResult | None = None) -> MarkdownView:
    direct = document_lines(content)
    table = table if table is not None else table_extractor.extract(structured, conv)
    warnings: list[dict[str, Any]] = []

    def warn(code: str, page: int | None = None, count: int = 0) -> None:
        warnings.append({"code": code, "page": page, "count": count})

    elements = [_Element("row", r.page, r.bbox, i, row=r) for i, r in enumerate(table.rows)]
    elements += [_Element("balance", b.page, b.bbox, i, detail=b.kind) for i, b in enumerate(table.balance_rows)]
    elements += [_Element("totals", t.page, t.bbox, i) for i, t in enumerate(table.totals_rows)]
    unresolved_boxes = [(u.page, u.bbox) for u in table.unresolved]
    keys, labels, unprinted = _columns(table)

    by_page: dict[int, list[SourceLine]] = {}
    for line in direct:
        by_page.setdefault(line.page, []).append(line)
    row_cells: dict[int, list[str]] = {}
    for i, row in enumerate(table.rows):
        row_cells[i] = [_cell(row.raw.get(key) if key in row.raw else row.extras.get(key))[0] for key in keys]

    pages_out: list[_PageOut] = []
    for page in content.pages:
        number = page.page_number
        out = _PageOut(number)
        pages_out.append(out)
        plines = by_page.get(number, [])
        if not plines:
            continue
        assigned = {line.id: _assign(line, elements) for line in plines}
        row_lines = [l for l in plines if assigned[l.id] is not None and assigned[l.id].kind == "row"]
        span = (min(l.bbox[1] for l in row_lines), max(l.bbox[3] for l in row_lines)) if row_lines else None
        other: list[SourceLine] = []
        emitted_rows: set[int] = set()
        table_open = False
        first_row_bbox = row_lines[0].bbox if row_lines else plines[0].bbox

        def open_table() -> None:
            out.items.append({"kind": "table_header", "text": "| " + " | ".join(labels[k] for k in keys) + " |",
                              "direct": [], "bbox": first_row_bbox})
            out.items.append({"kind": "table_separator", "text": "|" + "|".join("---" for _ in keys) + "|",
                              "direct": [], "bbox": first_row_bbox})

        for line in plines:
            element = assigned[line.id]
            centre = (line.bbox[1] + line.bbox[3]) / 2
            if element is not None and element.kind == "row":
                if element.index in emitted_rows:
                    continue
                if not table_open:
                    open_table()
                    table_open = True
                emitted_rows.add(element.index)
                members = [l for l in plines if assigned[l.id] is element]
                out.items.append({"kind": "table_row", "text": "| " + " | ".join(row_cells[element.index]) + " |",
                                  "direct": [l.id for l in members], "bbox": element.bbox})
                continue
            if element is not None:          # an opening/closing/total line printed inside the table
                table_open = False
                out.items.append({"kind": element.kind + "_line", "text": line.text.strip(), "direct": [line.id],
                                  "bbox": line.bbox, "detail": element.detail})
                continue
            unresolved = any(p == number and _overlap(line.bbox, b) / max(line.bbox[3] - line.bbox[1], 0.1) >= 0.5
                             for p, b in unresolved_boxes)
            if unresolved or (span is not None and span[0] <= centre <= span[1]):
                other.append(line)
                continue
            table_open = False
            out.items.append({"kind": "text", "text": line.text.strip(), "direct": [line.id], "bbox": line.bbox})

        # Rows whose printed text is not fully inside their cells are kept verbatim as well.
        for item in [i for i in out.items if i["kind"] == "table_row"]:
            members = [l for l in plines if l.id in item["direct"]]
            missing = Counter(t for l in members for t in _tokens(l.text)) - Counter(_item_tokens(item))
            if missing:
                other.extend(m for m in members if m not in other)
                warn("row_text_not_fully_in_cells", number, sum(missing.values()))
        if other:
            other.sort(key=lambda l: l.number)
            out.items.append({"kind": "other_heading", "text": OTHER_HEADING, "direct": [], "bbox": other[0].bbox})
            for line in other:
                out.items.append({"kind": "other_text", "text": line.text.strip(), "direct": [line.id], "bbox": line.bbox})
            warn("unplaced_lines", number, len(other))

    # Number the Markdown lines per page.
    md_lines: list[str] = []
    lines: list[SourceLine] = []
    printed_lines: dict[str, tuple[str, ...]] = {}
    balance_lines: dict[str, str] = {}
    totals_lines: list[str] = []
    source_of = {l.id: l.source for l in direct}
    for out in pages_out:
        heading = f"## Page {out.page}"
        page_items = [{"kind": "page_heading", "text": heading, "direct": [],
                       "bbox": out.items[0]["bbox"] if out.items else (0.0, 0.0, 0.0, 0.0)}] + out.items
        if md_lines:
            md_lines.append("")
        page_source = next((source_of[i] for item in out.items for i in item["direct"]), "pdf_text")
        for n, item in enumerate(page_items, start=1):
            md_lines.append(item["text"])
            line_id = f"p{out.page}.l{n}"
            lines.append(SourceLine(id=line_id, page=out.page, number=n, text=item["text"],
                                    bbox=tuple(item["bbox"]), source=page_source))
            printed_lines[line_id] = tuple(item["direct"])
            if item["kind"] == "balance_line":
                balance_lines[line_id] = item.get("detail") or "balance"
            elif item["kind"] == "totals_line":
                totals_lines.append(line_id)

    # Faithfulness: every token of every layout line must be present in its page's Markdown.
    lost = 0
    for out in pages_out:
        page_md = Counter(t for item in out.items for t in _item_tokens(item))
        page_direct = Counter(t for l in by_page.get(out.page, []) for t in _tokens(l.text))
        missing = sum((page_direct - page_md).values())
        if missing:
            lost += missing
            warn("content_lost", out.page, missing)

    kinds = Counter(item["kind"] for out in pages_out for item in out.items)
    stats = {"layout_lines": len(direct), "markdown_lines": len(lines), "table_rows": kinds.get("table_row", 0),
             "balance_or_total_lines": kinds.get("balance_line", 0) + kinds.get("totals_line", 0),
             "other_text_lines": kinds.get("other_text", 0), "tokens_lost": lost}
    return MarkdownView(
        markdown="\n".join(md_lines) + "\n", lines=lines, printed_lines=printed_lines,
        notes=_notes(conv, keys, labels, unprinted, balance_lines, totals_lines),
        warnings=warnings, stats=stats, faithful=lost == 0 and bool(direct),
    )
