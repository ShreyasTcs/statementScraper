"""Pipeline B's input: the statement as Markdown, built from the existing reading result and table detection.

Nothing is OCR'd again and no value is normalised:

* the page text is the same ``DocumentContent`` Pipeline A reads (PyMuPDF text layer / RapidOCR);
* the transaction table comes from the existing table detector (``table_extractor.extract``) run on that content;
* every table cell is the printed text the detector assigned to that column (``TableRow.raw`` / ``extras``);
* every line that is not a transaction row is kept verbatim (only leading indentation is removed, because Markdown
  would turn indented text into code);
* a line inside the table area that did not become a row goes to "Other text (not placed in the table)", with a warning.

Each Markdown line becomes a numbered source line (``pN.lM``) with page, table-row and original-line provenance, so
the unchanged extractor can cite it and the unchanged verification can find the printed values again.

A token check makes sure nothing is lost: every whitespace-separated token of every direct layout line must appear in
the Markdown of its page (as a multiset). A row whose printed text is not fully in its cells is also kept verbatim in
"Other text"; anything still missing is reported as lost content.
"""
from __future__ import annotations

import time
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
OTHER_HEADING = "### Other text (not placed in the table)"
BACKSLASH = chr(92)


@dataclass
class MarkdownView:
    markdown: str
    lines: list[SourceLine]
    provenance: list[dict[str, Any]]
    warnings: list[dict[str, Any]]
    stats: dict[str, Any]
    columns: list[dict[str, Any]]
    duration_ms: int
    faithful: bool


@dataclass
class _Element:
    kind: str            # row | balance | totals
    page: int
    bbox: tuple[float, float, float, float]
    index: int           # position in the detector's list
    row: TableRow | None = None
    text: str = ""


@dataclass
class _PageOut:
    page: int
    items: list[dict[str, Any]] = field(default_factory=list)   # {"text", "kind", "direct", "row", "bbox"}


def _tokens(text: str) -> list[str]:
    return [t for t in text.split() if t]


def _cell(text: str | None) -> tuple[str, bool]:
    value = " ".join((text or "").split())
    return value.replace("|", "\\|"), "|" in value


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
    """The table element a direct line belongs to: same page, at least half of the line's height inside it."""
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


def build_markdown(content: DocumentContent, structured: StructuredDocument, conv: Conventions) -> MarkdownView:
    started = time.perf_counter()
    direct = document_lines(content)
    table = table_extractor.extract(structured, conv)
    warnings: list[dict[str, Any]] = []

    def warn(code: str, text: str, page: int | None = None, lines: list[str] | None = None, severity: str = "warning") -> None:
        warnings.append({"code": code, "severity": severity, "page": page, "message": text, "direct_line_ids": lines or []})

    elements = [_Element("row", r.page, r.bbox, i, row=r) for i, r in enumerate(table.rows)]
    elements += [_Element("balance", b.page, b.bbox, i, text=b.text) for i, b in enumerate(table.balance_rows)]
    elements += [_Element("totals", t.page, t.bbox, i, text=t.text) for i, t in enumerate(table.totals_rows)]
    unresolved_boxes = [(u.page, u.bbox) for u in table.unresolved]
    keys, labels, unprinted = _columns(table)
    escaped_pipes = 0

    if not table.rows:
        warn("no_table_detected", "The table detector found no transaction rows; every line is kept as text.")
    if table.inferred_columns:
        warn("columns_inferred", "No column heading was recognised; the table detector inferred the columns from the "
             "content, so the Markdown column labels are the detector's, not printed ones.")
    if unprinted:
        warn("column_labels_not_printed", "Column labels not printed in the statement (detector names used): "
             + ", ".join(labels[k] for k in unprinted) + ".")

    by_page: dict[int, list[SourceLine]] = {}
    for line in direct:
        by_page.setdefault(line.page, []).append(line)
    row_cells: dict[int, list[str]] = {}
    for i, row in enumerate(table.rows):
        cells = []
        for key in keys:
            value, had_pipe = _cell(row.raw.get(key) if key in row.raw else row.extras.get(key))
            escaped_pipes += had_pipe
            cells.append(value)
        row_cells[i] = cells

    pages_out: list[_PageOut] = []
    for page in content.pages:
        number = page.page_number
        out = _PageOut(number)
        pages_out.append(out)
        plines = by_page.get(number, [])
        if not plines:
            warn("page_without_text", f"Page {number} has no readable text (nothing to convert).", page=number)
            continue
        assigned = {line.id: _assign(line, elements) for line in plines}
        row_lines = [l for l in plines if assigned[l.id] is not None and assigned[l.id].kind == "row"]
        span = (min(l.bbox[1] for l in row_lines), max(l.bbox[3] for l in row_lines)) if row_lines else None
        if row_lines and number not in table.header_pages and table.header_pages:
            warn("header_not_on_page", f"No column heading is printed on page {number}; the Markdown table repeats the "
                 "heading detected on another page.", page=number)
        other: list[SourceLine] = []
        emitted_rows: set[int] = set()
        table_open = False
        first_row_bbox = row_lines[0].bbox if row_lines else plines[0].bbox

        def open_table() -> None:
            out.items.append({"kind": "table_header", "text": "| " + " | ".join(labels[k] for k in keys) + " |",
                              "direct": [], "row": None, "bbox": first_row_bbox})
            out.items.append({"kind": "table_separator", "text": "|" + "|".join("---" for _ in keys) + "|",
                              "direct": [], "row": None, "bbox": first_row_bbox})

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
                                  "direct": [l.id for l in members], "row": element.row.index, "bbox": element.bbox})
                continue
            if element is not None:          # an opening/closing/total line printed inside the table
                table_open = False
                out.items.append({"kind": element.kind + "_line", "text": line.text.strip(), "direct": [line.id],
                                  "row": None, "bbox": line.bbox})
                continue
            unresolved = any(p == number and _overlap(line.bbox, b) / max(line.bbox[3] - line.bbox[1], 0.1) >= 0.5
                             for p, b in unresolved_boxes)
            if unresolved or (span is not None and span[0] <= centre <= span[1]):
                other.append(line)
                continue
            table_open = False
            out.items.append({"kind": "text", "text": line.text.strip(), "direct": [line.id], "row": None, "bbox": line.bbox})

        # Rows whose printed text is not fully inside their cells are kept verbatim as well.
        for item in [i for i in out.items if i["kind"] == "table_row"]:
            members = [l for l in plines if l.id in item["direct"]]
            missing = Counter(t for l in members for t in _tokens(l.text)) - Counter(_item_tokens(item))
            if missing:
                other.extend(m for m in members if m not in other)
                warn("row_text_not_fully_in_cells", f"A table row on page {number} has printed text the detector did "
                     f"not place in a column ({sum(missing.values())} token(s)); its lines are also kept verbatim in "
                     "Other text.", page=number, lines=item["direct"])
        if other:
            other.sort(key=lambda l: l.number)
            out.items.append({"kind": "other_heading", "text": OTHER_HEADING, "direct": [], "row": None, "bbox": other[0].bbox})
            for line in other:
                out.items.append({"kind": "other_text", "text": line.text.strip(), "direct": [line.id], "row": None, "bbox": line.bbox})
            reason = [l.id for l in other]
            warn("unplaced_lines", f"{len(other)} line(s) on page {number} are inside the table area or look like "
                 "transactions but did not become table rows; they are kept verbatim under Other text.",
                 page=number, lines=reason)

    if escaped_pipes:
        warn("pipe_escaped", f"{escaped_pipes} cell(s) contained a '|' character, written as '\\|' in Markdown.")

    # Number the Markdown lines per page and build the source lines Pipeline B's extractor reads.
    md_lines: list[str] = []
    lines: list[SourceLine] = []
    provenance: list[dict[str, Any]] = []
    source_of = {l.id: l.source for l in direct}
    for out in pages_out:
        heading = f"## Page {out.page}"
        page_items = [{"kind": "page_heading", "text": heading, "direct": [], "row": None,
                       "bbox": out.items[0]["bbox"] if out.items else (0.0, 0.0, 0.0, 0.0)}] + out.items
        if md_lines:
            md_lines.append("")
        page_source = next((source_of[i] for item in out.items for i in item["direct"]), "pdf_text")
        for n, item in enumerate(page_items, start=1):
            md_lines.append(item["text"])
            line_id = f"p{out.page}.l{n}"
            lines.append(SourceLine(id=line_id, page=out.page, number=n, text=item["text"],
                                    bbox=tuple(item["bbox"]), source=page_source))
            provenance.append({"id": line_id, "page": out.page, "kind": item["kind"], "table_row": item["row"],
                               "direct_line_ids": item["direct"], "bbox": [round(v, 1) for v in item["bbox"]]})

    # Faithfulness: every token of every direct line must be present in its page's Markdown.
    lost: list[dict[str, Any]] = []
    tokens_checked = 0
    for out in pages_out:
        page_md = Counter(t for item in out.items for t in _item_tokens(item))
        page_direct = Counter(t for l in by_page.get(out.page, []) for t in _tokens(l.text))
        tokens_checked += sum(page_direct.values())
        missing = page_direct - page_md
        if missing:
            lost.append({"page": out.page, "missing_tokens": sum(missing.values())})
            warn("content_lost", f"{sum(missing.values())} printed token(s) on page {out.page} are not in the Markdown.",
                 page=out.page, severity="error")

    kinds = Counter(p["kind"] for p in provenance)
    stats = {
        "pages": len(content.pages), "direct_lines": len(direct), "markdown_lines": len(lines),
        "detector_rows": len(table.rows), "table_rows_emitted": kinds.get("table_row", 0),
        "verbatim_text_lines": kinds.get("text", 0), "balance_or_total_lines": kinds.get("balance_line", 0) + kinds.get("totals_line", 0),
        "other_text_lines": kinds.get("other_text", 0), "tokens_checked": tokens_checked,
        "tokens_lost": sum(x["missing_tokens"] for x in lost),
        "direct_chars": sum(len(l.text) for l in direct), "markdown_chars": sum(len(l.text) for l in lines),
    }
    columns = [{"key": k, "label": labels[k], "printed": k not in unprinted} for k in keys]
    return MarkdownView(
        markdown="\n".join(md_lines) + "\n", lines=lines, provenance=provenance, warnings=warnings, stats=stats,
        columns=columns, duration_ms=int((time.perf_counter() - started) * 1000), faithful=not lost,
    )
