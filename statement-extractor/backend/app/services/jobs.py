"""Background processing without extra infrastructure.

A thread pool runs extraction jobs; their state lives in ``processing_runs``,
so it survives a restart and any request can read it. Guarantees:

* a run never stays ``processing`` because of an exception: any failure is
  recorded on the run with an error code;
* runs that were queued or processing when the server stopped are marked
  ``failed`` / ``interrupted`` at the next start (``recover_interrupted``);
* the same file uploaded again by the same user while it is still being
  processed is not processed twice.

This is deliberately single-process. To run several API processes, move the
queue to a real broker (see docs/DEPLOYMENT.md).
"""
from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.models import Correction, Document, ExtractionField, ProcessingRun, StatementExtraction, Transaction
from app.services import pipeline
from app.services.llm_client import LLMClient

logger = get_logger(__name__)

ACTIVE = ("queued", "processing")

#: Tests and the evaluation harness inject a fake model client here.
llm_client_factory: Callable[[], LLMClient] | None = None


class JobRunner:
    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}

    def _pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=max(1, settings.processing_workers),
                                                    thread_name_prefix="extract")
            return self._executor

    def submit(self, run_id: str) -> Future:
        future = self._pool().submit(execute, run_id)
        with self._lock:
            self._futures[run_id] = future
        future.add_done_callback(lambda _f: self._forget(run_id))
        return future

    def _forget(self, run_id: str) -> None:
        with self._lock:
            self._futures.pop(run_id, None)

    def shutdown(self, wait: bool = False) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=wait, cancel_futures=not wait)


runner = JobRunner()


def execute(run_id: str) -> None:
    """Process one run in its own session. Never raises."""
    db = SessionLocal()
    try:
        run = db.get(ProcessingRun, run_id)
        if run is None or run.status not in ACTIVE:
            return
        document = db.get(Document, run.document_id)
        if document is None:
            return
        client = llm_client_factory() if llm_client_factory and run.engine_requested in ("groq", "hybrid") else None
        pipeline.process(db, document, run, engine=run.engine_requested, client=client)
    except Exception:
        logger.exception("job failed for run %s", run_id)
        db.rollback()
        _mark_failed(run_id, "internal_error", "Processing failed unexpectedly.")
    finally:
        db.close()


def _mark_failed(run_id: str, code: str, text: str) -> None:
    with SessionLocal() as db:
        db.execute(update(ProcessingRun).where(ProcessingRun.id == run_id, ProcessingRun.status.in_(ACTIVE)).values(
            status="failed", error_code=code, error_message=text, stage="done",
            completed_at=datetime.now(timezone.utc)))
        db.commit()


def recover_interrupted() -> int:
    """Runs left queued/processing by a previous server process can never finish."""
    with SessionLocal() as db:
        result = db.execute(update(ProcessingRun).where(ProcessingRun.status.in_(ACTIVE)).values(
            status="failed", error_code="interrupted",
            error_message="Processing was interrupted because the server restarted. Process the document again.",
            stage="done", completed_at=datetime.now(timezone.utc)))
        db.commit()
        count = result.rowcount or 0
    if count:
        logger.warning("marked %s interrupted run(s) as failed", count)
    return count


def latest_run(db: Session, document_id: str) -> ProcessingRun | None:
    return db.scalar(
        select(ProcessingRun).where(ProcessingRun.document_id == document_id)
        .order_by(ProcessingRun.created_at.desc(), ProcessingRun.started_at.desc())
    )


def running_duplicate(db: Session, owner: str, sha256: str) -> tuple[Document, ProcessingRun] | None:
    for document in db.scalars(select(Document).where(Document.owner == owner, Document.sha256 == sha256)
                               .order_by(Document.created_at.desc())):
        run = latest_run(db, document.id)
        if run is not None and run.status in ACTIVE:
            return document, run
    return None


# --------------------------------------------------------------------------
# Deletion and retention
# --------------------------------------------------------------------------


def delete_files(document_id: str) -> None:
    base = settings.upload_dir.resolve()
    target = (settings.upload_dir / document_id).resolve()
    if target != base and target.is_relative_to(base) and target.exists():
        shutil.rmtree(target, ignore_errors=True)


def delete_document(db: Session, document: Document) -> None:
    """Remove a document, its runs, extractions, corrections and stored file.

    Deleted children first, explicitly: extractions reference runs, and that
    order must not depend on how the ORM sorts its work.
    """
    document_id = document.id
    extraction_ids = select(StatementExtraction.id).where(StatementExtraction.document_id == document_id)
    for model in (Correction, ExtractionField, Transaction):
        db.execute(delete(model).where(model.extraction_id.in_(extraction_ids)))
    db.execute(delete(StatementExtraction).where(StatementExtraction.document_id == document_id))
    db.execute(delete(ProcessingRun).where(ProcessingRun.document_id == document_id))
    db.execute(delete(Document).where(Document.id == document_id))
    db.commit()
    db.expunge_all()
    delete_files(document_id)


def purge_expired() -> int:
    if settings.retention_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.retention_days)
    removed = 0
    with SessionLocal() as db:
        for document in list(db.scalars(select(Document).where(Document.created_at < cutoff.replace(tzinfo=None)))):
            run = latest_run(db, document.id)
            if run is not None and run.status in ACTIVE:
                continue
            delete_document(db, document)
            removed += 1
    if removed:
        logger.info("retention: deleted %s document(s) older than %s days", removed, settings.retention_days)
    return removed


class RetentionThread(threading.Thread):
    def __init__(self, interval_seconds: float = 6 * 3600) -> None:
        super().__init__(name="retention", daemon=True)
        self.interval = interval_seconds
        self.stop_event = threading.Event()

    def run(self) -> None:  # pragma: no cover - timing loop
        while not self.stop_event.wait(self.interval):
            try:
                purge_expired()
            except Exception:
                logger.exception("retention purge failed")
