"""Run the side-by-side PoC on one statement or a set of statements.

From statement-extractor/backend:

    .venv/Scripts/python -m experiments.markdown_ab.run --statement ../data/corpus/uk_split_balance_bfwd.pdf \
        --model llama3.2:3b --pin a80c4f17acd5
    .venv/Scripts/python -m experiments.markdown_ab.run --set mocks  --model llama3.2:3b --pin a80c4f17acd5
    .venv/Scripts/python -m experiments.markdown_ab.run --set corpus --model llama3.2:3b --pin a80c4f17acd5
    .venv/Scripts/python -m experiments.markdown_ab.run --set corpus --markdown-only     # no model, inspect Markdown

Each statement is read once (PyMuPDF text layer, RapidOCR for pages without text). The same reading result feeds
Pipeline A (direct layout lines -> the existing LLM path with a local Ollama client), Pipeline B (Markdown-derived lines
-> the same extractor, mapper, verification, validation and completeness) and the deterministic local engine as a
reference. Results go to data/evaluations/<run-id>/ and data/evaluation.db. Model calls run one after another.
"""
from __future__ import annotations

# Pins the application's settings (external calls off, no Groq key, unused DB path). Must come before any app import.
from experiments.markdown_ab import (  # isort: skip
    APP_DB_SENTINEL, BACKEND_DIR, DATA_DIR, EVAL_DB, EVAL_DIR, LIVE_DB_FILES, PINNED_ENV,
)
from experiments.markdown_ab.local_llm import (  # isort: skip
    DEFAULT_ENDPOINT, FileAccessGuard, LocalOnlyError, NetworkMonitor, OllamaClient, SocketGuard, unload,
    verify_app_isolation, verify_endpoint, verify_model,
)

FILE_GUARD: FileAccessGuard | None = None


def install_file_guard(confidential_read_authorised: bool = False) -> FileAccessGuard:
    """Install the audit-hook file guard once per process. The live database, its WAL/SHM files and the app-database
    sentinel are always protected; the confidential statements and the uploads too, unless this process runs the
    authorised local comparison that reads them (read-only)."""
    global FILE_GUARD
    if FILE_GUARD is None:
        folders = [APP_DB_SENTINEL.parent]
        if not confidential_read_authorised:
            folders += [DATA_DIR / "companySamples", DATA_DIR / "uploads"]
        FILE_GUARD = FileAccessGuard(files=LIVE_DB_FILES + [APP_DB_SENTINEL], folders=folders).install()
    return FILE_GUARD

import argparse  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import platform  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import uuid  # noqa: E402
from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.models import Document  # noqa: E402
from app.schemas.llm import SCHEMA_VERSION, LLMChunk  # noqa: E402
from app.services import completeness_service, file_service  # noqa: E402
from app.services import pipeline as P  # noqa: E402
from app.services.completeness_service import AccountEvidence, ChunkReport  # noqa: E402
from app.services.confidence_service import confidence_service  # noqa: E402
from app.services.llm_extraction_service import PROMPT_VERSION, LLMExtractor, ResultMapper  # noqa: E402
from app.services.page_text import document_lines  # noqa: E402
from app.services.structure_service import structure_analyzer  # noqa: E402
from app.services.table_service import infer_conventions  # noqa: E402
from experiments.markdown_ab import report as reporting  # noqa: E402
from experiments.markdown_ab import score as scoring  # noqa: E402
from experiments.markdown_ab.markdown_view import MarkdownView, build_markdown  # noqa: E402
from experiments.markdown_ab.grid import GridRowCheck, analyze_local_with_grid, arithmetic_key, replay_steps  # noqa: E402
from app.services.llm_client import ScriptedClient  # noqa: E402

POC_VERSION = "poc-1"
SETS = {"mocks": DATA_DIR / "samples", "corpus": DATA_DIR / "corpus"}
SELECTION = ["corpus/uk_split_balance_bfwd.pdf", "corpus/de_split_nobalance.pdf"]
ALLOWED_ROOTS = {"samples": "non-confidential mock statements", "corpus": "non-confidential synthetic corpus"}
REFUSED = {
    "companySamples": "confidential statements: not authorised for this PoC",
    "uploads": "uploaded documents may be confidential: not authorised for this PoC",
    "fuzz": "the fuzz set is not authorised for this PoC",
}
SUFFIXES = {".pdf": "pdf", ".png": "png", ".jpg": "jpg", ".jpeg": "jpg"}
PIPELINES = {
    "A": "A - Direct OCR/Text + Local LLM",
    "B": "B - Markdown + Local LLM",
    "reference": "Reference - Existing Deterministic Local Engine",
}


class PipelineFailure(Exception):
    def __init__(self, code: str, message: str, run: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.run = code, message, run


# --------------------------------------------------------------------------
# Input selection
# --------------------------------------------------------------------------


def resolve_statement(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = (Path.cwd() / path)
    path = path.resolve()
    try:
        relative = path.relative_to(DATA_DIR.resolve())
    except ValueError:
        raise SystemExit(f"refused: {raw} is outside {DATA_DIR}; only data/samples and data/corpus are permitted") from None
    top = relative.parts[0] if relative.parts else ""
    if top in REFUSED:
        raise SystemExit(f"refused: {REFUSED[top]}")
    if top not in ALLOWED_ROOTS:
        raise SystemExit(f"refused: only data/samples and data/corpus are permitted (got data/{top})")
    if path.suffix.lower() not in SUFFIXES or not path.is_file():
        raise SystemExit(f"refused: {raw} is not a PDF/PNG/JPG statement file")
    return path


def statement_set(name: str) -> list[Path]:
    if name == "selection":
        return [resolve_statement(str(DATA_DIR / rel)) for rel in SELECTION]
    folder = SETS[name]
    return sorted(p.resolve() for p in folder.iterdir() if p.suffix.lower() in SUFFIXES)


def statement_id(path: Path) -> str:
    return path.relative_to(DATA_DIR.resolve()).as_posix()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_tree_hash() -> str:
    digest = hashlib.sha256()
    for folder in (BACKEND_DIR / "app", BACKEND_DIR / "experiments"):
        for path in sorted(folder.rglob("*.py")):
            digest.update(path.relative_to(BACKEND_DIR).as_posix().encode())
            digest.update(_sha256(path).encode())
    return digest.hexdigest()


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, tuple)):
        return list(value)
    if hasattr(value, "__dict__"):
        return {k: v for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=_json_default), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _dec(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def live_db_state() -> dict[str, Any]:
    out = {}
    for path in LIVE_DB_FILES:
        try:
            st = path.stat()
            out[path.name] = {"size": st.st_size, "mtime": st.st_mtime}
        except FileNotFoundError:
            out[path.name] = None
    return out


# --------------------------------------------------------------------------
# Reading (once per statement)
# --------------------------------------------------------------------------


def read_once(path: Path) -> tuple[Any, Any, dict[str, Any], int]:
    started = time.perf_counter()
    file_type = SUFFIXES[path.suffix.lower()]
    analysis = file_service.analyze(path, file_type)
    document = Document(  # a transient object: never added to a database session
        id=hashlib.sha256(str(path).encode()).hexdigest()[:32], original_filename=path.name, stored_filename=path.name,
        file_type=analysis.file_type, mime_type=analysis.mime_type, file_size_bytes=path.stat().st_size,
        sha256=_sha256(path), page_count=analysis.page_count, has_text_layer=analysis.has_text_layer,
        requires_ocr=analysis.requires_ocr, preview_path="",
    )
    content, pages = P.read_document(document, path)
    duration = int((time.perf_counter() - started) * 1000)
    lines = document_lines(content)
    per_page = []
    for page in content.pages:
        sources = sorted({b.source for b in page.blocks}) if page.blocks else []
        per_page.append({"page": page.page_number, "source": "+".join(sources) or "none", "blocks": len(page.blocks),
                         "lines": sum(1 for l in lines if l.page == page.page_number)})
    method = ("PyMuPDF text layer" if analysis.has_text_layer and not content.ocr_used else
              "RapidOCR (no text layer)" if not analysis.has_text_layer else "PyMuPDF text layer + RapidOCR for pages without text")
    characteristics = {
        "file_type": analysis.file_type, "pages": analysis.page_count, "has_text_layer": analysis.has_text_layer,
        "requires_ocr": analysis.requires_ocr, "ocr_used": content.ocr_used, "method": method,
        "ocr_engine_setting": settings.ocr_engine, "ocr_dpi": settings.ocr_dpi,
        "pages_processed": pages.processed, "pages_failed": pages.failed, "per_page": per_page,
        "direct_lines": len(lines), "sha256": document.sha256, "size_bytes": document.file_size_bytes,
    }
    return content, pages, characteristics, duration


# --------------------------------------------------------------------------
# Serialising results
# --------------------------------------------------------------------------


def _row(r: Any) -> dict[str, Any]:
    evidence = r.evidence or {}
    return {
        "index": r.index, "page": r.page, "bbox": [round(v, 1) for v in r.bbox], "date": _iso(r.date),
        "value_date": _iso(r.value_date), "description": r.description, "reference": r.reference,
        "debit": _dec(r.debit), "credit": _dec(r.credit), "amount": _dec(r.amount), "balance": _dec(r.balance),
        "currency": r.currency, "account": r.account_key, "balance_check": r.balance_check, "status": r.status,
        "issues": list(r.issues), "source_engine": r.source_engine,
        "source_lines": evidence.get("lines") or evidence.get("line_ids"), "evidence": evidence,
    }


def serialize(analysis: Any) -> dict[str, Any]:
    accounts = []
    for account in analysis.accounts:
        summary = P.account_summary(account)
        headers = {}
        for name, extraction in account.headers.items():
            best = extraction.best
            headers[name] = None if best is None else {"value": best.value, "normalized": best.normalized,
                                                       "page": best.page_number}
        accounts.append({
            "key": account.key, "engine": account.engine, "headers": headers, "totals": summary["totals"],
            "checks": summary["checks"], "balance_rows": summary["balance_rows"], "totals_rows": summary["totals_rows"],
            "unresolved": summary["unresolved"], "duplicates": summary["duplicates"], "columns": summary["columns"],
            "notes": summary["notes"], "unverified_values": account.unverified_values,
            "rows": [_row(r) for r in account.table.rows],
        })
    return {"status": "completed", "accounts": accounts, "completeness": analysis.completeness.as_dict(),
            "overall_confidence": analysis.overall, "engine": analysis.engine.as_dict(), "chunks": vars(analysis.chunks)}


def llm_record(client: OllamaClient, run: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outcomes = []
    if run is not None:
        for outcome in run.outcomes:
            item = outcome.as_dict()
            item["result"] = outcome.result.model_dump() if outcome.result is not None else None
            outcomes.append(item)
    valid = 0
    for call in client.calls:
        content = call.get("content")
        if content:
            try:
                LLMChunk.model_validate_json(content)
                valid += 1
            except Exception:  # noqa: BLE001 - counting invalid answers is the point
                pass
    summary = {
        "model": client.model, "prompt_version": PROMPT_VERSION, "schema_version": SCHEMA_VERSION,
        "settings": client.settings(), "calls": len(client.calls), "schema_valid_calls": valid,
        "schema_valid_rate": round(valid / len(client.calls), 4) if client.calls else None,
        "chunks": len(run.outcomes) if run is not None else 0,
        "chunks_ok": sum(1 for o in run.outcomes if o.status == "ok") if run is not None else 0,
        "chunk_errors": [o.error_code for o in run.outcomes if o.error_code] if run is not None else [],
        "fatal_error": run.fatal_error.code if run is not None and run.fatal_error is not None else None,
        "llm_ms": sum(c.get("latency_ms", 0) for c in client.calls),
        "input_tokens": sum(c.get("prompt_eval_count", 0) for c in client.calls),
        "output_tokens": sum(c.get("eval_count", 0) for c in client.calls),
    }
    return {"summary": summary, "outcomes": outcomes}, client.calls


# --------------------------------------------------------------------------
# The three columns
# --------------------------------------------------------------------------


def run_a(content: Any, pages: Any, client: OllamaClient) -> tuple[dict[str, Any], Any]:
    """Pipeline A: the existing analyze() with engine="groq" and the injected local client. Nothing else differs."""
    try:
        analysis = P.analyze(copy.deepcopy(content), engine="groq", client=client, pages=pages)
    except P.ProcessingError as exc:
        return {"status": "failed", "error": {"code": exc.code, "message": str(exc)}}, getattr(exc, "run", None)
    return serialize(analysis), analysis.engine.llm


def extract_from_lines(content: Any, lines: list, client: Any, pages: Any, row_hook: Any = None) -> Any:
    """Pipeline B: the same steps analyze() runs for engine="groq" (pipeline.py analyze/_run_groq), with the Markdown-
    derived lines in place of document_lines(content). Same conventions, extractor, mapper, validation, completeness."""
    structured = structure_analyzer.analyze(content)
    conv = infer_conventions(structured, ocr=content.ocr_used)
    if not lines:
        raise PipelineFailure("no_text", "No readable text was found in this document.")
    run, registry = LLMExtractor(client).run(lines, len(content.pages))
    chunks = ChunkReport(total=len(run.outcomes), failed=len(run.failed))
    if run.fatal_error is not None or not any(o.status == "ok" for o in run.outcomes):
        raise PipelineFailure(run.error_code or "llm_failed", P._llm_failure_text(run.error_code or "llm_failed"), run)
    mapped = ResultMapper(lines, conv).map(run, registry)
    if row_hook is not None:
        row_hook(mapped, structured, conv)      # e.g. the grid check, before validation sees the rows
    accounts = [P._finish_account(m.key, m.headers, m.table, m.unverified_values, "groq") for m in mapped]
    completeness = completeness_service.assess(
        [AccountEvidence(a.key, a.headers, a.table, a.validation, a.unverified_values) for a in accounts],
        pages, chunks, multiple_accounts_suspected=False, engine_disagreements=0,
    )
    overall = confidence_service.overall([c for a in accounts for c in a.header_confidence.values()],
                                         [r for a in accounts for r in a.table.rows])
    report = P.EngineReport(requested="groq", used="groq")
    report.llm = run
    return P.Analysis(structured, accounts, report, pages, chunks, completeness, overall), run


def run_b(content: Any, pages: Any, client: OllamaClient) -> tuple[dict[str, Any], Any, MarkdownView]:
    md_content = copy.deepcopy(content)
    structured = structure_analyzer.analyze(md_content)
    view = build_markdown(md_content, structured, infer_conventions(structured, ocr=md_content.ocr_used))
    try:
        analysis, run = extract_from_lines(copy.deepcopy(content), view.lines, client, pages)
    except PipelineFailure as exc:
        return {"status": "failed", "error": {"code": exc.code, "message": exc.message}}, exc.run, view
    return serialize(analysis), run, view


def run_reference(content: Any, pages: Any) -> dict[str, Any]:
    try:
        return serialize(P.analyze(copy.deepcopy(content), engine="local", pages=pages))
    except P.ProcessingError as exc:
        return {"status": "failed", "error": {"code": exc.code, "message": str(exc)}}


# --------------------------------------------------------------------------
# Evaluation database (write-once rows)
# --------------------------------------------------------------------------


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY, created_at TEXT, label TEXT, statements INTEGER,
    model TEXT, digest TEXT, status TEXT, summary_path TEXT);
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, batch_id TEXT, created_at TEXT, statement_id TEXT,
    statement_sha256 TEXT, pages INTEGER, model TEXT, digest TEXT, source_tree TEXT, poc_version TEXT, status TEXT,
    local_only_ok INTEGER, run_dir TEXT, report_path TEXT);
CREATE TABLE IF NOT EXISTS pipeline_results (run_id TEXT, pipeline TEXT, status TEXT, error_code TEXT, total_ms INTEGER,
    llm_ms INTEGER, conversion_ms INTEGER, rows INTEGER, completeness TEXT, calls INTEGER, schema_valid_calls INTEGER,
    PRIMARY KEY (run_id, pipeline));
CREATE TABLE IF NOT EXISTS metrics (run_id TEXT, pipeline TEXT, metric TEXT, eligible INTEGER, labelled INTEGER,
    correct INTEGER, incorrect INTEGER, unscored INTEGER, value REAL, PRIMARY KEY (run_id, pipeline, metric));
"""


def db() -> sqlite3.Connection:
    con = sqlite3.connect(EVAL_DB)
    con.executescript(SCHEMA_SQL)
    return con


def record_run(con: sqlite3.Connection, data: dict[str, Any]) -> None:
    run = data["run"]
    con.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        run["run_id"], run.get("batch_id"), run["created_at"], data["statement"]["id"], data["statement"]["sha256"],
        data["statement"]["pages"], (data.get("model") or {}).get("name"), (data.get("model") or {}).get("digest"),
        run["source_tree"], POC_VERSION, run["status"], int(bool(data["local_only"].get("ok"))), run["run_dir"],
        run["report"]))
    for key, item in data["pipelines"].items():
        llm = item.get("llm") or {}
        result = item.get("result") or {}
        con.execute("INSERT INTO pipeline_results VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            run["run_id"], key, result.get("status"), (result.get("error") or {}).get("code"), item.get("total_ms"),
            llm.get("llm_ms"), item.get("conversion_ms"),
            sum(len(a.get("rows", [])) for a in result.get("accounts", [])),
            (result.get("completeness") or {}).get("status"), llm.get("calls"), llm.get("schema_valid_calls")))
        for metric in (data["scores"].get(key) or {}).get("metrics", []):
            con.execute("INSERT INTO metrics VALUES (?,?,?,?,?,?,?,?,?)", (
                run["run_id"], key, metric["metric"], metric["eligible"], metric["labelled"], metric["correct"],
                metric["incorrect"], metric["unscored"], metric["value"]))
    con.commit()


# --------------------------------------------------------------------------
# One statement
# --------------------------------------------------------------------------


def write_manifest(run_dir: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(run_dir.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            entries.append({"path": path.relative_to(run_dir).as_posix(), "bytes": path.stat().st_size,
                            "sha256": _sha256(path)})
    write_json(run_dir / "manifest.json", {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": entries})
    return entries


def grid_section(content: Any, pages: Any, path: Path, truth: Any, ctx: dict[str, Any],
                 llm: dict[str, tuple]) -> dict[str, Any]:
    """The table grid on this statement: the local engine with the grid (kept only if its checks are no worse), and
    the grid check of A's and B's recorded answers (replayed, no new model calls; kept only if no worse)."""
    from collections import Counter

    chosen, _grid_only, extractor, decision = analyze_local_with_grid(content, pages, str(path))
    summary = extractor.summary()
    out: dict[str, Any] = {"reference": {"used": decision["used"], "reason": decision["reason"],
                                         "moved": summary["amounts_moved"], "moves": summary["moves"],
                                         "score": scoring.score(serialize(chosen), truth)},
                           "pages": summary["pages"]}
    model_name = (ctx.get("model") or {}).get("name") or "model"
    for key, (lines, boxes, calls, stored) in llm.items():
        if (stored or {}).get("status") != "completed":
            out[key] = {"skipped": "the pipeline did not complete"}
            continue
        plain = extract_from_lines(copy.deepcopy(content), lines, ScriptedClient(replay_steps(calls, model_name), model_name), pages)[0]
        check = GridRowCheck(extractor, boxes)
        checked = extract_from_lines(copy.deepcopy(content), lines, ScriptedClient(replay_steps(calls, model_name), model_name),
                                     pages, row_hook=check)[0]
        keep = bool(check.log) and arithmetic_key(checked) <= arithmetic_key(plain)
        final = serialize(checked) if keep else serialize(plain)
        out[key] = {"checked": check.checked, "corrected": len(check.log),
                    "moves": dict(Counter(f"{c['from']} -> {c['to']}" for c in check.log)),
                    "used": "grid check" if keep else ("model as read" if not check.log else "model as read (checks got worse)"),
                    "replay_reproduces_result": serialize(plain)["accounts"] == stored["accounts"],
                    "score": scoring.score(final, truth)}
    return out


def new_run_id(name: str) -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{''.join(ch if ch.isalnum() else '-' for ch in name)[:40]}-{uuid.uuid4().hex[:6]}"


def run_statement(path: Path, ctx: dict[str, Any], *, label: str | None = None, truth_source: Path | None = None,
                  run_id: str | None = None, on_stage: Any = None) -> dict[str, Any]:
    """One statement through A, B and the reference. ``run_id`` reuses a folder the caller created (the upload UI keeps
    the uploaded file inside it); ``truth_source`` names the statement whose ground truth applies; ``on_stage`` is
    called with a short stage name as the run progresses."""
    stage = on_stage or (lambda _name: None)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    if run_id is None:
        run_id = new_run_id(path.stem)
        (EVAL_DIR / run_id).mkdir(parents=True, exist_ok=False)
    run_dir = EVAL_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    label = label or statement_id(path)
    print(f"[{time.strftime('%H:%M:%S')}] {label} -> {run_id}", flush=True)
    monitor: NetworkMonitor | None = ctx.get("monitor")
    violations_before = len(monitor.violations) if monitor else 0
    started_total = time.perf_counter()
    data: dict[str, Any] = {
        "run": {"run_id": run_id, "batch_id": ctx.get("batch_id"), "created_at": stamp, "run_dir": str(run_dir),
                "report": str(run_dir / "report.html"), "source_tree": ctx["source_tree"], "poc_version": POC_VERSION,
                "execution": "sequential: read once, then A, then B, then the reference", "status": "running",
                "mode": "markdown-only" if ctx["markdown_only"] else "full"},
        "environment": ctx["environment"], "model": ctx.get("model"), "pipelines": {}, "scores": {},
        "local_only": {}, "labels": ctx.get("labels", PIPELINES),
    }
    try:
        stage("reading the document (text layer / OCR)")
        content, pages, characteristics, read_ms = read_once(path)
        data["statement"] = {"id": label, "sha256": characteristics["sha256"],
                             "pages": characteristics["pages"], "characteristics": characteristics, "read_ms": read_ms}
        direct = document_lines(content)
        write_text(run_dir / "input" / "direct_lines.txt", "\n".join(f"{l.id}: {l.text}" for l in direct) + "\n")
        truth = scoring.load_truth(truth_source or path)
        data["ground_truth"] = {"available": truth is not None,
                                "file": scoring.truth_path(truth_source or path).name if truth is not None else None,
                                "rows": len((truth or {}).get("rows", [])),
                                "rows_with_balance": sum(1 for r in (truth or {}).get("rows", []) if r.get("balance") is not None),
                                "rows_with_description": sum(1 for r in (truth or {}).get("rows", []) if r.get("description")),
                                "header_fields": sorted((truth or {}).get("header", {})),
                                "absent_fields": sorted((truth or {}).get("absent", []))}

        results: dict[str, dict[str, Any] | None] = {}
        if ctx["markdown_only"]:
            stage("building the Markdown")
            md_content = copy.deepcopy(content)
            structured = structure_analyzer.analyze(md_content)
            view = build_markdown(md_content, structured, infer_conventions(structured, ocr=md_content.ocr_used))
            data["pipelines"]["B"] = {"conversion_ms": view.duration_ms, "result": None, "skipped": "markdown-only mode"}
        else:
            stage("Pipeline A: direct lines + local LLM")
            client_a = ctx["make_client"]()
            t = time.perf_counter()
            result_a, run_obj_a = run_a(content, pages, client_a)
            total_a = int((time.perf_counter() - t) * 1000)
            llm_a, calls_a = llm_record(client_a, run_obj_a)
            write_json(run_dir / "pipelines" / "A" / "result.json", result_a)
            write_json(run_dir / "pipelines" / "A" / "llm_outcomes.json", llm_a)
            write_json(run_dir / "pipelines" / "A" / "llm_calls.json", calls_a)
            data["pipelines"]["A"] = {"total_ms": total_a, "conversion_ms": 0, "llm": llm_a["summary"], "result": result_a}
            results["A"] = result_a
            print(f"    A: {result_a['status']} in {total_a / 1000:.0f}s", flush=True)

            stage("Pipeline B: Markdown + local LLM")
            client_b = ctx["make_client"]()
            t = time.perf_counter()
            result_b, run_obj_b, view = run_b(content, pages, client_b)
            total_b = int((time.perf_counter() - t) * 1000)
            llm_b, calls_b = llm_record(client_b, run_obj_b)
            write_json(run_dir / "pipelines" / "B" / "result.json", result_b)
            write_json(run_dir / "pipelines" / "B" / "llm_outcomes.json", llm_b)
            write_json(run_dir / "pipelines" / "B" / "llm_calls.json", calls_b)
            data["pipelines"]["B"] = {"total_ms": total_b, "conversion_ms": view.duration_ms, "llm": llm_b["summary"],
                                      "result": result_b}
            results["B"] = result_b
            print(f"    B: {result_b['status']} in {total_b / 1000:.0f}s (Markdown {view.duration_ms} ms)", flush=True)

        write_text(run_dir / "input" / "markdown.md", view.markdown)
        write_text(run_dir / "input" / "markdown_lines.txt", "\n".join(f"{l.id}: {l.text}" for l in view.lines) + "\n")
        write_json(run_dir / "input" / "markdown_provenance.json", view.provenance)
        write_json(run_dir / "input" / "markdown_warnings.json", view.warnings)
        data["markdown"] = {"stats": view.stats, "warnings": view.warnings, "columns": view.columns,
                            "faithful": view.faithful, "duration_ms": view.duration_ms}

        stage("Reference: deterministic local engine")
        t = time.perf_counter()
        result_ref = run_reference(content, pages)
        data["pipelines"]["reference"] = {"total_ms": int((time.perf_counter() - t) * 1000), "conversion_ms": 0,
                                          "llm": None, "result": result_ref}
        write_json(run_dir / "pipelines" / "reference" / "result.json", result_ref)
        results["reference"] = result_ref

        stage("scoring and writing the report")
        for key, result in results.items():
            data["scores"][key] = scoring.score(result, truth)
        matched = {k: set(v.get("matched_rows", [])) for k, v in data["scores"].items()}
        data["discrepancies"] = scoring.align(results, matched)
        data["scoring"] = {"version": scoring.SCORING_VERSION, "definitions": scoring.DEFINITIONS,
                           "rules": scoring.__doc__}
        write_json(run_dir / "score.json", data["scores"])
        write_json(run_dir / "discrepancies.json", data["discrepancies"])
        if not ctx["markdown_only"]:
            stage("table grid check (offline, on the recorded answers)")
            try:
                by_id = {l.id: l for l in direct}
                md_boxes = {p["id"]: [(by_id[d].page, by_id[d].bbox) for d in p["direct_line_ids"] if d in by_id]
                            for p in view.provenance}
                data["grid"] = grid_section(content, pages, path, truth, ctx, {
                    "A": (direct, {l.id: [(l.page, l.bbox)] for l in direct}, calls_a, result_a),
                    "B": (view.lines, md_boxes, calls_b, result_b)})
            except Exception as exc:  # noqa: BLE001 - the grid is an addition; its failure is shown, not hidden
                data["grid"] = {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
            write_json(run_dir / "grid.json", data["grid"])
        data["run"]["status"] = "completed"
    except Exception as exc:  # noqa: BLE001 - a crash is recorded, never hidden
        data["run"]["status"] = "error"
        data["run"]["error"] = {"type": type(exc).__name__, "message": str(exc)[:500],
                                "traceback": traceback.format_exc()[-4000:]}
        print(f"    ERROR: {type(exc).__name__}: {exc}", flush=True)

    data["run"]["total_ms"] = int((time.perf_counter() - started_total) * 1000)
    data.setdefault("statement", {"id": label, "sha256": _sha256(path), "pages": None, "characteristics": {}})
    new_violations = monitor.violations[violations_before:] if monitor else []
    data["local_only"] = {
        **ctx["guards"],
        "network_monitor": monitor.summary() if monitor else {"ok": None, "note": "markdown-only mode makes no model calls"},
        "violations_during_this_run": new_violations,
        "socket_guard": ctx["socket_guard"].summary(),
        "file_guard": FILE_GUARD.summary() if FILE_GUARD else {"installed": False},
        "app_db_sentinel_exists_after": APP_DB_SENTINEL.exists(),
        "live_db_unchanged_since_start": live_db_state() == ctx["live_db_before"],
    }
    data["local_only"]["ok"] = (not new_violations and not ctx["socket_guard"].blocked and FILE_GUARD is not None
                                and not FILE_GUARD.blocked
                                and not APP_DB_SENTINEL.exists() and bool(ctx["guards"].get("app_isolation", {}).get("ok")))
    if new_violations:
        data["run"]["status"] = "aborted: non-local connection detected"
    write_json(run_dir / "run.json", {k: v for k, v in data.items() if k not in ("pipelines", "discrepancies")} | {
        "pipelines": {k: {kk: vv for kk, vv in v.items() if kk != "result"} for k, v in data["pipelines"].items()}})
    write_json(run_dir / "network.json", data["local_only"])
    write_text(run_dir / "report.html", reporting.render_run(data))
    data["manifest"] = write_manifest(run_dir)
    if not ctx["markdown_only"]:
        with db() as con:
            record_run(con, data)
    print(f"    report: {run_dir / 'report.html'}", flush=True)
    return data


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def build_index() -> Path:
    """data/evaluations/index.html: every run folder and batch, including runs that did not finish."""
    runs, batches = [], []
    if EVAL_DIR.exists():
        for folder in sorted(EVAL_DIR.iterdir(), reverse=True):
            if not folder.is_dir() or folder.name.startswith("_"):
                continue
            if folder.name.startswith("batch-"):
                info = folder / "batch.json"
                batches.append({"dir": folder.name, "batch": json.loads(info.read_text(encoding="utf-8")) if info.exists() else None})
                continue
            info = folder / "run.json"
            data = json.loads(info.read_text(encoding="utf-8")) if info.exists() else None
            status = {}
            for key in ("A", "B", "reference"):
                result = folder / "pipelines" / key / "result.json"
                if result.exists():
                    status[key] = json.loads(result.read_text(encoding="utf-8")).get("status")
            runs.append({"dir": folder.name, "run": data, "status": status})
    path = EVAL_DIR / "index.html"
    write_text(path, reporting.render_index(runs, batches))
    return path


def list_statements() -> None:
    print("Permitted statements (pass one with --statement, or a set with --set):")
    for name in ("mocks", "corpus"):
        for path in statement_set(name):
            print(f"  [{name}] ../data/{statement_id(path)}")
    print("Refused: " + "; ".join(f"data/{k} ({v})" for k, v in REFUSED.items()))


GROQ_HOST = "api.groq.com"
GROQ_LABELS = {"A": "A - Direct OCR/Text + Groq LLM", "B": "B - Markdown + Groq LLM",
               "reference": "Reference - Existing Deterministic Local Engine"}


class RecordingGroq:
    """The application's GroqClient, recording every answer (for the grid check and the run folder)."""

    def __init__(self, inner: Any, cfg: Any) -> None:
        self.inner, self.cfg = inner, cfg
        self.model = inner.model
        self.calls: list[dict[str, Any]] = []

    def settings(self) -> dict[str, Any]:
        return {"provider": "groq", "model": self.model, "endpoint": f"https://{GROQ_HOST}", "temperature": 0,
                "seed": self.cfg.groq_seed, "reasoning_effort": self.cfg.groq_reasoning_effort,
                "max_completion_tokens": self.cfg.groq_max_completion_tokens, "format": "strict JSON schema"}

    def complete(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> Any:
        from app.services.llm_client import LLMError

        record: dict[str, Any] = {"started": time.strftime("%Y-%m-%dT%H:%M:%S"), "provider": "groq", "schema_name": schema_name}
        t0 = time.perf_counter()
        try:
            response = self.inner.complete(system=system, user=user, schema=schema, schema_name=schema_name)
        except LLMError as exc:
            record.update(error_code=exc.code, retry_after=exc.retry_after, latency_ms=int((time.perf_counter() - t0) * 1000))
            self.calls.append(record)
            raise
        record.update(content=response.content, done_reason="length" if response.finish_reason == "length" else "stop",
                      prompt_eval_count=response.input_tokens, eval_count=response.output_tokens,
                      latency_ms=response.latency_ms, reported_model=response.model)
        self.calls.append(record)
        return response


def _groq_settings() -> Any:
    """The application's settings with external calls on and the key from backend/.env, for this client only.
    The key is never printed; the process-wide settings stay as pinned (external calls off)."""
    from pydantic import SecretStr

    key = ""
    env = BACKEND_DIR / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("GROQ_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    return settings.model_copy(update={"allow_external_llm_calls": True, "groq_api_key": SecretStr(key)})


def groq_status() -> dict[str, Any]:
    cfg = _groq_settings()
    problems = [p["code"] for p in cfg.engine_problems("groq")]
    return {"ready": not problems, "problems": problems, "model": cfg.groq_model}


def _groq_allow() -> Any:
    import socket as _socket

    allowed: set[str] = set()

    def allow(ip: str) -> bool:
        if ip in allowed:
            return True
        try:
            allowed.update(info[4][0] for info in _socket.getaddrinfo(GROQ_HOST, 443))
        except OSError:
            return False
        return ip in allowed
    return allow


def build_context(model: str | None, pin: str | None, endpoint_url: str, markdown_only: bool,
                  provider: str = "ollama") -> dict[str, Any]:
    """All checks that must pass before any statement is processed. Raises LocalOnlyError or SystemExit if one fails.
    The network monitor is created here but started by the caller, around the work it watches."""
    guards: dict[str, Any] = {"app_isolation": verify_app_isolation(APP_DB_SENTINEL)}
    guards["pinned_environment"] = {k: ("<empty>" if v == "" else v) for k, v in PINNED_ENV.items()}
    ctx: dict[str, Any] = {"markdown_only": markdown_only, "source_tree": source_tree_hash(),
                           "live_db_before": live_db_state(), "guards": guards, "socket_guard": SocketGuard(),
                           "environment": {"python": sys.version.split()[0], "platform": platform.platform(),
                                           "poc_version": POC_VERSION}}
    if markdown_only:
        return ctx
    if provider == "groq":
        from app.services.llm_client import GroqClient

        cfg = _groq_settings()
        problems = [p["code"] for p in cfg.engine_problems("groq")]
        if problems:
            raise SystemExit(f"Groq is not usable: {problems}")
        guards["external_llm"] = {"provider": "groq", "endpoint": f"https://{GROQ_HOST}", "model": cfg.groq_model,
                                  "statement_text_sent_to_provider": True,
                                  "other_outside_connections": "refused by the in-process socket guard"}
        ctx["model"] = {"name": cfg.groq_model, "provider": "groq", "digest": None, "endpoint": f"https://{GROQ_HOST}"}
        ctx["labels"] = GROQ_LABELS
        ctx["socket_guard"] = SocketGuard(allow_remote=_groq_allow())
        ctx["make_client"] = lambda: RecordingGroq(GroqClient(cfg), cfg)
        print(f"model {cfg.groq_model} on Groq (external; statement text is sent to Groq)", flush=True)
        return ctx
    if not model or not pin:
        raise SystemExit("--model and --pin are required (or use --markdown-only)")
    endpoint = verify_endpoint(endpoint_url)
    guards["endpoint"] = endpoint
    identity = verify_model(endpoint["endpoint"], model, pin)
    ctx["model"] = identity
    monitor = NetworkMonitor(ollama_port=endpoint["port"])
    if not monitor.server_pids:
        raise LocalOnlyError("could not identify the Ollama server process listening on the loopback port")
    ctx["monitor"] = monitor
    ctx["make_client"] = lambda: OllamaClient(model, endpoint["endpoint"], seed=settings.groq_seed,
                                              num_predict=settings.groq_max_completion_tokens, monitor=monitor)
    print(f"model {identity['name']} digest {identity['digest'][:12]} (pin {pin}) at {endpoint['endpoint']}", flush=True)
    return ctx


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--list", action="store_true", help="list the permitted statements and exit")
    target.add_argument("--index", action="store_true", help="rebuild data/evaluations/index.html and exit")
    target.add_argument("--statement", help="a statement file under data/samples or data/corpus")
    target.add_argument("--set", choices=["mocks", "corpus", "selection"], help="a permitted statement set")
    parser.add_argument("--model", help="local Ollama model name, e.g. llama3.2:3b")
    parser.add_argument("--pin", help="expected model digest (at least the first 12 hex digits)")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Ollama endpoint (loopback only)")
    parser.add_argument("--label", default="", help="a short label for the batch")
    parser.add_argument("--markdown-only", action="store_true", help="build and inspect the Markdown; no model calls")
    parser.add_argument("--provider", choices=["ollama", "groq"], default="ollama",
                        help="groq is external: the statement text is sent to Groq")
    args = parser.parse_args(argv)
    install_file_guard()
    if args.list:
        list_statements()
        return 0
    if args.index:
        print(f"index: {build_index()}")
        return 0

    statements = [resolve_statement(args.statement)] if args.statement else statement_set(args.set)
    ctx = build_context(args.model, args.pin, args.endpoint, args.markdown_only, provider=args.provider)
    guards, monitor = ctx["guards"], ctx.get("monitor")

    batch_id = None
    if len(statements) > 1 or args.set:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        batch_id = f"batch-{stamp}-{args.set or 'single'}{'-' + args.label if args.label else ''}-{uuid.uuid4().hex[:6]}"
        ctx["batch_id"] = batch_id

    socket_guard = ctx["socket_guard"]
    runs: list[dict[str, Any]] = []
    batch_dir = EVAL_DIR / batch_id if batch_id else None
    if batch_dir is not None:
        batch_dir.mkdir(parents=True, exist_ok=False)

    def write_batch(final: bool) -> Path | None:
        """The batch summary, rewritten after every statement (marked in progress) and once more at the end."""
        if batch_dir is None:
            return None
        processed = {r["statement"]["id"] for r in runs if "statement" in r}
        unprocessed = [statement_id(p) for p in statements if statement_id(p) not in processed]
        batch = {"batch_id": batch_id, "set": args.set, "label": args.label, "model": ctx.get("model"),
                 "markdown_only": args.markdown_only, "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "state": "final" if final else f"in progress: {len(runs)} of {len(statements)} statements done",
                 "statements": [statement_id(p) for p in statements], "unprocessed": unprocessed,
                 "network_monitor": monitor.summary() if monitor else None,
                 "live_db_unchanged": live_db_state() == ctx["live_db_before"],
                 "app_db_sentinel_exists": APP_DB_SENTINEL.exists(), "labels": PIPELINES}
        summary_path = batch_dir / "summary.html"
        write_text(summary_path, reporting.render_batch(batch, runs, batch_dir))
        write_json(batch_dir / "batch.json", batch | {"runs": [
            {"run_id": r["run"]["run_id"], "statement": (r.get("statement") or {}).get("id"), "status": r["run"]["status"]}
            for r in runs]})
        if final and not args.markdown_only:
            with db() as con:
                con.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?)", (
                    batch_id, batch["created"], args.set or "", len(statements), args.model,
                    (ctx.get("model") or {}).get("digest"),
                    "stopped" if unprocessed else "completed", str(summary_path)))
                con.commit()
        return summary_path

    with socket_guard:
        if monitor is not None:
            monitor.start()
        try:
            for path in statements:
                data = run_statement(path, ctx)
                runs.append(data)
                write_batch(final=False)
                if monitor is not None and monitor.violation:
                    print("STOPPED: a non-local connection was detected; remaining statements were not processed.", flush=True)
                    break
        finally:
            if monitor is not None:
                monitor.stop()
                unload(guards["endpoint"]["endpoint"], args.model)
    summary_path = write_batch(final=True)
    if summary_path is not None:
        print(f"batch summary: {summary_path}", flush=True)
    print(f"index of all runs: {build_index()}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
