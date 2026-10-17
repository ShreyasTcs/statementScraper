"""Direct text extraction from PDFs with a usable text layer.

Text PDFs never go through OCR - PyMuPDF already gives us text plus exact
coordinates, which is both faster and more accurate.

Cells are built from **characters**, not from PyMuPDF's spans or words. A
statement row is often one PDF line whose cells sit a few ems apart, and some
producers (right-to-left pages especially) hand back a label and its far-away
value as a single span, or even a single "word". Each character carries its own
box, so a gap is a gap wherever the producer put it; the text of each cell keeps
the producer's logical order.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from pathlib import Path

import fitz  # PyMuPDF

from app.core.config import settings
from app.core.logging import get_logger
from app.rules.numbers import canonical
from app.services.document_model import Block, DocumentContent, Page

logger = get_logger(__name__)

#: Characters further apart than this many ems belong to different cells.
_CELL_GAP_EMS = 1.2

# (logical index, char, x0, y0, x1, y1, font size)
_Char = tuple[int, str, float, float, float, float, float]


def _normalise(text: str) -> str:
    return canonical(unicodedata.normalize("NFKC", text))


def _dominant_direction(blocks: list[dict]) -> tuple[int, int]:
    """The writing direction most characters follow: (1, 0) is ordinary left-to-right."""
    votes: Counter[tuple[int, int]] = Counter()
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            votes[(round(dx), round(dy))] += sum(len(s.get("chars", [])) for s in line.get("spans", []))
    return votes.most_common(1)[0][0] if votes else (1, 0)


def _upright(direction: tuple[int, int], width: float, height: float):
    """A map from the page's own coordinates to ones where its text reads left to right.

    Statements are sometimes printed landscape by turning the *content* of a
    portrait page; the characters then run bottom-to-top. Also returns the
    size of the upright page.
    """
    if direction == (0, -1):  # reads upwards: turn the page clockwise
        return (lambda x, y: (height - y, x)), (height, width)
    if direction == (0, 1):  # reads downwards: turn it anticlockwise
        return (lambda x, y: (y, width - x)), (height, width)
    if direction == (-1, 0):  # upside down
        return (lambda x, y: (width - x, height - y)), (width, height)
    return (lambda x, y: (x, y)), (width, height)


def _cluster_chars(chars: list[_Char]) -> list[list[_Char]]:
    """Cells of one line: characters chained together by gaps under 1.2 em."""
    groups: list[list[_Char]] = []
    for char in sorted(chars, key=lambda c: c[2]):
        limit = _CELL_GAP_EMS * max(char[6], 1.0)
        for group in groups:
            lo, hi = min(c[2] for c in group), max(c[4] for c in group)
            if char[2] - hi <= limit and lo - char[4] <= limit:
                group.append(char)
                break
        else:
            groups.append([char])
    # A later character can bridge two groups that started apart.
    merged = True
    while merged and len(groups) > 1:
        merged = False
        groups.sort(key=lambda g: min(c[2] for c in g))
        for i in range(len(groups) - 1):
            a, b = groups[i], groups[i + 1]
            limit = _CELL_GAP_EMS * max(max(c[6] for c in a), 1.0)
            if min(c[2] for c in b) - max(c[4] for c in a) <= limit:
                groups[i] = a + b
                del groups[i + 1]
                merged = True
                break
    return groups


def _words_of(group: list[_Char]) -> list[tuple[str, float, float]]:
    """(text, x0, x1) per whitespace-separated word, in left-to-right order."""
    words: list[tuple[str, float, float]] = []
    current: list[_Char] = []
    for char in sorted(group, key=lambda c: c[0]):  # logical order
        if char[1].isspace():
            if current:
                words.append(_word(current))
                current = []
        else:
            current.append(char)
    if current:
        words.append(_word(current))
    return sorted(words, key=lambda w: w[1])


def _word(chars: list[_Char]) -> tuple[str, float, float]:
    text = _normalise("".join(c[1] for c in chars))
    return text, min(c[2] for c in chars), max(c[4] for c in chars)


class TextReader:
    """Reads the selectable text layer of a PDF."""

    def can_read(self, file_path: str | Path) -> bool:
        path = Path(file_path)
        if path.suffix.lower() != ".pdf":
            return False
        try:
            with fitz.open(path) as doc:
                return any(
                    len(page.get_text("text").strip()) >= settings.min_chars_for_text_layer
                    for page in doc
                )
        except Exception:  # pragma: no cover - corrupt file
            return False

    def read(self, file_path: str | Path, document_id: str = "") -> DocumentContent:
        path = Path(file_path)
        pages: list[Page] = []
        with fitz.open(path) as doc:
            for index, page in enumerate(doc, start=1):
                # Text coordinates are in the page's *unrotated* space, so its size is too.
                rect = page.rect * page.derotation_matrix
                raw_blocks = page.get_text("rawdict").get("blocks", [])
                direction = _dominant_direction(raw_blocks)
                to_upright, (page_width, page_height) = _upright(direction, rect.width, rect.height)
                blocks: list[Block] = []
                counter = 0
                for block in raw_blocks:
                    if block.get("type") != 0:  # skip images
                        continue
                    for line in block.get("lines", []):
                        dx, dy = line.get("dir", (1.0, 0.0))
                        if (round(dx), round(dy)) != direction:
                            continue  # a stamp or watermark running another way
                        chars: list[_Char] = []
                        for span in line.get("spans", []):
                            size = float(span.get("size", 0) or 0)
                            for char in span.get("chars", []):
                                cx0, cy0, cx1, cy1 = char["bbox"]
                                (ax, ay), (bx, by) = to_upright(cx0, cy0), to_upright(cx1, cy1)
                                chars.append((len(chars), char["c"], min(ax, bx), min(ay, by), max(ax, bx), max(ay, by), size))
                        for group in _cluster_chars(chars):
                            logical = sorted(group, key=lambda c: c[0])
                            text = _normalise("".join(c[1] for c in logical)).strip()
                            if not text:
                                continue
                            counter += 1
                            blocks.append(
                                Block(
                                    id=f"p{index}-b{counter}",
                                    text=text,
                                    bbox=(
                                        min(c[2] for c in group), min(c[3] for c in group),
                                        max(c[4] for c in group), max(c[5] for c in group),
                                    ),
                                    source="pdf_text",
                                    font_size=max(c[6] for c in group) or None,
                                    words=_words_of(group),
                                )
                            )
                pages.append(Page(page_number=index, width=page_width, height=page_height, blocks=blocks))
        logger.info("pdf text read: pages=%s blocks=%s", len(pages), sum(len(p.blocks) for p in pages))
        return DocumentContent(document_id=document_id, pages=pages, source="pdf_text", ocr_used=False)

    def page_has_text(self, file_path: str | Path, page_number: int) -> bool:
        with fitz.open(Path(file_path)) as doc:
            page = doc[page_number - 1]
            return len(page.get_text("text").strip()) >= settings.min_chars_for_text_layer

    def render_page(self, file_path: str | Path, page_number: int, dpi: int = 200) -> bytes:
        """Rasterise one PDF page so it can be sent to OCR."""
        with fitz.open(Path(file_path)) as doc:
            page = doc[page_number - 1]
            pix = page.get_pixmap(dpi=dpi)
            return pix.tobytes("png")

    def page_size(self, file_path: str | Path, page_number: int) -> tuple[float, float]:
        with fitz.open(Path(file_path)) as doc:
            rect = doc[page_number - 1].rect
            return rect.width, rect.height
