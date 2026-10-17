"""Numbered source lines: the document as the language model sees it.

Each visual line of each page gets a stable ID (``p3.l17`` = page 3, line 17).
Text is laid out by horizontal position (like ``pdftotext -layout``): a value
starts at the character column matching where it sits on the page, so an
amount under "Credit" lines up with the "Credit" heading even when the "Debit"
cell of that row is empty. Separate cells are always at least two spaces apart.

The model cites these IDs for every value, which is what lets every value be
checked against the original text afterwards, and lets the reviewer jump to it.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.services.document_model import Block, DocumentContent

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class SourceLine:
    id: str
    page: int
    number: int
    text: str
    bbox: BBox
    source: str = "pdf_text"


def _cluster(blocks: list[Block]) -> list[list[Block]]:
    """Group blocks whose vertical extents overlap into visual lines."""
    lines: list[list[Block]] = []
    for block in sorted(blocks, key=lambda b: (b.y0 + b.y1) / 2):
        centre = (block.y0 + block.y1) / 2
        if lines:
            current = lines[-1]
            top = min(b.y0 for b in current)
            bottom = max(b.y1 for b in current)
            if top - 1 <= centre <= bottom + 1:
                current.append(block)
                continue
        lines.append([block])
    return lines


#: Character columns across the width of a page.
LAYOUT_COLUMNS = 150


def _join(blocks: list[Block], unit: float, left: float) -> str:
    """Visual layout, left to right (right-to-left cells keep their own reading order)."""
    out = ""
    previous: Block | None = None
    for block in sorted(blocks, key=lambda b: b.x0):
        text = block.text.strip()
        if not text:
            continue
        column = max(0, int(round((block.x0 - left) / unit)))
        if previous is not None:
            char_width = max((previous.x1 - previous.x0) / max(len(previous.text), 1), 2.0)
            minimum = 2 if block.x0 - previous.x1 > 1.8 * char_width else 1
            out += " " * max(column - len(out), minimum)
        else:
            out += " " * column
        out += text
        previous = block
    return out.rstrip()


def document_lines(content: DocumentContent) -> list[SourceLine]:
    out: list[SourceLine] = []
    for page in content.pages:
        number = 0
        blocks = [b for b in page.blocks if b.text.strip()]
        if not blocks:
            continue
        unit = max(page.width, 1.0) / LAYOUT_COLUMNS
        left = min(b.x0 for b in blocks)
        for group in _cluster(blocks):
            text = _join(group, unit, left)
            if not text:
                continue
            number += 1
            out.append(SourceLine(
                id=f"p{page.page_number}.l{number}",
                page=page.page_number,
                number=number,
                text=text,
                bbox=(min(b.x0 for b in group), min(b.y0 for b in group), max(b.x1 for b in group), max(b.y1 for b in group)),
                source=group[0].source,
            ))
    return out


def union_bbox(lines: list[SourceLine]) -> BBox:
    return (
        min(line.bbox[0] for line in lines), min(line.bbox[1] for line in lines),
        max(line.bbox[2] for line in lines), max(line.bbox[3] for line in lines),
    )
