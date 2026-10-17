"""Schema migrations are additive, preserve data, back up first and are idempotent."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db.migrations import CURRENT_VERSION, current_version, migrate

LEGACY = Path(__file__).resolve().parent / "fixtures" / "legacy_schema_v1.sql"


def legacy_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(LEGACY.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO documents (id, original_filename, stored_filename, file_type, mime_type, file_size_bytes,"
                     " sha256, page_count, has_text_layer, requires_ocr, preview_path, created_at)"
                     " VALUES ('d1', 'old.pdf', 'original.pdf', 'pdf', 'application/pdf', 10, 'abc', 1, 1, 0, 'x',"
                     " '2026-01-01 00:00:00')")
        conn.execute("INSERT INTO processing_runs (id, document_id, status, ocr_used, started_at, completed_at)"
                     " VALUES ('r1', 'd1', 'completed', 0, '2026-01-01 00:00:01', '2026-01-01 00:00:02')")
        conn.execute("INSERT INTO statement_extractions (id, document_id, processing_run_id, overall_confidence, summary,"
                     " created_at) VALUES ('e1', 'd1', 'r1', 0.9, '{\"totals\": {}}', '2026-01-01 00:00:02')")
        conn.execute("INSERT INTO transactions (id, extraction_id, row_index, page, txn_date, date_inherited, description,"
                     " debit, direction_inferred, balance_check, status) VALUES ('t1', 'e1', 1, 1, '2026-01-02', 0,"
                     " 'Coffee', '4.50', 0, 'ok', 'high')")


def test_legacy_database_is_upgraded_in_place(tmp_path):
    db_file = tmp_path / "statements.db"
    legacy_db(db_file)
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    assert current_version(engine) == 1

    assert migrate(engine, backup=True) == CURRENT_VERSION
    backups = list(tmp_path.glob("statements.db.bak-v1-*"))
    assert len(backups) == 1

    with engine.connect() as conn:
        cols = {c["name"] for c in inspect(conn).get_columns("transactions")}
        assert {"account_key", "amount", "deleted", "original", "evidence"} <= cols
        assert "corrections" in inspect(conn).get_table_names()
        row = conn.execute(text("SELECT description, debit, account_key, source_engine, deleted FROM transactions")).one()
        assert tuple(row) == ("Coffee", "4.50", "A1", "local", 0)
        run = conn.execute(text("SELECT engine_requested, engine_used, stage, created_at FROM processing_runs")).one()
        assert run[0] == "local" and run[1] == "local" and run[2] == "done" and run[3] is not None
        assert conn.execute(text("SELECT owner FROM documents")).scalar() == "local"

    # The backup is the untouched original.
    with sqlite3.connect(backups[0]) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        assert "account_key" not in cols

    # Running again changes nothing and makes no new backup.
    assert migrate(engine, backup=True) == CURRENT_VERSION
    assert len(list(tmp_path.glob("statements.db.bak-*"))) == 1
    engine.dispose()


def test_fresh_database_starts_at_current_version(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'new.db').as_posix()}")
    assert current_version(engine) == 0
    assert migrate(engine) == CURRENT_VERSION
    assert current_version(engine) == CURRENT_VERSION
    engine.dispose()


def test_legacy_result_is_served_but_never_presumed_complete(tmp_path):
    from app.models import Document, ProcessingRun, StatementExtraction
    from app.services.result_service import build_result

    document = Document(id="d1", original_filename="old.pdf", stored_filename="x", file_type="pdf",
                        mime_type="application/pdf", file_size_bytes=1, sha256="a", page_count=1, preview_path="")
    run = ProcessingRun(id="r1", document_id="d1", status="completed")
    extraction = StatementExtraction(id="e1", document_id="d1", processing_run_id="r1",
                                     summary={"totals": {"mode": "running_balance"}, "checks": [], "columns": []})
    out = build_result(document, run, extraction, [], [])
    assert out.completeness.status == "needs_review"
    assert out.completeness.reasons[0].code == "legacy_not_assessed"
