"""OCR for images and scanned PDFs (Phase 3).

OCR stays behind this interface so the backend can be swapped without touching
the pipeline. `OCR_ENGINE` chooses the probe order:

  auto (default) - Tesseract, then RapidOCR, then PaddleOCR
  tesseract      - Tesseract first
  rapidocr       - RapidOCR first (ONNX, pip-installable, ~5 s/page on CPU)
  paddleocr      - PaddleOCR first (PP-OCRv5; much slower on CPU)

When no backend is installed the pipeline degrades to "extraction failure",
keeping the uploaded document visible so the user can type the values manually
(03_app_flow.md section 13).
"""
from __future__ import annotations

import io
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from app.core.config import settings
from app.core.logging import get_logger
from app.rules.numbers import canonical
from app.services.document_model import Block, Page

logger = get_logger(__name__)


class OCRUnavailableError(Exception):
    """No OCR backend is installed in this environment."""


@dataclass
class OCRWord:
    text: str
    bbox: tuple[float, float, float, float]
    confidence: float


class OCRBackend:
    name = "none"

    def available(self) -> bool:
        raise NotImplementedError

    def recognise(self, image: Image.Image) -> list[OCRWord]:
        raise NotImplementedError


class PaddleOCRBackend(OCRBackend):
    """PaddleOCR, supporting both the 2.x and 3.x Python APIs.

    The two are not compatible: 3.x dropped `PaddleOCR(show_log=...)` and
    `engine.ocr(..., cls=True)` in favour of keyword modules and `predict()`.
    """

    name = "paddleocr"

    def __init__(self) -> None:
        self._engine = None

    @staticmethod
    def _version() -> tuple[int, ...]:
        import paddleocr

        parts: list[int] = []
        for chunk in str(getattr(paddleocr, "__version__", "0")).split("."):
            digits = "".join(ch for ch in chunk if ch.isdigit())
            parts.append(int(digits) if digits else 0)
        return tuple(parts) or (0,)

    def available(self) -> bool:
        try:
            import paddle  # noqa: F401  (the runtime, not just the wrapper)
            import paddleocr  # noqa: F401
        except Exception:
            return False
        return True

    def _get_engine(self):
        if self._engine is None:
            from paddleocr import PaddleOCR

            if self._version()[0] >= 3:
                self._engine = PaddleOCR(
                    lang="en",
                    use_doc_orientation_classify=False,
                    use_doc_unwarping=False,
                    use_textline_orientation=False,
                    enable_mkldnn=settings.paddle_enable_mkldnn,
                )
            else:
                self._engine = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        return self._engine

    def recognise(self, image: Image.Image) -> list[OCRWord]:
        import numpy as np

        engine = self._get_engine()
        array = np.array(image.convert("RGB"))
        if self._version()[0] >= 3:
            return self._recognise_v3(engine, array)
        return self._recognise_v2(engine, array)

    @staticmethod
    def _recognise_v3(engine, array) -> list[OCRWord]:
        words: list[OCRWord] = []
        for result in engine.predict(array):
            payload = result.json.get("res", {}) if hasattr(result, "json") else dict(result)
            texts = payload.get("rec_texts") or []
            scores = payload.get("rec_scores") or []
            polygons = payload.get("rec_polys")
            if polygons is None:
                polygons = payload.get("dt_polys") or []
            for text, score, polygon in zip(texts, scores, polygons):
                if not str(text).strip():
                    continue
                xs = [float(point[0]) for point in polygon]
                ys = [float(point[1]) for point in polygon]
                words.append(
                    OCRWord(
                        text=str(text).strip(),
                        bbox=(min(xs), min(ys), max(xs), max(ys)),
                        confidence=float(score),
                    )
                )
        return words

    @staticmethod
    def _recognise_v2(engine, array) -> list[OCRWord]:
        words: list[OCRWord] = []
        for page_result in engine.ocr(array, cls=True) or []:
            for line in page_result or []:
                polygon, (text, score) = line[0], line[1]
                if not str(text).strip():
                    continue
                xs = [float(point[0]) for point in polygon]
                ys = [float(point[1]) for point in polygon]
                words.append(
                    OCRWord(
                        text=str(text).strip(),
                        bbox=(min(xs), min(ys), max(xs), max(ys)),
                        confidence=float(score),
                    )
                )
        return words


class RapidOCRBackend(OCRBackend):
    """RapidOCR: PP-OCR models on ONNX Runtime. Pure pip, no system binary."""

    name = "rapidocr"

    def __init__(self) -> None:
        self._engine = None

    def available(self) -> bool:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except Exception:
            return False
        return True

    def recognise(self, image: Image.Image) -> list[OCRWord]:
        import numpy as np

        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        result, _ = self._engine(np.array(image.convert("RGB")))
        words: list[OCRWord] = []
        for polygon, text, score in result or []:
            if not str(text).strip():
                continue
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
            words.append(
                OCRWord(str(text).strip(), (min(xs), min(ys), max(xs), max(ys)), float(score))
            )
        return words


class TesseractBackend(OCRBackend):
    name = "tesseract"

    def available(self) -> bool:
        try:
            import pytesseract

            pytesseract.get_tesseract_version()
        except Exception:
            return False
        return True

    def recognise(self, image: Image.Image) -> list[OCRWord]:
        import pytesseract
        from pytesseract import Output

        data = pytesseract.image_to_data(image, output_type=Output.DICT)
        # Group words into lines so the extractor sees label/value pairs.
        lines: dict[tuple[int, int, int], list[int]] = {}
        for i, text in enumerate(data["text"]):
            if not text.strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            lines.setdefault(key, []).append(i)

        words: list[OCRWord] = []
        for indices in lines.values():
            text = " ".join(data["text"][i].strip() for i in indices)
            x0 = min(data["left"][i] for i in indices)
            y0 = min(data["top"][i] for i in indices)
            x1 = max(data["left"][i] + data["width"][i] for i in indices)
            y1 = max(data["top"][i] + data["height"][i] for i in indices)
            confidences = [float(data["conf"][i]) for i in indices if float(data["conf"][i]) >= 0]
            score = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
            words.append(
                OCRWord(text=text, bbox=(float(x0), float(y0), float(x1), float(y1)), confidence=score)
            )
        return words


def _is_blank(image: Image.Image) -> bool:
    """A scanner's empty back side: specks, no text. Not worth seconds of OCR."""
    import numpy as np

    gray = np.asarray(image.convert("L").resize((400, max(1, int(400 * image.height / image.width)))))
    return float((gray < 140).mean()) < 0.0015


def _quality(words: list[OCRWord]) -> float:
    """How much confidently read text there is: the yardstick for orientation."""
    return sum(w.confidence * len(w.text.strip()) for w in words if w.confidence >= 0.6)


def _tall_ratio(words: list[OCRWord]) -> float:
    """Share of text boxes taller than wide: the signature of a sideways page."""
    long_words = [w for w in words if len(w.text.strip()) >= 3]
    if not long_words:
        return 0.0
    tall = sum(1 for w in long_words if (w.bbox[3] - w.bbox[1]) > 1.4 * (w.bbox[2] - w.bbox[0]))
    return tall / len(long_words)


def _reads_downwards(words: list[OCRWord]) -> bool | None:
    """True when the column headings lie above most dated rows; None if there is no table to judge."""
    from app.rules import lexicon
    from app.rules.dates import looks_like_date

    heading_y: list[float] = []
    for w in words:
        if lexicon.classify("column", w.text) not in (None, "ignore"):
            heading_y.append((w.bbox[1] + w.bbox[3]) / 2)
    if len(heading_y) < 2:
        return None
    heading_y.sort()
    # the headings sit together: take the tightest run of at least two
    best = min(
        (heading_y[i : i + 2] for i in range(len(heading_y) - 1)),
        key=lambda pair: pair[1] - pair[0],
    )
    heading = sum(best) / 2
    dated = [(w.bbox[1] + w.bbox[3]) / 2 for w in words if looks_like_date(w.text)]
    if len(dated) < 2:
        return None
    below = sum(1 for y in dated if y > heading)
    return below >= len(dated) - below


def _estimate_skew(image: Image.Image) -> float:
    """Degrees (counter-clockwise) that make the text lines horizontal; 0 when already straight."""
    import numpy as np

    gray = image.convert("L")
    scale = 900 / max(gray.size)
    if scale < 1:
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)))
    arr = np.asarray(gray, dtype=float)
    ink = Image.fromarray(((arr < arr.mean() - 0.6 * arr.std()) * 255).astype("uint8"))

    def sharpness(angle: float) -> float:
        rotated = ink.rotate(angle, resample=Image.BILINEAR, fillcolor=0)
        return float(np.var(np.asarray(rotated, dtype=float).sum(axis=1)))

    coarse = max((a * 0.5 for a in range(-12, 13)), key=sharpness)
    fine = max((coarse + a * 0.1 for a in range(-5, 6)), key=sharpness)
    return fine if sharpness(fine) > 1.03 * sharpness(0.0) else 0.0


class OCRService:
    """Selects an available OCR backend and normalizes its output."""

    def __init__(self, backends: list[OCRBackend] | None = None) -> None:
        self._backends = backends if backends is not None else self._preferred_backends()
        self._active: OCRBackend | None = None
        self._probed = False

    @staticmethod
    def _preferred_backends() -> list[OCRBackend]:
        """Probe order from `OCR_ENGINE`.

        "auto" tries Tesseract first: PaddleOCR on CPU costs tens of seconds a
        page, and installing it for PP-StructureV3 should not silently slow the
        OCR path down. Set OCR_ENGINE=paddleocr to prefer it anyway.
        """
        engine = settings.ocr_engine.lower()
        # Whatever is asked for goes first, but the rest still follow: naming an
        # engine that is not installed should not cost the user OCR altogether.
        if engine == "paddleocr":
            return [PaddleOCRBackend(), RapidOCRBackend(), TesseractBackend()]
        if engine == "rapidocr":
            return [RapidOCRBackend(), TesseractBackend(), PaddleOCRBackend()]
        return [TesseractBackend(), RapidOCRBackend(), PaddleOCRBackend()]

    @property
    def backend_name(self) -> str:
        return self._resolve().name if self.is_available else "none"

    @property
    def is_available(self) -> bool:
        return self._resolve() is not None

    def _resolve(self) -> OCRBackend | None:
        if not self._probed:
            self._probed = True
            for backend in self._backends:
                try:
                    if backend.available():
                        self._active = backend
                        logger.info("OCR backend selected: %s", backend.name)
                        break
                except Exception:  # pragma: no cover - defensive probe
                    continue
            else:
                logger.warning(
                    "No OCR backend available - scanned documents will need manual entry."
                )
        return self._active

    def extract(
        self,
        image_source: str | Path | bytes,
        page_number: int = 1,
        target_size: tuple[float, float] | None = None,
    ) -> Page:
        """OCR one page image into a normalized `Page`.

        `target_size` rescales coordinates into the page's own coordinate
        system (PDF points) so highlights line up with the rendered document.
        """
        backend = self._resolve()
        if backend is None:
            raise OCRUnavailableError(
                "OCR is not available in this environment. Install rapidocr-onnxruntime, pytesseract or paddleocr."
            )

        if isinstance(image_source, bytes):
            image = Image.open(io.BytesIO(image_source))
        else:
            image = Image.open(Path(image_source))
        image = image.convert("RGB")

        if _is_blank(image):
            logger.info("ocr page=%s skipped: blank", page_number)
            width, height = target_size or (float(image.width), float(image.height))
            return Page(page_number=page_number, width=width, height=height, blocks=[])

        image, quarter_turns = self._orient(backend, image)
        if quarter_turns % 2 and target_size:
            target_size = (target_size[1], target_size[0])
        try:
            angle = _estimate_skew(image)
        except Exception:  # pragma: no cover - never let deskewing break extraction
            angle = 0.0
        if angle:
            logger.info("ocr page=%s deskewed by %.1f degrees", page_number, angle)
            image = image.rotate(angle, resample=Image.BICUBIC, fillcolor=(255, 255, 255))

        words = backend.recognise(image)
        scale_x = scale_y = 1.0
        width, height = float(image.width), float(image.height)
        if target_size:
            scale_x = target_size[0] / width if width else 1.0
            scale_y = target_size[1] / height if height else 1.0
            width, height = target_size

        blocks = [
            Block(
                id=f"p{page_number}-o{index}",
                text=canonical(unicodedata.normalize("NFKC", word.text)),
                bbox=(
                    word.bbox[0] * scale_x,
                    word.bbox[1] * scale_y,
                    word.bbox[2] * scale_x,
                    word.bbox[3] * scale_y,
                ),
                source="ocr",
                confidence=word.confidence,
            )
            for index, word in enumerate(words, start=1)
        ]
        logger.info("ocr page=%s blocks=%s backend=%s", page_number, len(blocks), backend.name)
        return Page(page_number=page_number, width=width, height=height, blocks=blocks)

    def _orient(self, backend: OCRBackend, image: Image.Image) -> tuple[Image.Image, int]:
        """Turn a sideways or upside-down page upright; returns the image and quarter turns used.

        Judged on a small copy, so an ordinary upright page costs one quick pass.
        Sideways text shows up as tall, narrow OCR boxes. Upside-down text is
        read correctly line by line, so it is told apart by structure instead:
        on a statement the column headings sit *above* the dated rows.
        """
        small = image.copy()
        small.thumbnail((1000, 1000))
        cache: dict[int, list[OCRWord]] = {}

        def read(turns: int) -> list[OCRWord]:
            if turns not in cache:
                cache[turns] = backend.recognise(small.rotate(90 * turns, expand=True) if turns else small)
            return cache[turns]

        base = read(0)
        sideways = _tall_ratio(base) >= 0.5
        if not sideways and _quality(base) >= 120 and _reads_downwards(base) is not False:
            return image, 0
        pair = (1, 3) if sideways else (0, 2)
        verdicts = {turns: _reads_downwards(read(turns)) for turns in pair}
        if verdicts[pair[0]] is True and verdicts[pair[1]] is not True:
            chosen = pair[0]
        elif verdicts[pair[1]] is True and verdicts[pair[0]] is not True:
            chosen = pair[1]
        else:  # no table to go by: whichever reads better
            chosen = max(pair, key=lambda t: _quality(read(t)))
        if chosen:
            logger.info("ocr page turned %s quarter(s) to read upright", chosen)
            return image.rotate(90 * chosen, expand=True), chosen
        return image, 0


ocr_service = OCRService()
