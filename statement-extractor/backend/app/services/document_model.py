"""Normalized document model (TRD section 6).

Every text source - PDF text layer or OCR - is normalized into this shape, so
the extraction engine never sees vendor-specific structures. The bboxes are
also what the frontend uses to highlight the source region on the *original*
uploaded document.

Coordinates are stored in PDF/image pixel space for the page they belong to,
alongside that page's width/height, so the viewer can scale them to whatever
zoom the user has chosen.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

BBox = tuple[float, float, float, float]  # x0, y0, x1, y1

TextSource = Literal["pdf_text", "ocr"]


@dataclass
class Block:
    """One line/region of text located on a page."""

    id: str
    text: str
    bbox: BBox
    source: TextSource
    confidence: float | None = None
    #: Populated by the structure analyzer: title | table | text | key_value ...
    region_type: str | None = None
    #: Point size of the largest span on the line. Merchant names are set in
    #: larger type than the rest of a page, which makes this a useful signal.
    font_size: float | None = None
    #: (text, x0, x1) per word. Exact for PDF text; empty for OCR, where
    #: `word_spans` estimates positions from character counts.
    words: list[tuple[str, float, float]] = field(default_factory=list)

    def word_spans(self) -> list[tuple[str, float, float]]:
        """Words with x-extents, so a fused cell can be split between columns."""
        if self.words:
            return self.words
        pieces = self.text.split()
        if not pieces:
            return []
        total = sum(len(p) for p in pieces) + (len(pieces) - 1)
        width = self.x1 - self.x0
        spans, cursor = [], 0
        for piece in pieces:
            start = self.x0 + width * cursor / total
            cursor += len(piece)
            spans.append((piece, start, self.x0 + width * cursor / total))
            cursor += 1
        return spans

    @property
    def x0(self) -> float:
        return self.bbox[0]

    @property
    def y0(self) -> float:
        return self.bbox[1]

    @property
    def x1(self) -> float:
        return self.bbox[2]

    @property
    def y1(self) -> float:
        return self.bbox[3]

    @property
    def center(self) -> tuple[float, float]:
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 1.0)

    #: Rough width-to-height ratio of a character, used to express an OCR
    #: block's prominence on the same scale as a PDF point size.
    _CHAR_ASPECT = 1.8

    @property
    def prominence(self) -> float:
        """Type size of this line.

        PDF blocks carry the real point size. For OCR the bbox *height* is a
        poor proxy: on a photographed till receipt the paper curls, so a long
        body line's polygon is taller than the merchant's name set in double
        width (measured: 97px for the address line vs 53px for the header).
        Average character width does not pick up that skew and still scales
        with the type size, so it is the better estimate.
        """
        if self.font_size:
            return self.font_size
        text = self.text.strip()
        if len(text) >= 3:
            return (self.x1 - self.x0) / len(text) * self._CHAR_ASPECT
        return self.height


@dataclass
class Page:
    page_number: int
    width: float
    height: float
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


@dataclass
class DocumentContent:
    """Normalized text + coordinates for a whole document."""

    document_id: str
    pages: list[Page] = field(default_factory=list)
    source: TextSource = "pdf_text"
    ocr_used: bool = False
    #: Path to the original upload. Layout models work on pixels, so the
    #: structure analyzer needs the file the text was derived from.
    source_path: str | None = None

    @property
    def total_characters(self) -> int:
        return sum(len(b.text) for p in self.pages for b in p.blocks)

    def iter_blocks(self):
        for page in self.pages:
            for block in page.blocks:
                yield page, block


@dataclass
class StructuredDocument:
    """Document content enriched with layout/structure information."""

    content: DocumentContent
    model: str
    #: Blocks the analyzer believes form the document header / merchant area.
    header_block_ids: list[str] = field(default_factory=list)
    #: Blocks belonging to a totals/summary region.
    summary_block_ids: list[str] = field(default_factory=list)

    @property
    def pages(self) -> list[Page]:
        return self.content.pages

    def iter_blocks(self):
        return self.content.iter_blocks()
