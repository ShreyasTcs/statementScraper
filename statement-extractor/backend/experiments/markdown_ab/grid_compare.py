"""Before/after comparison of the table grid on the application's deterministic (local) engine.

    cd statement-extractor/backend
    .venv/Scripts/python -m experiments.markdown_ab.grid_compare --set synthetic   # data/samples + data/corpus
    .venv/Scripts/python -m experiments.markdown_ab.grid_compare --set real        # data/companySamples (local, read-only)
    .venv/Scripts/python -m experiments.markdown_ab.grid_compare --set uploads     # data/uploads (local, read-only)

For each statement the document is read once. "Before" is ``analyze(engine="local")`` exactly as the application runs
it; "after" is the same call with the application's table extractor replaced by ``GridTableExtractor`` for the
duration of that call only. Both results are scored against the existing ground truth where it exists; without
ground truth the statement's own arithmetic is compared (running-balance and totals checks, swapped-direction flags,
completeness). Results go to data/evaluations/grid-<id>/: a summary page, one page per statement, the grid as JSON
and a picture of the grid for every page.

Real statements and uploads are identified by a hash only, and their pages show counts, never values. Nothing is
sent anywhere: the in-process socket guard refuses every non-loopback connection, and no model is used.
"""
from __future__ import annotations

from experiments.markdown_ab import run as R  # isort: skip  (pins the application's settings first)

import argparse  # noqa: E402
import copy  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import uuid  # noqa: E402
from collections import Counter  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

from app.services import pipeline as P  # noqa: E402
from experiments.markdown_ab import report as rep  # noqa: E402
from experiments.markdown_ab import score as scoring  # noqa: E402
from experiments.markdown_ab.grid import GridRowCheck, analyze_local_with_grid, arithmetic_key, draw_overlay, replay_steps  # noqa: E402
from app.services.llm_client import ScriptedClient  # noqa: E402
from app.services.page_text import document_lines  # noqa: E402
from experiments.markdown_ab.local_llm import SocketGuard, verify_app_isolation  # noqa: E402

COMPLETENESS_RANK = {"complete": 0, "partial": 1, "needs_review": 2, "failed": 3}
KEY_METRICS = ["transactions", "amount_direction", "running_balance", "statement_fields", "opening_balance", "closing_balance"]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def statements(name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if name in ("synthetic", "all"):
        for set_name in ("mocks", "corpus"):
            for path in R.statement_set(set_name):
                out.append({"path": path, "id": R.statement_id(path), "kind": "synthetic", "truth": True})
    real_hashes: set[str] = set()
    if name in ("real", "uploads", "all"):
        for path in sorted((R.DATA_DIR / "companySamples").iterdir()):
            if path.suffix.lower() in R.SUFFIXES:
                digest = _sha(path)
                real_hashes.add(digest)
                if name in ("real", "all"):
                    out.append({"path": path, "id": f"real/{digest[:12]}", "kind": "real", "truth": True})
    if name in ("uploads", "all"):
        seen = set(real_hashes)
        skipped = 0
        for path in sorted((R.DATA_DIR / "uploads").glob("*/*")):
            if path.suffix.lower() not in R.SUFFIXES:
                continue
            digest = _sha(path)
            if digest in seen:
                skipped += 1
                continue
            seen.add(digest)
            out.append({"path": path, "id": f"upload/{digest[:12]}", "kind": "upload", "truth": False})
        if skipped:
            print(f"{skipped} upload(s) are identical to a real statement or to another upload and were not run twice")
    return out


def diagnostics(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "completed":
        return {"status": result.get("status"), "error": (result.get("error") or {}).get("code")}
    rows = [r for a in result.get("accounts", []) for r in a.get("rows", [])]
    issues = Counter()
    for r in rows:
        for text in r.get("issues", []):
            issues[text.split(" '")[0][:60]] += 1
    failed = [c.get("key") for a in result.get("accounts", []) for c in a.get("checks", []) if c.get("status") == "fail"]
    return {
        "status": "completed", "rows": len(rows),
        "balance_mismatches": sum(1 for r in rows if r.get("balance_check") == "mismatch"),
        "swapped_flags": sum(1 for r in rows for t in r.get("issues", []) if "swapped" in t.lower()),
        "failed_checks": len(failed), "failed_check_keys": failed,
        "completeness": (result.get("completeness") or {}).get("status"),
        "mode": ((result.get("accounts") or [{}])[0].get("totals") or {}).get("mode"),
        "issue_kinds": dict(issues),
    }


def row_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    fields = ("date", "debit", "credit", "amount", "balance")
    rb = [r for a in before.get("accounts", []) for r in a.get("rows", [])]
    ra = [r for a in after.get("accounts", []) for r in a.get("rows", [])]
    changed = sum(1 for x, y in zip(rb, ra) if any(x.get(f) != y.get(f) for f in fields))
    moved = sum(1 for x, y in zip(rb, ra)
                if (x.get("debit") is not None and y.get("credit") == x.get("debit") and y.get("debit") is None)
                or (x.get("credit") is not None and y.get("debit") == x.get("credit") and y.get("credit") is None))
    return {"rows_before": len(rb), "rows_after": len(ra), "rows_changed": changed, "debit_credit_moves": moved,
            "identical": len(rb) == len(ra) and changed == 0}


def _m(score: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((m for m in score.get("metrics", []) if m["metric"] == name), None)


def verdict(item: dict[str, Any]) -> str:
    if item.get("error"):
        return "error"
    if item["changes"]["identical"]:
        return "identical"
    if item["truth"]:
        better = worse = 0
        for name in KEY_METRICS:
            b, a = _m(item["score_before"], name), _m(item["score_after"], name)
            if b and a and b["labelled"]:
                better += a["correct"] > b["correct"]
                worse += a["correct"] < b["correct"]
        b_extra = (_m(item["score_before"], "transactions") or {}).get("extra", 0)
        a_extra = (_m(item["score_after"], "transactions") or {}).get("extra", 0)
        worse += a_extra > b_extra
        better += a_extra < b_extra
        return "worse" if worse else "better" if better else "changed, same score"
    b, a = item["diag_before"], item["diag_after"]
    key = lambda d: (d.get("failed_checks", 99), d.get("balance_mismatches", 99), d.get("swapped_flags", 99),
                     COMPLETENESS_RANK.get(d.get("completeness"), 9))
    return "better (arithmetic)" if key(a) < key(b) else "worse (arithmetic)" if key(a) > key(b) else "changed, same arithmetic"


GROQ_HOST = "api.groq.com"


class RecordingGroq:
    """Wraps the application's GroqClient and records every answer (for the offline grid check), like OllamaClient."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.model = inner.model
        self.calls: list[dict[str, Any]] = []

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


def groq_context() -> dict[str, Any]:
    """A context for the user-authorised Groq run: the application's own GroqClient with the key from backend/.env
    (never printed) and external calls enabled for this process only."""
    from pydantic import SecretStr

    from app.core.config import settings
    from app.services.llm_client import GroqClient

    key = ""
    for line in (R.BACKEND_DIR / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith("GROQ_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
    cfg = settings.model_copy(update={"allow_external_llm_calls": True, "groq_api_key": SecretStr(key)})
    problems = [p["code"] for p in cfg.engine_problems("groq")]
    if problems:
        raise SystemExit(f"Groq is not usable: {problems}")
    import socket as _socket

    allowed_ips: set[str] = set()

    def allow(ip: str) -> bool:
        if ip in allowed_ips:
            return True
        try:
            allowed_ips.update(info[4][0] for info in _socket.getaddrinfo(GROQ_HOST, 443))
        except OSError:
            return False
        return ip in allowed_ips

    return {"model": {"name": cfg.groq_model, "provider": "groq", "digest": None,
                      "authorisation": "user-authorised on 2026-10-15: the 7 real statements may be sent to Groq"},
            "make_client": lambda: RecordingGroq(GroqClient(cfg)),
            "socket_guard": SocketGuard(allow_remote=allow), "monitor": None}


def llm_before_after(content: Any, pages: Any, extractor: Any, ctx: dict[str, Any], truth: Any, out: Path,
                     recorded: Path | None = None) -> dict[str, Any]:
    """A and B once with the local model; then the same recorded answers replayed through the same path, once
    unchanged (must reproduce the stored result) and once with the grid check before validation (kept only if the
    statement's checks are no worse)."""
    report: dict[str, Any] = {}
    model = ctx["model"]["name"]
    direct = document_lines(content)
    for key in ("A", "B"):
        t0 = time.perf_counter()
        if key == "A":
            lines = direct
            boxes = {l.id: [(l.page, l.bbox)] for l in direct}
        else:
            md_content = copy.deepcopy(content)
            structured_md = P.structure_analyzer.analyze(md_content)
            view = R.build_markdown(md_content, structured_md, P.infer_conventions(structured_md, ocr=md_content.ocr_used))
            lines = view.lines
            by_id = {l.id: l for l in direct}
            boxes = {p["id"]: [(by_id[d].page, by_id[d].bbox) for d in p["direct_line_ids"] if d in by_id]
                     for p in view.provenance}
        if recorded is not None:
            calls = json.loads((recorded / f"llm_{key}_calls.json").read_text(encoding="utf-8"))
            try:
                stored = R.serialize(R.extract_from_lines(copy.deepcopy(content), lines,
                                                          ScriptedClient(replay_steps(calls, model), model), pages)[0])
            except R.PipelineFailure as exc:
                stored = {"status": "failed", "error": {"code": exc.code, "message": exc.message}}
        else:
            client = ctx["make_client"]()
            stored = R.run_a(content, pages, client)[0] if key == "A" else R.run_b(content, pages, client)[0]
            calls = client.calls
        llm_ms = int((time.perf_counter() - t0) * 1000)
        R.write_json(out / f"llm_{key}_calls.json", calls)
        item: dict[str, Any] = {"llm_ms": llm_ms, "calls": len(calls), "status": stored.get("status"),
                                "source": "replayed recorded answers" if recorded is not None else "live local model"}
        if stored.get("status") != "completed":
            item.update(score_before=scoring.score(stored, truth), score_after=scoring.score(stored, truth),
                        decision={"used": "none", "reason": "the pipeline failed; nothing to check"}, corrections=0)
            report[key] = item
            continue
        plain = R.extract_from_lines(copy.deepcopy(content), lines, ScriptedClient(replay_steps(calls, model), model), pages)[0]
        check = GridRowCheck(extractor, boxes)
        checked = R.extract_from_lines(copy.deepcopy(content), lines, ScriptedClient(replay_steps(calls, model), model),
                                       pages, row_hook=check)[0]
        replay_matches = R.serialize(plain)["accounts"] == stored["accounts"]
        keep = arithmetic_key(checked) <= arithmetic_key(plain)
        after = R.serialize(checked) if keep else R.serialize(plain)
        as_read = {"status": "completed", "accounts": [{"rows": check.rows_as_read, "totals": {}}]}
        after_check = {"status": "completed", "accounts": [{"rows": check.rows_after_check, "totals": {}}]}
        item.update(score_before=scoring.score(stored, truth), score_after=scoring.score(after, truth),
                    score_grid_only=scoring.score(R.serialize(checked), truth),
                    score_model_as_read=scoring.score(as_read, truth), score_after_grid_check=scoring.score(after_check, truth),
                    diag_before=diagnostics(stored), diag_after=diagnostics(after), replay_reproduces_stored=replay_matches,
                    corrections=len(check.log), moves=dict(Counter(f"{c['from']} -> {c['to']}" for c in check.log)),
                    amounts_checked=check.checked,
                    decision={"used": "grid check" if keep and check.log else "model as read",
                              "reason": (f"{len(check.log)} correction(s); checks {arithmetic_key(plain)} -> {arithmetic_key(checked)}"
                                         if check.log else "the grid agreed with every amount the model placed")})
        report[key] = item
    return report


def run_one(entry: dict[str, Any], out_dir: Path, ctx: dict[str, Any] | None = None, replay_from: Path | None = None) -> dict[str, Any]:
    path: Path = entry["path"]
    slug = entry["id"].replace("/", "_").replace(".", "_")
    item: dict[str, Any] = {"id": entry["id"], "kind": entry["kind"], "truth": entry["truth"], "pages": None}
    started = time.perf_counter()
    try:
        content, pages, characteristics, read_ms = R.read_once(path)
        item.update(pages=characteristics["pages"], method=characteristics["method"], read_ms=read_ms)
        before = R.serialize(P.analyze(copy.deepcopy(content), engine="local", pages=pages))
        chosen, grid_only, extractor, decision = analyze_local_with_grid(content, pages, str(path))
        after, raw = R.serialize(chosen), R.serialize(grid_only)
        truth = scoring.load_truth(path) if entry["truth"] else None
        item.update(score_before=scoring.score(before, truth), score_after=scoring.score(after, truth),
                    score_grid_only=scoring.score(raw, truth), diag_grid_only=diagnostics(raw), decision=decision,
                    diag_before=diagnostics(before), diag_after=diagnostics(after), changes=row_changes(before, after),
                    grid=extractor.summary())
        pictures = []
        structured = P.structure_analyzer.analyze(copy.deepcopy(content))
        conv_pictures = P.infer_conventions(structured, ocr=content.ocr_used)
        for page in structured.pages:
            grid = extractor._grid(page, conv_pictures)
            mapping = next((m for (p, _), m in extractor.mappings.items() if p == page.page_number), None)
            native = bool(page.blocks) and all(b.source != "ocr" for b in page.blocks)
            name = f"{slug}/grid-page-{page.page_number}.png"
            draw_overlay(page, grid, mapping, str(path), native, out_dir / name)
            pictures.append(name)
        item["pictures"] = pictures
        if replay_from is not None and all((replay_from / slug / f"llm_{k}_calls.json").exists() for k in ("A", "B")):
            item["llm"] = llm_before_after(content, pages, extractor, {"model": {"name": "llama3.2:3b"}}, truth, out_dir / slug,
                                           recorded=replay_from / slug)
        elif ctx is not None:
            item["llm"] = llm_before_after(content, pages, extractor, ctx, truth, out_dir / slug)
        if entry["kind"] == "synthetic":
            R.write_json(out_dir / slug / "before.json", before)
            R.write_json(out_dir / slug / "after.json", after)
    except Exception as exc:  # noqa: BLE001 - recorded, never hidden
        item["error"] = {"type": type(exc).__name__, "message": str(exc)[:300], "traceback": traceback.format_exc()[-3000:]}
    item["ms"] = int((time.perf_counter() - started) * 1000)
    item["verdict"] = verdict(item) if "changes" in item or item.get("error") else "error"
    R.write_json(out_dir / slug / "comparison.json", item)
    return item


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def _cell(score: dict[str, Any], name: str) -> str:
    m = _m(score, name)
    if not m or not m["labelled"]:
        return "<span class=muted>unscored</span>"
    return f"{m['correct']}/{m['labelled']}"


VERDICT_BADGE = {"identical": "info", "better": "ok", "better (arithmetic)": "ok", "worse": "bad", "worse (arithmetic)": "bad",
                 "changed, same score": "warn", "changed, same arithmetic": "warn", "error": "bad"}


def render_summary(items: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    counts = Counter(i["verdict"] for i in items)
    out = ["<header class='top'><h1>Table grid · before / after (deterministic engine)</h1>"
           f"<p>{rep.e(meta['set'])} · {len(items)} statement(s) · {rep.e(meta['created'])} UTC</p></header><main>",
           "<section><h2>Outcome</h2><p>" + " ".join(rep.badge(f"{k}: {v}", VERDICT_BADGE.get(k, "info")) for k, v in sorted(counts.items())) + "</p>"
           "<p class='muted'>Before: the application's local engine as it ships. After: the same engine with the amount "
           "columns decided by the page grid. 'Better'/'worse' compare the ground-truth metrics (transactions, amount and "
           "direction, running balance, statement fields, opening/closing, extra rows); without ground truth, the "
           "statement's own checks (failed checks, balance mismatches, swapped flags, completeness).</p></section>"]
    out.append("<section><h2>Per statement</h2><div class='scroll'><table><tr><th>Statement</th><th class='num'>Pages</th>"
               "<th>Text / OCR</th><th>Transactions<br>before → after</th><th>Amount + direction<br>before → after</th>"
               "<th>Direction errors<br>before → after</th><th>Running balance<br>before → after</th>"
               "<th>Failed checks<br>before → after</th><th>Completeness<br>before → after</th><th class='num'>Amounts moved by the grid</th>"
               "<th>Verdict</th><th>Detail</th></tr>")
    for i in items:
        if i.get("error"):
            out.append(f"<tr><td>{rep.e(i['id'])}</td><td colspan='10'>{rep.badge('error', 'bad')} {rep.e(i['error']['type'])}: "
                       f"{rep.e(i['error']['message'])}</td><td></td></tr>")
            continue
        sb, sa, db, da = i["score_before"], i["score_after"], i["diag_before"], i["diag_after"]
        dir_b = (_m(sb, "amount_only") or {}).get("direction_errors") if i["truth"] else None
        dir_a = (_m(sa, "amount_only") or {}).get("direction_errors") if i["truth"] else None
        slug = i["id"].replace("/", "_").replace(".", "_")
        out.append(
            f"<tr><td>{rep.e(i['id'])}</td><td class='num'>{rep.e(i['pages'])}</td><td>{rep.e(i.get('method'))}</td>"
            f"<td>{_cell(sb, 'transactions')} → {_cell(sa, 'transactions')}</td>"
            f"<td>{_cell(sb, 'amount_direction')} → {_cell(sa, 'amount_direction')}</td>"
            f"<td>{rep.e(dir_b if dir_b is not None else '–')} → {rep.e(dir_a if dir_a is not None else '–')}</td>"
            f"<td>{_cell(sb, 'running_balance')} → {_cell(sa, 'running_balance')}</td>"
            f"<td>{rep.e(db.get('failed_checks'))} → {rep.e(da.get('failed_checks'))}</td>"
            f"<td>{rep.e(db.get('completeness'))} → {rep.e(da.get('completeness'))}</td>"
            f"<td class='num'>{rep.e(i['grid']['amounts_moved'])}</td>"
            f"<td>{rep.badge(i['verdict'], VERDICT_BADGE.get(i['verdict'], 'info'))}</td>"
            f"<td><a href='{rep.e(slug)}/index.html'>grid</a></td></tr>")
    out.append("</table></div></section><footer>Generated by experiments/markdown_ab/grid_compare.py. No scripts, no external "
               "resources. Pictures of the grid are local files in this folder.</footer></main>")
    return rep.page("Grid before/after", "".join(out))


def render_item(item: dict[str, Any]) -> str:
    out = [f"<header class='top'><h1>Table grid · {rep.e(item['id'])}</h1><p>{rep.e(item.get('method'))} · "
           f"{rep.e(item.get('pages'))} page(s) · verdict {rep.e(item['verdict'])}</p></header><main>"]
    if item.get("error"):
        out.append(f"<section><pre>{rep.e(item['error']['traceback'])}</pre></section></main>")
        return rep.page("Grid", "".join(out))
    grid = item["grid"]
    out.append("<section><h2>What the grid decided</h2>" + rep.kv([
        ("Amounts moved to another column", rep.e(grid["amounts_moved"]) + " " + rep.e(grid["moves"] or "")),
        ("Amounts where the grid agreed with the original", rep.e(grid["amounts_confirmed"])),
        ("Rows changed", rep.e(item["changes"]["rows_changed"])),
        ("Result used", rep.e((item.get("decision") or {}).get("used")) + " - " + rep.e((item.get("decision") or {}).get("reason"))),
        ("Rows before / after", rep.e(f"{item['changes']['rows_before']} / {item['changes']['rows_after']}")),
    ]) + "<h3>Heading columns matched to grid columns</h3><ul>"
        + "".join(f"<li>page {rep.e(n['page'])}: {rep.e(n['result'])}</li>" for n in grid["matchings"]) + "</ul>")
    out.append("<h3>Grid per page</h3><div class='scroll'><table><tr><th>Page</th><th>Amount columns (x0–x1, right edge, amounts)</th>"
               "<th>Ruling lines</th><th>Picture</th></tr>")
    for g, pic in zip(grid["pages"], item.get("pictures", [])):
        name = pic.split("/", 1)[1]
        if g is None:
            out.append(f"<tr><td>–</td><td class='muted'>no transaction table found on this page</td><td></td><td><a href='{rep.e(name)}'>open</a></td></tr>")
            continue
        bands = "; ".join(f"{b['x0']}–{b['x1']} (edge {b['right_edge']}, {b['amounts']}, {b['split_by']})" for b in g["bands"])
        out.append(f"<tr><td>{rep.e(g['page'])}</td><td>{rep.e(bands)}</td><td>{rep.e(g['note'])}</td><td><a href='{rep.e(name)}'>open</a></td></tr>")
    out.append("</table></div></section>")
    out.append("<section><h2>Before / after</h2><div class='scroll'><table><tr><th>Measure</th><th>Before</th><th>After</th></tr>")
    for name in KEY_METRICS + ["amount_only", "date", "duplicates"]:
        b, a = _m(item["score_before"], name), _m(item["score_after"], name)
        if b is None:
            continue
        fmt = lambda m: (f"{m['correct']}/{m['labelled']}" if m["labelled"] and m["correct"] is not None else
                         f"{m['incorrect']}" if m["metric"] == "duplicates" else "unscored")
        out.append(f"<tr><td>{rep.e(name)}</td><td>{rep.e(fmt(b))}</td><td>{rep.e(fmt(a))}</td></tr>")
    for key in ("rows", "balance_mismatches", "swapped_flags", "failed_checks", "completeness", "mode"):
        out.append(f"<tr><td>{rep.e(key)}</td><td>{rep.e(item['diag_before'].get(key))}</td><td>{rep.e(item['diag_after'].get(key))}</td></tr>")
    out.append("</table></div></section></main>")
    return rep.page(f"Grid · {item['id']}", "".join(out))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--set", required=True, choices=["synthetic", "real", "uploads", "all"])
    parser.add_argument("--with-llm", action="store_true", help="also run A and B with the local model, then check them with the grid")
    parser.add_argument("--model", default="llama3.2:3b")
    parser.add_argument("--pin", default="a80c4f17acd5")
    parser.add_argument("--replay-from", help="a finished grid-<id> folder: re-check its recorded LLM answers, no model calls")
    parser.add_argument("--llm", choices=["ollama", "groq"], default="ollama",
                        help="groq is only allowed with --set real (user-authorised 2026-10-15)")
    args = parser.parse_args(argv)
    if args.llm == "groq" and (args.set != "real" or not args.with_llm):
        raise SystemExit("--llm groq is authorised only for --set real --with-llm")
    R.install_file_guard(confidential_read_authorised=args.set != "synthetic")
    verify_app_isolation(R.APP_DB_SENTINEL)
    live_before = R.live_db_state()
    entries = statements(args.set)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = R.EVAL_DIR / f"grid-{stamp}-{args.set}-{uuid.uuid4().hex[:6]}"
    out_dir.mkdir(parents=True, exist_ok=False)
    items: list[dict[str, Any]] = []
    if args.with_llm and args.llm == "groq":
        ctx = groq_context()
    else:
        ctx = R.build_context(args.model, args.pin, R.DEFAULT_ENDPOINT, markdown_only=False) if args.with_llm else None
    monitor = ctx.get("monitor") if ctx else None
    guard_ctx = ctx["socket_guard"] if ctx else SocketGuard()
    with guard_ctx as guard:
        if monitor is not None:
            monitor.start()
        for entry in entries:
            if monitor is not None and monitor.violation:
                print("STOPPED: a non-local connection was detected", flush=True)
                break
            item = run_one(entry, out_dir, ctx, Path(args.replay_from) if args.replay_from else None)
            items.append(item)
            slug = entry["id"].replace("/", "_").replace(".", "_")
            R.write_text(out_dir / slug / "index.html", render_item(item))
            llm = item.get("llm") or {}
            extra = " | " + " ".join(f"{k}: {v.get('corrections', '-')} corrected, used {(v.get('decision') or {}).get('used')}"
                                     for k, v in llm.items()) if llm else ""
            print(f"  {entry['id']:44} {item['verdict']:26} moved {item.get('grid', {}).get('amounts_moved', '-')}"
                  f" | used: {(item.get('decision') or {}).get('used', '-')}{extra}", flush=True)
        if monitor is not None:
            monitor.stop()
    meta = {"set": args.set, "created": stamp, "statements": len(entries), "socket_guard": guard.summary(),
            "model": (ctx or {}).get("model"), "network_monitor": monitor.summary() if monitor else None,
            "file_guard": R.FILE_GUARD.summary() if R.FILE_GUARD else None,
            "live_db_unchanged": R.live_db_state() == live_before, "app_db_sentinel_exists": R.APP_DB_SENTINEL.exists()}
    R.write_json(out_dir / "summary.json", {"meta": meta, "items": [{k: v for k, v in i.items() if k != "grid"} for i in items]})
    R.write_text(out_dir / "summary.html", render_summary(items, meta))
    print(f"summary: {out_dir / 'summary.html'}")
    print(f"verdicts: {dict(Counter(i['verdict'] for i in items))} | live DB unchanged: {meta['live_db_unchanged']} | "
          f"blocked connections: {len(guard.blocked)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
