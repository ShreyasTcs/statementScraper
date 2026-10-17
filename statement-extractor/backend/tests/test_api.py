"""HTTP API: processing, review workflow, exports, deletion, security."""
from __future__ import annotations

import csv
import io
import time

import pytest
import statement_factory as sf
from llm_fixtures import oracle

from app.core.config import settings
from app.services.llm_client import ScriptedClient


def upload(client, path, engine=None, wait=True, headers=None):
    data = {"engine": engine} if engine else {}
    with open(path, "rb") as handle:
        return client.post(f"/api/v1/documents?wait={'true' if wait else 'false'}",
                           files={"file": (path.name, handle, "application/pdf")}, data=data, headers=headers or {})


def result(client, doc_id, headers=None):
    response = client.get(f"/api/v1/documents/{doc_id}/result", headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()


def rows_of(res, account=0):
    return [t for t in res["accounts"][account]["transactions"] if not t["deleted"]]


def field_of(res, name, account=0):
    return next(f for f in res["accounts"][account]["header"] if f["name"] == name)


# --------------------------------------------------------------------- processing


def test_health_and_config_expose_no_secrets(app_client):
    health = app_client.get("/api/v1/health").json()
    assert health["status"] == "ok" and health["default_engine"] == "local"
    config = app_client.get("/api/v1/config").json()
    names = {e["name"]: e for e in config["engines"]}
    assert names["local"]["available"] is True
    assert names["groq"]["available"] is False
    assert {p["code"] for p in names["groq"]["problems"]} >= {"external_calls_disabled", "groq_key_missing"}
    assert "test-key" not in str(config)


def test_upload_in_background_reports_real_progress_then_result(app_client, statement):
    path, truth = statement(sf.Spec())
    response = upload(app_client, path, wait=False)
    assert response.status_code == 202
    doc_id = response.json()["document_id"]
    deadline = time.time() + 60
    while True:
        status = app_client.get(f"/api/v1/documents/{doc_id}/status").json()["run"]
        if status["status"] in ("completed", "failed"):
            break
        assert time.time() < deadline, "processing did not finish"
        time.sleep(0.2)
    assert status["status"] == "completed" and status["stage"] == "done"
    assert status["engine_used"] == "local"
    res = result(app_client, doc_id)
    assert res["completeness"]["status"] == "complete"
    assert len(rows_of(res)) == len(truth["rows"])
    assert res["summary"]["headline"]["code"] == "summary_complete"
    listing = app_client.get("/api/v1/documents").json()
    assert any(item["id"] == doc_id and item["completeness"] == "complete" for item in listing["items"])


def test_engine_that_is_not_configured_is_refused_not_substituted(app_client, statement):
    path, _ = statement(sf.Spec())
    response = upload(app_client, path, engine="groq")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "external_calls_disabled"


def test_groq_engine_through_the_api(app_client, statement, groq_enabled):
    path, truth = statement(sf.Spec())
    groq_enabled(lambda: ScriptedClient([oracle(truth)]))
    response = upload(app_client, path, engine="groq")
    assert response.status_code == 201, response.text
    doc_id = response.json()["document_id"]
    status = app_client.get(f"/api/v1/documents/{doc_id}/status").json()["run"]
    assert status["engine_used"] == "groq" and status["model"] == "openai/gpt-oss-20b"
    assert status["llm_calls"] == 1 and status["chunks"][0]["status"] == "ok"
    res = result(app_client, doc_id)
    assert res["engine"]["used"] == "groq"
    assert all(t["source_engine"] == "groq" and t["evidence"]["lines"] for t in rows_of(res))


def test_failed_groq_run_is_recorded_with_its_code(app_client, statement, groq_enabled):
    from app.services.llm_client import LLMError

    path, _ = statement(sf.Spec())
    groq_enabled(lambda: ScriptedClient([LLMError("llm_auth", "bad key", fatal=True)]))
    doc_id = upload(app_client, path, engine="groq").json()["document_id"]
    status = app_client.get(f"/api/v1/documents/{doc_id}/status").json()["run"]
    assert status["status"] == "failed" and status["error"]["code"] == "llm_auth"
    res = result(app_client, doc_id)
    assert res["completeness"]["status"] == "failed"
    assert res["notice"]["code"] == "llm_auth"
    assert res["accounts"] == []  # no local rows passed off as Groq results


# --------------------------------------------------------------------- review


def test_review_workflow_corrections_are_audited_and_revalidated(app_client, statement):
    path, truth = statement(sf.Spec(omit_index=4))  # one row missing: the books do not balance
    doc_id = upload(app_client, path).json()["document_id"]
    res = result(app_client, doc_id)
    assert res["completeness"]["status"] == "needs_review"
    missing = truth["rows"][4]

    # Add the missing row in its place (after displayed row 4).
    created = app_client.post(f"/api/v1/documents/{doc_id}/transactions", json={
        "after_index": 4, "date": missing["date"], "description": missing["description"],
        "debit": missing["debit"], "credit": missing["credit"], "balance": missing["balance"]})
    assert created.status_code == 201, created.text
    res = created.json()
    assert res["completeness"]["status"] == "complete", res["completeness"]["reasons"]
    row = rows_of(res)[4]
    assert row["source_engine"] == "user" and row["description"] == missing["description"]

    # Edit an amount: the original is preserved, the change audited, the books re-checked.
    target = rows_of(res)[1]
    for bad, code in (("twelve", "invalid_amount"), ("-5.00", "amount_negative")):
        edited = app_client.patch(f"/api/v1/documents/{doc_id}/transactions/{target['id']}", json={"debit": bad})
        assert edited.status_code == 422 and edited.json()["detail"]["code"] == code
    edited = app_client.patch(f"/api/v1/documents/{doc_id}/transactions/{target['id']}", json={"debit": "85.35"})
    res = edited.json()
    changed = next(t for t in rows_of(res) if t["id"] == target["id"])
    assert changed["debit"] == "85.35" and changed["corrected"] is True
    assert changed["original"]["debit"] == target["debit"]
    assert res["completeness"]["status"] == "needs_review"

    # Undo by entering the right value again.
    res = app_client.patch(f"/api/v1/documents/{doc_id}/transactions/{target['id']}",
                           json={"debit": target["debit"]}).json()
    assert res["completeness"]["status"] == "complete"

    # Delete and restore are soft and audited.
    res = app_client.delete(f"/api/v1/documents/{doc_id}/transactions/{target['id']}").json()
    assert any(t["id"] == target["id"] and t["deleted"] for t in res["accounts"][0]["transactions"])
    assert res["completeness"]["status"] == "needs_review"
    res = app_client.post(f"/api/v1/documents/{doc_id}/transactions/{target['id']}/restore").json()
    assert res["completeness"]["status"] == "complete"

    corrections = app_client.get(f"/api/v1/documents/{doc_id}/corrections").json()
    actions = [c["action"] for c in corrections]
    assert actions[:1] == ["create"] and actions.count("update") == 2 and "delete" in actions and "restore" in actions
    update = next(c for c in corrections if c["action"] == "update")
    assert update["attribute"] == "debit" and update["old_value"] == target["debit"] and update["new_value"] == "85.35"


def test_field_correction_uses_the_statement_conventions(app_client, statement):
    path, _ = statement(sf.Spec())
    doc_id = upload(app_client, path).json()["document_id"]
    res = result(app_client, doc_id)
    holder = field_of(res, "account_holder")
    res = app_client.patch(f"/api/v1/documents/{doc_id}/fields/{holder['id']}", json={"value": "Jane Q. Example"}).json()
    changed = field_of(res, "account_holder")
    assert changed["value"] == "Jane Q. Example" and changed["corrected"] and changed["original_value"] == "Jane Example"
    period = field_of(res, "period_to")
    bad = app_client.patch(f"/api/v1/documents/{doc_id}/fields/{period['id']}", json={"value": "not a date"})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_date"
    res = app_client.patch(f"/api/v1/documents/{doc_id}/fields/{period['id']}", json={"value": "31/08/2026"}).json()
    assert field_of(res, "period_to")["normalized"] == "2026-08-31"


def test_duplicate_can_be_confirmed_genuine_or_deleted(app_client, statement):
    path, _ = statement(sf.Spec(duplicate_index=3))
    doc_id = upload(app_client, path).json()["document_id"]
    res = result(app_client, doc_id)
    later, _ = res["accounts"][0]["duplicates"][0]
    dup = next(t for t in rows_of(res) if t["index"] == later)
    res = app_client.delete(f"/api/v1/documents/{doc_id}/transactions/{dup['id']}").json()
    assert res["accounts"][0]["duplicates"] == []
    assert res["completeness"]["status"] == "complete"


def test_unresolved_line_can_be_dismissed(app_client, statement, groq_enabled):
    path, truth = statement(sf.Spec())

    def drop(payload, _lines):
        payload["transactions"].pop(2)
        return payload

    groq_enabled(lambda: ScriptedClient([oracle(truth, drop)]))
    doc_id = upload(app_client, path, engine="groq").json()["document_id"]
    res = result(app_client, doc_id)
    assert res["accounts"][0]["unresolved"]
    res = app_client.post(f"/api/v1/documents/{doc_id}/unresolved/A1/0/dismiss").json()
    assert res["accounts"][0]["unresolved"][0]["dismissed"] is True
    reasons = [r["code"] for r in res["completeness"]["reasons"]]
    assert "unresolved_lines" not in reasons
    assert "checks_failed" in reasons  # the missing row still breaks the books: dismissing proves nothing


# --------------------------------------------------------------------- exports & deletion


def test_csv_export_neutralises_formulas_and_reflects_corrections(app_client, statement):
    spec = sf.Spec()
    txns = sf.default_txns(spec)
    txns[0].description = "=HYPERLINK(\"http://example.invalid\",\"x\")"
    path, _ = statement(spec, txns)
    doc_id = upload(app_client, path).json()["document_id"]
    res = result(app_client, doc_id)
    second = rows_of(res)[1]
    app_client.patch(f"/api/v1/documents/{doc_id}/transactions/{second['id']}", json={"description": "@SUM(A1:A2)"})
    third = rows_of(res)[2]
    app_client.delete(f"/api/v1/documents/{doc_id}/transactions/{third['id']}")
    response = app_client.get(f"/api/v1/documents/{doc_id}/transactions.csv")
    assert response.status_code == 200
    table = list(csv.reader(io.StringIO(response.text)))
    header, body = table[0], table[1:]
    desc = header.index("Description")
    assert body[0][desc].startswith("'=")
    assert body[1][desc] == "'@SUM(A1:A2)"
    assert len(body) == len(rows_of(res)) - 1  # the deleted row is not exported
    assert all(not cell.startswith("'") for cell in (r[header.index("Balance")] for r in body))
    json_export = app_client.get(f"/api/v1/documents/{doc_id}/result.json")
    assert json_export.status_code == 200 and json_export.json()["corrected"] is True


def test_delete_removes_document_data_and_file(app_client, statement, tmp_root):
    path, _ = statement(sf.Spec())
    doc_id = upload(app_client, path).json()["document_id"]
    assert (settings.upload_dir / doc_id).exists()
    assert app_client.delete(f"/api/v1/documents/{doc_id}").status_code == 204
    assert app_client.get(f"/api/v1/documents/{doc_id}/result").status_code == 404
    assert not (settings.upload_dir / doc_id).exists()


# --------------------------------------------------------------------- limits & security


def test_rejects_wrong_type_oversize_and_too_many_pages(app_client, statement, tmp_path, monkeypatch):
    fake = tmp_path / "statement.pdf"
    fake.write_bytes(b"MZ not a pdf at all")
    response = upload(app_client, fake)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "unsupported_file"

    path, _ = statement(sf.Spec())
    monkeypatch.setattr(settings, "max_file_size_bytes", 1000)
    response = upload(app_client, path)
    assert response.status_code == 413 and response.json()["detail"]["code"] == "file_too_large"
    monkeypatch.setattr(settings, "max_file_size_bytes", 25 * 1024 * 1024)

    monkeypatch.setattr(settings, "max_pages", 0)
    response = upload(app_client, path)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "too_many_pages"


def test_password_protected_pdf_is_refused(app_client, statement, tmp_path):
    import fitz

    path, _ = statement(sf.Spec())
    locked = tmp_path / "locked.pdf"
    with fitz.open(path) as doc:
        doc.save(locked, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="user-secret", owner_pw="owner-secret")
    response = upload(app_client, locked)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "password_protected"


def test_validation_errors_do_not_echo_input(app_client, statement):
    path, _ = statement(sf.Spec())
    doc_id = upload(app_client, path).json()["document_id"]
    res = result(app_client, doc_id)
    txn = rows_of(res)[0]
    response = app_client.patch(f"/api/v1/documents/{doc_id}/transactions/{txn['id']}",
                                json={"confirmed": "SECRET-VALUE-123"})
    assert response.status_code == 422
    assert "SECRET-VALUE-123" not in response.text
    assert response.json()["detail"]["code"] == "invalid_request"


def test_token_auth_isolates_users(app_client, statement, monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "auth_mode", "token")
    monkeypatch.setattr(settings, "api_tokens", SecretStr("alice:alice-token-0123456789,bob:bob-token-0123456789"))
    alice = {"Authorization": "Bearer alice-token-0123456789"}
    bob = {"Authorization": "Bearer bob-token-0123456789"}
    path, _ = statement(sf.Spec())
    assert upload(app_client, path).status_code == 401
    assert upload(app_client, path, headers={"Authorization": "Bearer wrong-token-0000000000"}).status_code == 401
    doc_id = upload(app_client, path, headers=alice).json()["document_id"]
    assert app_client.get(f"/api/v1/documents/{doc_id}/result", headers=alice).status_code == 200
    for route in ("result", "status", "preview", "transactions.csv"):
        assert app_client.get(f"/api/v1/documents/{doc_id}/{route}", headers=bob).status_code == 404
    assert app_client.delete(f"/api/v1/documents/{doc_id}", headers=bob).status_code == 404
    assert all(item["id"] != doc_id for item in app_client.get("/api/v1/documents", headers=bob).json()["items"])
    assert app_client.get("/api/v1/health").status_code == 200  # health needs no token


def test_interrupted_runs_are_marked_failed_on_start():
    from app.db.session import SessionLocal
    from app.models import Document, ProcessingRun
    from app.services import jobs

    with SessionLocal() as db:
        document = Document(original_filename="x.pdf", stored_filename="x.pdf", file_type="pdf",
                            mime_type="application/pdf", file_size_bytes=1, sha256="0" * 64, preview_path="")
        db.add(document)
        db.flush()
        run = ProcessingRun(document_id=document.id, status="processing", stage="llm")
        db.add(run)
        db.commit()
        run_id = run.id
    assert jobs.recover_interrupted() >= 1
    with SessionLocal() as db:
        run = db.get(ProcessingRun, run_id)
        assert run.status == "failed" and run.error_code == "interrupted"


@pytest.mark.parametrize("value", ["=1+1", "+1", "-cmd", "@x", "\tx"])
def test_csv_safe_prefixes_formula_starts(value):
    from app.services.result_service import csv_safe

    assert csv_safe(value).startswith("'")
    assert csv_safe("-12.50") == "-12.50"
