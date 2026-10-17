"""Layout / structure analysis (Phase 4).

The layout model is wrapped behind `StructureAnalyzer`; no model-specific data
structure ever reaches the API or the UI (implementation plan Phase 4).

Two implementations satisfy the interface:

* `PPStructureV3Analyzer` - the full PaddleOCR 3.x `PPStructureV3` pipeline.
  The most detailed regions, ~84s per page.
* `PaddleLayoutAnalyzer` - PP-DocLayout_plus-L, PP-StructureV3's layout module
  on its own, driven through `LayoutDetection`. ~7s per page, coarser regions.
* `GeometricStructureAnalyzer` - deterministic classification of header,
  key-value and summary regions from block positions and anchors. Sub-millisecond,
  and the active adapter for POC V1.

Both Paddle adapters need `enable_mkldnn=False` on Windows CPU, and
PP-StructureV3 additionally needs its text detector's input capped - see
`PPStructureV3Analyzer._predict` for why, and the README for the measurements.

Select with `STRUCTURE_ENGINE=geometric|layout|ppstructure|auto`.
"""
from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger
from app.rules.common import contains_anchor
from app.services.document_model import DocumentContent, Page, StructuredDocument

logger = get_logger(__name__)

_SUMMARY_ANCHORS = (
    "total",
    "grand total",
    "amount payable",
    "net amount",
    "net payable",
    "balance due",
    "subtotal",
    "sub total",
)

_KEY_VALUE_HINT = (":", "-")

#: PP-DocLayout labels -> the vocabulary the extraction engine understands.
_PP_LABEL_MAP = {
    "doc_title": "header",
    "header": "header",
    "paragraph_title": "title",
    "figure_title": "title",
    "table_title": "title",
    "table": "table",
    "footer": "footer",
}


class StructureAnalyzer:
    """Interface used by the pipeline."""

    model = "none"

    def analyze(self, content: DocumentContent) -> StructuredDocument:  # pragma: no cover
        raise NotImplementedError


def _mark_summary_regions(page: Page) -> list[str]:
    """Totals/summary detection from anchors.

    Layout models label structure (title, text, table) but not *meaning*, and
    the amount ranking needs to know which lines belong to the totals block -
    so this runs for both analyzers.
    """
    summary_ids: list[str] = []
    for block in page.blocks:
        lowered = block.text.lower()
        if any(contains_anchor(lowered, anchor) for anchor in _SUMMARY_ANCHORS):
            block.region_type = "summary"
            summary_ids.append(block.id)
    return summary_ids


class GeometricStructureAnalyzer(StructureAnalyzer):
    """Deterministic layout analysis from block geometry and anchors."""

    model = "geometric-v1"

    def analyze(self, content: DocumentContent) -> StructuredDocument:
        header_ids: list[str] = []
        summary_ids: list[str] = []

        for page in content.pages:
            if not page.blocks:
                continue
            header_cutoff = page.height * 0.22
            for block in page.blocks:
                if block.y0 <= header_cutoff:
                    block.region_type = "header"
                    header_ids.append(block.id)
                elif any(hint in block.text for hint in _KEY_VALUE_HINT):
                    block.region_type = "key_value"
                else:
                    block.region_type = "text"
            # Summary wins over the positional guess.
            summary_ids.extend(_mark_summary_regions(page))
            header_ids = [b for b in header_ids if b not in set(summary_ids)]

        logger.info(
            "structure analysed: model=%s header=%s summary=%s",
            self.model,
            len(header_ids),
            len(summary_ids),
        )
        return StructuredDocument(
            content=content,
            model=self.model,
            header_block_ids=header_ids,
            summary_block_ids=summary_ids,
        )


class _PaddleAnalyzer(StructureAnalyzer):
    """Shared plumbing for the Paddle-backed analyzers.

    Only the region *classification* is taken from the model - the normalized
    document model, extraction and UI never see PaddleX structures.
    """

    model = "paddle"

    #: Rendering DPI for PDF pages handed to the layout model.
    render_dpi = 150

    def __init__(self) -> None:
        self._pipeline = None

    def available(self) -> bool:
        try:
            import paddleocr  # noqa: F401
        except Exception:
            return False
        return True

    def _get_pipeline(self):  # pragma: no cover - subclass responsibility
        raise NotImplementedError

    def _predict(self, pipeline, image):  # pragma: no cover
        raise NotImplementedError

    def analyze(self, content: DocumentContent) -> StructuredDocument:
        header_ids: list[str] = []
        summary_ids: list[str] = []

        source = Path(content.source_path) if content.source_path else None
        if source is None or not source.exists():
            logger.warning("%s needs the original file; falling back", self.model)
            return GeometricStructureAnalyzer().analyze(content)

        try:
            pipeline = self._get_pipeline()
        except Exception:
            logger.exception("%s could not be initialised; falling back", self.model)
            return GeometricStructureAnalyzer().analyze(content)

        for page in content.pages:
            try:
                regions, scale_x, scale_y = self._regions_for(pipeline, source, page)
            except Exception:
                logger.exception("%s failed on page %s", self.model, page.page_number)
                continue

            for label, (rx0, ry0, rx1, ry1) in regions:
                region_type = _PP_LABEL_MAP.get(label, "text")
                box = (rx0 * scale_x, ry0 * scale_y, rx1 * scale_x, ry1 * scale_y)
                for block in page.blocks:
                    if not _overlaps(block.bbox, box):
                        continue
                    block.region_type = region_type
                    if region_type == "header":
                        header_ids.append(block.id)
                    elif region_type in ("table", "footer"):
                        summary_ids.append(block.id)

            for block in page.blocks:
                if block.region_type is None:
                    block.region_type = "text"
            summary_ids.extend(_mark_summary_regions(page))

        logger.info(
            "structure analysed: model=%s header=%s summary=%s",
            self.model,
            len(header_ids),
            len(summary_ids),
        )
        return StructuredDocument(
            content=content,
            model=self.model,
            header_block_ids=header_ids,
            summary_block_ids=summary_ids,
        )

    def _regions_for(self, pipeline, source: Path, page: Page):
        """Run the layout model for one page and return regions + coord scaling.

        Model coordinates are in rendered-image pixels; the document model uses
        the page's own space (PDF points, or image pixels for an upload), so
        they are scaled back before anything is stored.
        """
        if source.suffix.lower() == ".pdf":
            import fitz

            with fitz.open(source) as doc:
                pix = doc[page.page_number - 1].get_pixmap(dpi=self.render_dpi)
                image_bytes = pix.tobytes("png")
                image_width, image_height = pix.width, pix.height

            import io

            import numpy as np
            from PIL import Image

            array = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
            output = list(self._predict(pipeline, array))
        else:
            from PIL import Image

            with Image.open(source) as image:
                image_width, image_height = image.width, image.height
            output = list(self._predict(pipeline, str(source)))

        regions: list[tuple[str, tuple[float, float, float, float]]] = []
        for result in output:
            payload = result.json.get("res", {}) if hasattr(result, "json") else {}
            # `LayoutDetection` returns the boxes directly; the full
            # PP-StructureV3 pipeline nests them under "layout_det_res".
            boxes = payload.get("boxes")
            if boxes is None:
                boxes = payload.get("layout_det_res", {}).get("boxes", [])
            for box in boxes:
                coordinate = box.get("coordinate") or []
                if len(coordinate) != 4:
                    continue
                regions.append((str(box.get("label", "")).lower(), tuple(float(v) for v in coordinate)))

        scale_x = page.width / image_width if image_width else 1.0
        scale_y = page.height / image_height if image_height else 1.0
        return regions, scale_x, scale_y


class PPStructureV3Analyzer(_PaddleAnalyzer):
    """The full PP-StructureV3 layout-parsing pipeline.

    Richer than the layout module alone: it also runs region detection and
    refines the layout boxes against the text it finds, which is what picks up
    the small headings a till receipt is made of. Measured on the KFC sample:
    19 regions vs 11, and only this one covers the merchant's name line.
    That costs ~84s per page against ~7s, so it is opt-in.
    """

    model = "PP-StructureV3"

    def _get_pipeline(self):
        if self._pipeline is None:
            from paddleocr import PPStructureV3

            # Recognition sub-pipelines this app has no use for stay off - the
            # text is already in hand from the PDF reader or the OCR service.
            # The general-OCR sub-pipeline cannot be switched off (paddlex 3.7.2
            # runs it unconditionally); it is capped instead, see _predict.
            # enable_mkldnn=False avoids a oneDNN/PIR crash in paddlepaddle
            # 3.3.1 on Windows CPU.
            self._pipeline = PPStructureV3(
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_seal_recognition=False,
                use_formula_recognition=False,
                use_chart_recognition=False,
                use_table_recognition=False,
                enable_mkldnn=settings.paddle_enable_mkldnn,
            )
        return self._pipeline

    def _predict(self, pipeline, image):
        # PP-StructureV3 ships `limit_type: min` for text detection, i.e. never
        # downscale, to keep small print legible. PP-OCRv5_server_det then gets
        # the page at full resolution and, in paddlepaddle 3.3.1 on Windows CPU,
        # dies with an access violation above ~1 MP - measured: 1152x800 fine,
        # 1280x896 segfaults, which takes the whole worker process down.
        # Capping the long side keeps it under that ceiling. The detections
        # themselves are only used to refine the layout boxes; the text this
        # app extracts comes from the OCR service, at full resolution.
        return pipeline.predict(
            input=image,
            text_det_limit_type="max",
            text_det_limit_side_len=settings.paddle_text_det_max_side,
        )


class PaddleLayoutAnalyzer(_PaddleAnalyzer):
    """PP-StructureV3's layout-detection module on its own.

    An order of magnitude faster than the full pipeline (~7s vs ~84s per page)
    because it loads and runs one model instead of five, at the cost of the
    finer regions the full pipeline resolves.
    """

    model = "PP-DocLayout_plus-L"

    def _get_pipeline(self):
        if self._pipeline is None:
            from paddleocr import LayoutDetection

            self._pipeline = LayoutDetection(
                model_name=self.model,
                enable_mkldnn=settings.paddle_enable_mkldnn,
            )
        return self._pipeline

    def _predict(self, pipeline, image):
        return pipeline.predict(image, layout_nms=True)


def _overlaps(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def get_structure_analyzer() -> StructureAnalyzer:
    """Resolve the configured analyzer, falling back when one is unavailable."""
    engine = settings.structure_engine.lower()

    if engine in ("ppstructure", "pp-structurev3", "layout", "auto"):
        # "auto" prefers the cheap layout module; PP-StructureV3 is asked for
        # by name because it costs ~12x more per page.
        adapter: _PaddleAnalyzer = (
            PaddleLayoutAnalyzer()
            if engine in ("layout", "auto")
            else PPStructureV3Analyzer()
        )
        if adapter.available():
            logger.info("structure engine: %s", adapter.model)
            return adapter
        if engine != "auto":
            logger.warning(
                "STRUCTURE_ENGINE=%s but PaddleOCR is not installed; using the "
                "geometric analyzer",
                engine,
            )

    return GeometricStructureAnalyzer()


structure_analyzer = get_structure_analyzer()
