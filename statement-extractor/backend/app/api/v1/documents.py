"""Document endpoints.

Every route is scoped to the calling user: another user's document is a 404,
indistinguishable from one that does not exist. Errors are
``{"detail": {"code", "message", "params"}}`` with stable codes.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import current_user
from app.db.session import get_db
from app.models import Correction, Document, ExtractionField, ProcessingRun, StatementExtraction, Transaction
from app.schemas.schemas import (
    CorrectionOut,
    DocumentList,
    FieldUpdate,
    ReprocessRequest,
    ResultOut,
    StatusOut,
    TransactionCreate,
    TransactionUpdate,
    UploadOut,
)
from app.services import correction_service, file_service, jobs, pipeline, result_service
from app.services.correction_service import CorrectionError
from app.services.file_service import FileValidationError

logger = get_logger(__name__)
router = APIRouter(prefix="/documents", tags=["documents"])


def _error(status: int, code: str, message: str, **params) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, "params": params})


def _engine_or_error(engine: str | None) -> str:
    chosen = engine or settings.extraction_engine
    problems = settings.engine_problems(chosen)
    if problems:
        # Never fall back to another engine behind the user's back.
        raise _error(422, problems[0]["code"], problems[0]["message"], engine=chosen,
                     problems=[p["code"] for p in problems])
    return chosen


def _read_upload(file: UploadFile) -> bytes:
    """Read at most the size limit (+1 byte to detect anything larger)."""
    limit = settings.max_file_size_bytes
    data = file.file.read(limit + 1)
    if len(data) > limit:
        raise _error(413, "file_too_large",
                     f"This file is too large (limit {limit // (1024 * 1024)} MB). Try compressing or splitting it.",
                     limit_mb=limit // (1024 * 1024))
    return data


# --------------------------------------------------------------------------
# Upload and processing
# --------------------------------------------------------------------------


@router.post("", response_model=UploadOut, status_code=202)
def upload_document(
    response: Response,
    file: UploadFile = File(...),
    engine: Literal["local", "groq", "hybrid"] | None = Form(default=None),
    wait: bool = Query(default=False, description="Process before answering instead of in the background."),
    db: Session = Depends(get_db),
    user: str = Depends(current_user),
) -> UploadOut:
    chosen = _engine_or_error(engine)
    content = _read_upload(file)
    try:
        file_type, mime_type = file_service.validate_upload(file.filename or "", content)
    except FileValidationError as exc:
        raise _error(413 if exc.code == "file_too_large" else 422, exc.code, exc.message) from exc

    sha = file_service.sha256_of(content)
    running = jobs.running_duplicate(db, user, sha)
    if running is not None:
        document, run = running
        return UploadOut(document_id=document.id, run_id=run.id, status=run.status,
                         engine=run.engine_requested or chosen, page_count=document.page_count,
                         duplicate_of_running=True)

    document = Document(
        original_filename=(file.filename or f"upload.{file_type}")[:255],
        stored_filename="",
        file_type=file_type,
        mime_type=mime_type,
        file_size_bytes=len(content),
        sha256=sha,
        preview_path="",
        owner=user,
    )
    db.add(document)
    db.flush()
    stored = file_service.store_original(document.id, document.original_filename, content)
    document.stored_filename = stored.name
    document.preview_path = str(stored)

    try:
        analysis = file_service.analyze(stored, file_type)
    except FileValidationError as exc:
        db.rollback()
        jobs.delete_files(document.id)
        raise _error(422, exc.code, exc.message) from exc
    except Exception as exc:
        db.rollback()
        jobs.delete_files(document.id)
        logger.warning("upload analysis failed: %s", type(exc).__name__)
        raise _error(422, "unreadable_file", "This document could not be read.") from exc
    if analysis.page_count > settings.max_pages:
        db.rollback()
        jobs.delete_files(document.id)
        raise _error(422, "too_many_pages", f"This document has {analysis.page_count} pages; the limit is {settings.max_pages}.",
                     pages=analysis.page_count, limit=settings.max_pages)

    document.page_count = analysis.page_count
    document.has_text_layer = analysis.has_text_layer
    document.requires_ocr = analysis.requires_ocr
    run = ProcessingRun(document_id=document.id, status="queued", stage="queued", engine_requested=chosen)
    db.add(run)
    db.commit()

    if wait:
        jobs.execute(run.id)
        db.refresh(run)
        response.status_code = 201
    else:
        jobs.runner.submit(run.id)
    return UploadOut(document_id=document.id, run_id=run.id, status=run.status, engine=chosen,
                     page_count=document.page_count)


@router.post("/{document_id}/reprocess", response_model=UploadOut, status_code=202)
def reprocess(document_id: str, body: ReprocessRequest, response: Response, wait: bool = Query(default=False),
              db: Session = Depends(get_db), user: str = Depends(current_user)) -> UploadOut:
    document = _document(db, document_id, user)
    latest = jobs.latest_run(db, document.id)
    if latest is not None and latest.status in jobs.ACTIVE:
        raise _error(409, "processing", "This document is still being processed.")
    chosen = _engine_or_error(body.engine)
    run = ProcessingRun(document_id=document.id, status="queued", stage="queued", engine_requested=chosen)
    db.add(run)
    db.commit()
    if wait:
        jobs.execute(run.id)
        db.refresh(run)
        response.status_code = 201
    else:
        jobs.runner.submit(run.id)
    return UploadOut(document_id=document.id, run_id=run.id, status=run.status, engine=chosen,
                     page_count=document.page_count)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _document(db: Session, document_id: str, user: str) -> Document:
    document = db.get(Document, document_id)
    if document is None or (document.owner or "local") != user:
        raise _error(404, "not_found", "Document not found.")
    return document


def _extraction_for(db: Session, run: ProcessingRun | None) -> StatementExtraction | None:
    if run is None:
        return None
    return db.scalar(select(StatementExtraction).where(StatementExtraction.processing_run_id == run.id))


def _load(db: Session, document_id: str, user: str, *, require_extraction: bool = False):
    document = _document(db, document_id, user)
    run = jobs.latest_run(db, document.id)
    if run is not None and run.status in jobs.ACTIVE:
        raise _error(409, "processing", "This document is still being processed.", stage=run.stage)
    extraction = _extraction_for(db, run)
    if extraction is None and run is None:
        raise _error(404, "not_processed", "This document has not been processed.")
    if extraction is None and require_extraction:
        raise _error(409, "no_extraction", "The last processing run failed; there is nothing to edit.")
    fields: list[ExtractionField] = []
    transactions: list[Transaction] = []
    if extraction is not None:
        fields = list(db.scalars(select(ExtractionField).where(ExtractionField.extraction_id == extraction.id)
                                 .order_by(ExtractionField.account_key, ExtractionField.display_order)))
        transactions = list(db.scalars(select(Transaction).where(Transaction.extraction_id == extraction.id)
                                       .order_by(Transaction.row_index)))
    return document, run, extraction, fields, transactions


def _result(db: Session, document_id: str, user: str) -> ResultOut:
    document, run, extraction, fields, transactions = _load(db, document_id, user)
    corrections = 0
    if extraction is not None:
        corrections = db.scalar(select(func.count()).select_from(Correction)
                                .where(Correction.extraction_id == extraction.id)) or 0
    return result_service.build_result(document, run, extraction, fields, transactions, corrections)


@router.get("", response_model=DocumentList)
def list_documents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=100),
    db: Session = Depends(get_db),
    user: str = Depends(current_user),
) -> DocumentList:
    query = select(Document).where(Document.owner == user)
    if q:
        query = query.where(or_(Document.original_filename.ilike(f"%{q}%")))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    items = []
    for document in db.scalars(query.order_by(Document.created_at.desc()).limit(limit).offset(offset)):
        run = jobs.latest_run(db, document.id)
        extraction = _extraction_for(db, run)
        rows = 0
        if extraction is not None:
            rows = db.scalar(select(func.count()).select_from(Transaction).where(
                Transaction.extraction_id == extraction.id, Transaction.deleted.is_(False))) or 0
        items.append(result_service.list_item(document, run, extraction, rows))
    return DocumentList(items=items, total=total, limit=limit, offset=offset)


@router.get("/{document_id}/status", response_model=StatusOut)
def get_status(document_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)) -> StatusOut:
    document = _document(db, document_id, user)
    run = jobs.latest_run(db, document.id)
    if run is None:
        raise _error(404, "not_processed", "This document has not been processed.")
    return StatusOut(document_id=document.id, run=result_service.run_out(run))


@router.get("/{document_id}/result", response_model=ResultOut)
def get_result(document_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)) -> ResultOut:
    return _result(db, document_id, user)


@router.get("/{document_id}/corrections", response_model=list[CorrectionOut])
def list_corrections(document_id: str, db: Session = Depends(get_db),
                     user: str = Depends(current_user)) -> list[CorrectionOut]:
    _, _, extraction, _, _ = _load(db, document_id, user)
    if extraction is None:
        return []
    return [
        CorrectionOut(id=c.id, entity_type=c.entity_type, entity_id=c.entity_id, action=c.action,
                      attribute=c.attribute, old_value=c.old_value, new_value=c.new_value, user=c.user,
                      created_at=c.created_at.isoformat() if c.created_at else None)
        for c in db.scalars(select(Correction).where(Correction.extraction_id == extraction.id)
                            .order_by(Correction.created_at))
    ]


# --------------------------------------------------------------------------
# Review: edits, revalidation
# --------------------------------------------------------------------------


def _editable(db: Session, document_id: str, user: str) -> StatementExtraction:
    _, _, extraction, _, _ = _load(db, document_id, user, require_extraction=True)
    assert extraction is not None
    return extraction


def _apply(db: Session, extraction: StatementExtraction, document_id: str, user: str, action) -> ResultOut:
    try:
        action()
        db.flush()
        pipeline.revalidate(db, extraction)
        db.commit()
    except CorrectionError as exc:
        db.rollback()
        raise _error(422, exc.code, exc.message, **exc.params) from exc
    return _result(db, document_id, user)


@router.patch("/{document_id}/fields/{field_id}", response_model=ResultOut)
def update_field(document_id: str, field_id: str, body: FieldUpdate, db: Session = Depends(get_db),
                 user: str = Depends(current_user)) -> ResultOut:
    extraction = _editable(db, document_id, user)
    field = db.get(ExtractionField, field_id)
    if field is None or field.extraction_id != extraction.id:
        raise _error(404, "unknown_field", "No such field.")
    return _apply(db, extraction, document_id, user,
                  lambda: correction_service.update_field(db, extraction, field, body, user))


def _transaction(db: Session, extraction: StatementExtraction, txn_id: str) -> Transaction:
    t = db.get(Transaction, txn_id)
    if t is None or t.extraction_id != extraction.id:
        raise _error(404, "transaction_not_found", "No such transaction.")
    return t


@router.patch("/{document_id}/transactions/{txn_id}", response_model=ResultOut)
def update_transaction(document_id: str, txn_id: str, body: TransactionUpdate, db: Session = Depends(get_db),
                       user: str = Depends(current_user)) -> ResultOut:
    extraction = _editable(db, document_id, user)
    t = _transaction(db, extraction, txn_id)
    return _apply(db, extraction, document_id, user,
                  lambda: correction_service.update_transaction(db, extraction, t, body, user))


@router.post("/{document_id}/transactions", response_model=ResultOut, status_code=201)
def create_transaction(document_id: str, body: TransactionCreate, db: Session = Depends(get_db),
                       user: str = Depends(current_user)) -> ResultOut:
    extraction = _editable(db, document_id, user)
    return _apply(db, extraction, document_id, user,
                  lambda: correction_service.create_transaction(db, extraction, body, user))


@router.delete("/{document_id}/transactions/{txn_id}", response_model=ResultOut)
def delete_transaction(document_id: str, txn_id: str, db: Session = Depends(get_db),
                       user: str = Depends(current_user)) -> ResultOut:
    extraction = _editable(db, document_id, user)
    t = _transaction(db, extraction, txn_id)
    return _apply(db, extraction, document_id, user,
                  lambda: correction_service.delete_transaction(db, extraction, t, user))


@router.post("/{document_id}/transactions/{txn_id}/restore", response_model=ResultOut)
def restore_transaction(document_id: str, txn_id: str, db: Session = Depends(get_db),
                        user: str = Depends(current_user)) -> ResultOut:
    extraction = _editable(db, document_id, user)
    t = _transaction(db, extraction, txn_id)
    return _apply(db, extraction, document_id, user,
                  lambda: correction_service.restore_transaction(db, extraction, t, user))


@router.post("/{document_id}/unresolved/{account}/{index}/dismiss", response_model=ResultOut)
def dismiss_unresolved(document_id: str, account: str, index: int, dismissed: bool = Query(default=True),
                       db: Session = Depends(get_db), user: str = Depends(current_user)) -> ResultOut:
    extraction = _editable(db, document_id, user)
    return _apply(db, extraction, document_id, user,
                  lambda: correction_service.set_unresolved_dismissed(db, extraction, account, index, dismissed, user))


@router.post("/{document_id}/revalidate", response_model=ResultOut)
def revalidate(document_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)) -> ResultOut:
    extraction = _editable(db, document_id, user)
    return _apply(db, extraction, document_id, user, lambda: None)


# --------------------------------------------------------------------------
# Files, exports, deletion
# --------------------------------------------------------------------------


@router.get("/{document_id}/transactions.csv")
def download_transactions(document_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)) -> Response:
    _, _, _, _, transactions = _load(db, document_id, user, require_extraction=True)
    return Response(
        content=result_service.transactions_csv(transactions),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="transactions-{document_id[:8]}.csv"'},
    )


@router.get("/{document_id}/result.json")
def download_result(document_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)) -> Response:
    result = _result(db, document_id, user)
    return Response(
        content=result_service.result_json(result),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="statement-{document_id[:8]}.json"'},
    )


@router.get("/{document_id}/preview")
def preview_document(document_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)) -> FileResponse:
    """Serves the original upload, untouched."""
    document = _document(db, document_id, user)
    try:
        path = file_service.resolve_stored_path(document.id, document.stored_filename)
    except FileValidationError as exc:
        raise _error(404, exc.code, exc.message) from exc
    return FileResponse(
        path,
        media_type={"pdf": "application/pdf", "jpg": "image/jpeg", "png": "image/png"}.get(document.file_type, "application/octet-stream"),
        filename=document.original_filename,
        content_disposition_type="inline",
    )


@router.delete("/{document_id}", status_code=204)
def delete_document(document_id: str, db: Session = Depends(get_db), user: str = Depends(current_user)) -> Response:
    document = _document(db, document_id, user)
    run = jobs.latest_run(db, document.id)
    if run is not None and run.status in jobs.ACTIVE:
        raise _error(409, "processing", "This document is still being processed; delete it when processing ends.")
    jobs.delete_document(db, document)
    return Response(status_code=204)
