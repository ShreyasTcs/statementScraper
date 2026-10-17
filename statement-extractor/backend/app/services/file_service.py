"""Upload validation, storage and input analysis (Phase 2)."""
from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_PDF_MAGIC = b"%PDF-"
_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FileValidationError(Exception):
    """Raised when an upload fails validation. `code` is safe to show users."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class FileAnalysis:
    file_type: str
    mime_type: str
    has_text_layer: bool
    page_count: int
    requires_ocr: bool


def validate_upload(filename: str, content: bytes) -> tuple[str, str]:
    """Validate extension, sniffed type and size. Returns (file_type, mime)."""
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in settings.allowed_extensions:
        raise FileValidationError(
            "unsupported_file",
            "This file format isn't supported. Please upload a PDF, JPG or PNG of the statement.",
        )

    if len(content) == 0:
        raise FileValidationError("empty_file", "This file is empty.")

    if len(content) > settings.max_file_size_bytes:
        limit_mb = settings.max_file_size_bytes // (1024 * 1024)
        raise FileValidationError(
            "file_too_large",
            f"This file is too large. Please upload a smaller document - "
            f"try compressing or splitting the PDF (limit {limit_mb} MB).",
        )

    # Trust the bytes, not the extension.
    if content.startswith(_PDF_MAGIC):
        sniffed = "pdf"
    elif content.startswith(_JPEG_MAGIC):
        sniffed = "jpg"
    elif content.startswith(_PNG_MAGIC):
        sniffed = "png"
    else:
        raise FileValidationError(
            "unsupported_file",
            "This file format isn't supported. Please upload a PDF bank statement.",
        )

    declared = "jpg" if suffix == "jpeg" else suffix
    if declared != sniffed:
        raise FileValidationError(
            "content_mismatch",
            "This file's contents do not match its extension.",
        )

    mime = mimetypes.types_map.get(f".{suffix}") or (
        "application/pdf" if sniffed == "pdf" else f"image/{'jpeg' if sniffed == 'jpg' else 'png'}"
    )
    return sniffed, mime


def sha256_of(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def store_original(document_id: str, filename: str, content: bytes) -> Path:
    """Persist the original upload under a server-generated name.

    The original file is never modified: the review screen renders exactly
    these bytes (FR-03).
    """
    suffix = Path(filename).suffix.lower()
    target_dir = settings.upload_dir / document_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"original{suffix}"
    target.write_bytes(content)
    return target


def resolve_stored_path(document_id: str, stored_filename: str) -> Path:
    """Resolve a stored file, refusing anything outside the upload directory."""
    base = (settings.upload_dir / document_id).resolve()
    candidate = (base / stored_filename).resolve()
    if not candidate.is_relative_to(settings.upload_dir.resolve()):
        raise FileValidationError("invalid_path", "Invalid document reference.")
    if not candidate.exists():
        raise FileValidationError("not_found", "Document file is no longer available.")
    return candidate


def analyze(path: Path, file_type: str) -> FileAnalysis:
    """Detect page count and whether a usable text layer exists."""
    if file_type == "pdf":
        return _analyze_pdf(path)
    return _analyze_image(path, file_type)


def _analyze_pdf(path: Path) -> FileAnalysis:
    with fitz.open(path) as doc:
        if doc.needs_pass:
            raise FileValidationError("password_protected", "This PDF is password protected. Upload an unprotected copy.")
        page_count = doc.page_count
        pages_with_text = 0
        for page in doc:
            text = page.get_text("text").strip()
            if len(text) >= settings.min_chars_for_text_layer:
                pages_with_text += 1

    # A usable text layer means every page can be read directly; otherwise the
    # scanned pages still need OCR.
    has_text_layer = pages_with_text > 0
    requires_ocr = pages_with_text < page_count
    logger.info(
        "pdf analysed: pages=%s pages_with_text=%s requires_ocr=%s",
        page_count,
        pages_with_text,
        requires_ocr,
    )
    return FileAnalysis(
        file_type="pdf",
        mime_type="application/pdf",
        has_text_layer=has_text_layer,
        page_count=page_count,
        requires_ocr=requires_ocr,
    )


def _analyze_image(path: Path, file_type: str) -> FileAnalysis:
    with Image.open(path) as img:
        img.verify()
    return FileAnalysis(
        file_type=file_type,
        mime_type="image/jpeg" if file_type in ("jpg", "jpeg") else "image/png",
        has_text_layer=False,
        page_count=1,
        requires_ocr=True,
    )
