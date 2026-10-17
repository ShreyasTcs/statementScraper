"""Additive, versioned schema migrations.

The project started with ``Base.metadata.create_all`` only, which creates
missing tables but never adds columns to existing ones. This module upgrades an
existing database in place:

* the current version is kept in ``schema_migrations``; a database created
  before versioning existed is version 1;
* every migration only *adds* (columns with a default or NULL, new tables,
  backfills of those new columns) - nothing is dropped, renamed or rebuilt;
* before any pending migration runs, the SQLite file is copied aside
  (``statements.db.bak-v<from>-<timestamp>``) unless disabled;
* each migration runs in a transaction and is idempotent, so a crash halfway
  can simply be re-run.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from app.core.logging import get_logger

logger = get_logger(__name__)

CURRENT_VERSION = 2

#: (table, column, DDL type and default) added in version 2.
_V2_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("documents", "owner", "VARCHAR(64) NOT NULL DEFAULT 'local'"),
    ("processing_runs", "created_at", "DATETIME"),
    ("processing_runs", "engine_requested", "VARCHAR(16)"),
    ("processing_runs", "engine_used", "VARCHAR(16)"),
    ("processing_runs", "fallback_reason", "VARCHAR(64)"),
    ("processing_runs", "stage", "VARCHAR(32)"),
    ("processing_runs", "progress_current", "INTEGER"),
    ("processing_runs", "progress_total", "INTEGER"),
    ("processing_runs", "pages_processed", "INTEGER"),
    ("processing_runs", "pages_failed", "INTEGER"),
    ("processing_runs", "model", "VARCHAR(64)"),
    ("processing_runs", "prompt_version", "VARCHAR(16)"),
    ("processing_runs", "schema_version", "VARCHAR(16)"),
    ("processing_runs", "llm_calls", "INTEGER"),
    ("processing_runs", "llm_input_tokens", "INTEGER"),
    ("processing_runs", "llm_output_tokens", "INTEGER"),
    ("processing_runs", "llm_latency_ms", "INTEGER"),
    ("processing_runs", "chunks", "JSON"),
    ("statement_extractions", "completeness_status", "VARCHAR(16)"),
    ("statement_extractions", "engine", "VARCHAR(16)"),
    ("statement_extractions", "corrected", "BOOLEAN NOT NULL DEFAULT 0"),
    ("statement_extractions", "revalidated_at", "DATETIME"),
    ("extraction_fields", "account_key", "VARCHAR(32)"),
    ("extraction_fields", "provenance", "VARCHAR(16)"),
    ("extraction_fields", "evidence", "JSON"),
    ("extraction_fields", "validation_code", "VARCHAR(64)"),
    ("extraction_fields", "reason_codes", "JSON"),
    ("extraction_fields", "corrected", "BOOLEAN NOT NULL DEFAULT 0"),
    ("extraction_fields", "original_value", "TEXT"),
    ("extraction_fields", "original_normalized", "TEXT"),
    ("transactions", "account_key", "VARCHAR(32)"),
    ("transactions", "amount", "VARCHAR(32)"),
    ("transactions", "currency", "VARCHAR(3)"),
    ("transactions", "source_engine", "VARCHAR(16)"),
    ("transactions", "provenance", "VARCHAR(16)"),
    ("transactions", "evidence", "JSON"),
    ("transactions", "issue_codes", "JSON"),
    ("transactions", "reason_codes", "JSON"),
    ("transactions", "corrected", "BOOLEAN NOT NULL DEFAULT 0"),
    ("transactions", "deleted", "BOOLEAN NOT NULL DEFAULT 0"),
    ("transactions", "original", "JSON"),
)

_V2_BACKFILL: tuple[str, ...] = (
    "UPDATE processing_runs SET engine_requested = 'local' WHERE engine_requested IS NULL",
    "UPDATE processing_runs SET engine_used = 'local' WHERE engine_used IS NULL AND status = 'completed'",
    "UPDATE processing_runs SET created_at = COALESCE(started_at, completed_at) WHERE created_at IS NULL",
    "UPDATE processing_runs SET stage = 'done' WHERE stage IS NULL AND status IN ('completed', 'failed')",
    "UPDATE statement_extractions SET engine = 'local' WHERE engine IS NULL",
    "UPDATE extraction_fields SET account_key = 'A1' WHERE account_key IS NULL",
    "UPDATE transactions SET account_key = 'A1' WHERE account_key IS NULL",
    "UPDATE transactions SET source_engine = 'local' WHERE source_engine IS NULL",
)


def _columns(conn: Connection, table: str) -> set[str]:
    return {c["name"] for c in inspect(conn).get_columns(table)}


def _migration_v2(conn: Connection) -> None:
    from app.db.session import Base
    from app import models  # noqa: F401  (register mappers)

    for table, column, ddl in _V2_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    # New tables (corrections) - create_all only creates what is missing.
    Base.metadata.create_all(bind=conn)
    for statement in _V2_BACKFILL:
        conn.execute(text(statement))


MIGRATIONS: dict[int, Callable[[Connection], None]] = {2: _migration_v2}


def current_version(engine: Engine) -> int:
    """0 for an empty database, 1 for one created before versioning."""
    with engine.connect() as conn:
        tables = set(inspect(conn).get_table_names())
        if "schema_migrations" in tables:
            value = conn.execute(text("SELECT MAX(version) FROM schema_migrations")).scalar()
            return int(value or 1)
        return 1 if "documents" in tables else 0


def _sqlite_file(engine: Engine) -> Path | None:
    url = engine.url
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    path = Path(url.database)
    return path if path.exists() else None


def _backup(engine: Engine, from_version: int) -> Path | None:
    """Consistent copy via SQLite's online backup API (includes pages still in the WAL)."""
    source = _sqlite_file(engine)
    if source is None:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = source.with_name(f"{source.name}.bak-v{from_version}-{stamp}")
    engine.dispose()
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    return target


def migrate(engine: Engine, backup: bool = True) -> int:
    """Bring the database to CURRENT_VERSION. Returns the version reached."""
    from app.db.session import Base
    from app import models  # noqa: F401

    version = current_version(engine)
    if version == 0:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as conn:
            _ensure_version_table(conn)
            conn.execute(text("INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :t)"),
                         {"v": CURRENT_VERSION, "t": _now()})
        logger.info("database created at schema version %s", CURRENT_VERSION)
        return CURRENT_VERSION

    pending = [v for v in sorted(MIGRATIONS) if v > version]
    if not pending:
        return version
    if backup:
        copy = _backup(engine, version)
        if copy is not None:
            logger.info("database backed up before migration to %s", copy.name)
    for target in pending:
        with engine.begin() as conn:
            _ensure_version_table(conn)
            if version == 1:
                # Record that the pre-versioning schema was the starting point.
                conn.execute(text("INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, :t)"),
                             {"t": _now()})
            MIGRATIONS[target](conn)
            conn.execute(text("INSERT INTO schema_migrations (version, applied_at) VALUES (:v, :t)"),
                         {"v": target, "t": _now()})
        logger.info("database migrated to schema version %s", target)
        version = target
    return version


def _ensure_version_table(conn: Connection) -> None:
    conn.execute(text(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at VARCHAR(40) NOT NULL)"
    ))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
