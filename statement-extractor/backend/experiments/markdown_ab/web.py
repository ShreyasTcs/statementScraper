"""Local upload UI for the PoC.

    cd statement-extractor/backend
    .venv/Scripts/python -m experiments.markdown_ab.web            # then open http://127.0.0.1:8765

Upload a bank statement (PDF, PNG or JPG) and choose a local model. The statement is read once and run through
Pipeline A (direct lines + local LLM), Pipeline B (Markdown + the same local LLM) and the deterministic reference,
exactly as the command line does. The page shows progress and then opens the side-by-side comparison report.

* The server listens on 127.0.0.1 only. Uploads are processed one at a time, in the order they arrive.
* All of the command line's local-only checks run for every upload (loopback endpoint, pinned model digest, external
  calls off and no Groq client, in-process socket guard, file guard, network monitor).
* The uploaded file is stored only inside its own run folder (data/evaluations/<run-id>/input/). If it is byte-for-byte
  one of the synthetic statements in data/samples or data/corpus, that statement's ground truth is used for scoring.
"""
from __future__ import annotations

# Pins the application's settings before any application module is imported (main() installs the file guard).
from experiments.markdown_ab import run as R  # isort: skip

import argparse  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import queue  # noqa: E402
import re  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI, File, Form, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.file_service import FileValidationError, validate_upload  # noqa: E402
from experiments.markdown_ab import report as reporting  # noqa: E402
from experiments.markdown_ab.local_llm import DEFAULT_ENDPOINT, verify_app_isolation, verify_endpoint, verify_model  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
#: Candidate local models and the digests verified for them (Ollama's 12-digit IDs).
MODELS = [("llama3.2:3b", "a80c4f17acd5"), ("gemma3:4b", "a2af6cc3eb7f")]
EXTENSIONS = {"pdf": ".pdf", "png": ".png", "jpg": ".jpg", "jpeg": ".jpg"}
UI_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
RUN_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z-]{5,120}$")

STATE: dict[str, Any] = {"models": {}, "isolation": None, "endpoint": None, "truth_by_hash": {}}
JOBS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()
WORK: queue.Queue[str] = queue.Queue()

EXTRA_STYLE = """
form.upload { display:grid; gap:14px; }
fieldset { border:1px solid var(--line); border-radius:6px; padding:10px 14px; }
legend { font-weight:600; padding:0 4px; }
label.opt { display:block; padding:4px 0; }
input[type=file] { font-size:15px; padding:8px; border:1px dashed #9aa5b1; border-radius:6px; width:100%; background:#fbfcfd; }
button { justify-self:start; background:var(--accent); color:#fff; border:0; border-radius:6px; padding:9px 22px; font-size:15px;
  font-weight:600; cursor:pointer; }
button:hover { background:#174b7a; }
.stages li { margin:3px 0; }
.big { font-size:17px; }
nav a { color:#cfe3f7; margin-right:16px; }
"""


def ui_page(title: str, body: str, refresh: int | None = None) -> HTMLResponse:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    html_text = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                 f'<meta http-equiv="Content-Security-Policy" content="{UI_CSP}">{meta}'
                 f'<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="referrer" content="no-referrer">'
                 f'<title>{reporting.e(title)}</title><style>{reporting.STYLE}{EXTRA_STYLE}</style></head><body>'
                 "<header class='top'><h1>Bank statement extraction PoC · upload and compare</h1>"
                 "<p>A · Direct OCR/Text + Local LLM &nbsp;|&nbsp; B · Markdown + Local LLM &nbsp;|&nbsp; Reference · deterministic local engine</p>"
                 "<nav><a href='/'>Upload</a><a href='/runs'>All runs</a></nav></header>"
                 f"<main>{body}</main></body></html>")
    return HTMLResponse(html_text, headers={"Content-Security-Policy": UI_CSP, "Cache-Control": "no-store"})


# --------------------------------------------------------------------------
# Start-up checks
# --------------------------------------------------------------------------


def startup_checks(endpoint_url: str) -> None:
    STATE["isolation"] = verify_app_isolation(R.APP_DB_SENTINEL)
    STATE["endpoint"] = verify_endpoint(endpoint_url)
    for name, pin in MODELS:
        try:
            STATE["models"][name] = {"pin": pin, "identity": verify_model(STATE["endpoint"]["endpoint"], name, pin), "error": None}
        except Exception as exc:  # noqa: BLE001 - shown on the page, the model is then not offered
            STATE["models"][name] = {"pin": pin, "identity": None, "error": f"{type(exc).__name__}: {exc}"}
    STATE["groq"] = R.groq_status()
    for name in ("mocks", "corpus"):
        for path in R.statement_set(name):
            STATE["truth_by_hash"][hashlib.sha256(path.read_bytes()).hexdigest()] = path


# --------------------------------------------------------------------------
# The worker: one upload at a time
# --------------------------------------------------------------------------


def _stage(job: dict[str, Any], name: str) -> None:
    with LOCK:
        now = time.time()
        if job["stages"]:
            job["stages"][-1]["ended"] = now
        job["stages"].append({"name": name, "started": now, "ended": None})


def worker() -> None:
    while True:
        run_id = WORK.get()
        job = JOBS[run_id]
        with LOCK:
            job.update(state="running", started=time.time())
        try:
            ctx = R.build_context(job["model"], job["pin"], STATE["endpoint"]["endpoint"], job["markdown_only"],
                                  provider=job.get("provider", "ollama"))
            monitor = ctx.get("monitor")
            with ctx["socket_guard"]:
                if monitor is not None:
                    monitor.start()
                try:
                    data = R.run_statement(job["path"], ctx, label=job["label"], truth_source=job["truth"],
                                           run_id=run_id, on_stage=lambda name: _stage(job, name))
                finally:
                    if monitor is not None:
                        monitor.stop()
            status = data["run"]["status"]
            with LOCK:
                job.update(state="done" if status == "completed" else "failed", run_status=status,
                           error=(data["run"].get("error") or {}).get("message"))
        except BaseException as exc:  # noqa: BLE001 - the page must show what happened
            with LOCK:
                job.update(state="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            with LOCK:
                job["finished"] = time.time()
                if job["stages"] and job["stages"][-1]["ended"] is None:
                    job["stages"][-1]["ended"] = job["finished"]
            try:
                R.build_index()
            except Exception:  # noqa: BLE001 - the index is a convenience
                pass
            WORK.task_done()


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def _guard_panel() -> str:
    iso = STATE["isolation"] or {}
    endpoint = STATE["endpoint"] or {}
    rows = [
        ("Ollama endpoint", reporting.yes(endpoint.get("loopback_only"), "loopback only", "NOT loopback") + " "
         + reporting.e(endpoint.get("endpoint"))),
        ("External LLM calls", reporting.yes(iso.get("allow_external_llm_calls") is False, "disabled", "ENABLED")),
        ("Groq client", reporting.yes(not iso.get("groq_client_constructible", True), "cannot be built", "CAN be built")),
        ("Application database", reporting.yes(iso.get("database_url_is_sentinel"), "not used (sentinel path)", "NOT isolated")),
    ]
    for name, info in STATE["models"].items():
        ident = info["identity"]
        rows.append((f"Model {name}", reporting.yes(ident is not None, f"digest {ident['digest'][:12]} verified" if ident else "", "unavailable")
                     + ("" if ident else " " + reporting.e(info["error"]))))
    groq = STATE.get("groq") or {}
    rows.append(("Groq (external)", reporting.badge("available, needs consent per upload", "warn") if groq.get("ready")
                 else reporting.badge("not configured", "info")))
    return reporting.kv([(k, v) for k, v in rows])


def _job_rows() -> str:
    with LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)
    if not jobs:
        return "<p class='muted'>No uploads in this session yet.</p>"
    out = ["<table><tr><th>Uploaded</th><th>Statement</th><th>Mode</th><th>Model</th><th>State</th><th></th></tr>"]
    for j in jobs:
        state = {"queued": reporting.badge("queued", "info"), "running": reporting.badge("running", "warn"),
                 "done": reporting.badge("done", "ok"), "failed": reporting.badge("failed", "bad")}[j["state"]]
        out.append(f"<tr><td>{reporting.e(time.strftime('%H:%M:%S', time.localtime(j['created'])))}</td><td>{reporting.e(j['label'])}</td>"
                   f"<td>{'Markdown only' if j['markdown_only'] else 'Full comparison'}</td><td>{reporting.e(j['model'] or '–')}</td>"
                   f"<td>{state}</td><td><a href='/runs/{reporting.e(j['run_id'])}'>open</a></td></tr>")
    out.append("</table>")
    return "".join(out)


@app.get("/", response_class=HTMLResponse)
def home(error: str | None = None) -> HTMLResponse:
    available = [(n, i) for n, i in STATE["models"].items() if i["identity"] is not None]
    model_opts = "".join(
        f"<label class='opt'><input type='radio' name='model' value='{reporting.e(n)}'{' checked' if k == 0 else ''}> "
        f"{reporting.e(n)} <span class='muted'>({reporting.e(i['identity']['parameter_size'])}, {reporting.e(i['identity']['quantization'])}, "
        f"digest {reporting.e(i['identity']['digest'][:12])})</span></label>" for k, (n, i) in enumerate(available))
    groq = STATE.get("groq") or {}
    if groq.get("ready"):
        model_opts += (f"<label class='opt'><input type='radio' name='model' value='groq'> Groq · {reporting.e(groq.get('model'))} "
                       "<span class='badge warn'>external service</span> <span class='muted'>(fast; the statement text is "
                       "sent to Groq)</span></label>"
                       "<label class='opt'><input type='checkbox' name='groq_consent' value='yes'> I agree that, when Groq is "
                       "chosen, this statement's text is sent to Groq's servers.</label>")
    if not available and not groq.get("ready"):
        model_opts = "<p>" + reporting.badge("no verified local model", "bad") + " only the Markdown-only mode can run.</p>"
    body = []
    if error:
        body.append(f"<section>{reporting.badge('upload refused', 'bad')} {reporting.e(error)}</section>")
    body.append(
        "<section><h2>Upload a statement</h2>"
        "<form class='upload' method='post' action='/runs' enctype='multipart/form-data'>"
        "<input type='file' name='file' accept='.pdf,.png,.jpg,.jpeg' required>"
        f"<fieldset><legend>Model (used by A and B)</legend>{model_opts}</fieldset>"
        "<fieldset><legend>What to run</legend>"
        "<label class='opt'><input type='radio' name='mode' value='full' checked> Full comparison: A, B and the reference "
        "<span class='muted'>(on this 2-core CPU about 10–15 minutes per pipeline for a one-page statement, measured)</span></label>"
        "<label class='opt'><input type='radio' name='mode' value='markdown'> Markdown only: read the statement and show the "
        "Markdown and the reference engine's result, no model calls <span class='muted'>(seconds)</span></label></fieldset>"
        "<button type='submit'>Upload and run</button></form>"
        f"<p class='muted'>With a local model everything stays on this computer; with Groq the statement text is sent to "
        f"Groq. The file is kept in its run folder under data/evaluations/. "
        f"Accepted: PDF, PNG, JPG up to {settings.max_file_size_bytes // (1024 * 1024)} MB and {settings.max_pages} pages. "
        "If the file is identical to one of the synthetic statements in data/samples or data/corpus, its ground truth is "
        "used for scoring; otherwise the report shows the three results without a scorecard.</p></section>")
    body.append(f"<section><h2>Local-only checks (at start-up; repeated for every upload)</h2>{_guard_panel()}</section>")
    body.append(f"<section><h2>This session</h2>{_job_rows()}</section>")
    return ui_page("PoC · upload", "".join(body))


@app.post("/runs")
async def create_run(file: UploadFile = File(...), model: str = Form(""), mode: str = Form("full"),
                     groq_consent: str = Form("")) -> Response:
    content = await file.read(settings.max_file_size_bytes + 1)
    if len(content) > settings.max_file_size_bytes:
        return home(error=f"The file is larger than {settings.max_file_size_bytes // (1024 * 1024)} MB.")
    try:
        file_type, _mime = validate_upload(file.filename or "statement.pdf", content)
    except FileValidationError as exc:
        return home(error=exc.message)
    markdown_only = mode == "markdown"
    pin = None
    provider = "ollama"
    if not markdown_only and model == "groq":
        if not (STATE.get("groq") or {}).get("ready"):
            return home(error="Groq is not configured.")
        if groq_consent != "yes":
            return home(error="To use Groq, tick the box agreeing that the statement text is sent to Groq.")
        provider = "groq"
    elif not markdown_only:
        info = STATE["models"].get(model)
        if info is None or info["identity"] is None:
            return home(error="Choose a verified local model, or the Markdown-only mode.")
        pin = info["pin"]
    digest = hashlib.sha256(content).hexdigest()
    truth = STATE["truth_by_hash"].get(digest)
    label = f"upload {digest[:12]}" + (f" (identical to {R.statement_id(truth)})" if truth else "")
    run_id = R.new_run_id("upload-" + digest[:8])
    folder = R.EVAL_DIR / run_id / "input"
    folder.mkdir(parents=True, exist_ok=False)
    path = folder / ("uploaded" + EXTENSIONS.get(file_type, ".pdf"))
    path.write_bytes(content)
    with LOCK:
        JOBS[run_id] = {"run_id": run_id, "label": label, "path": path, "truth": truth, "provider": provider,
                        "model": None if markdown_only else model,
                        "pin": pin, "markdown_only": markdown_only, "state": "queued", "created": time.time(),
                        "started": None, "finished": None, "stages": [], "error": None}
    WORK.put(run_id)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60} min {seconds % 60:02d} s" if seconds >= 60 else f"{seconds} s"


@app.get("/runs", response_class=HTMLResponse)
def all_runs() -> HTMLResponse:
    rows = []
    for folder in sorted(R.EVAL_DIR.iterdir(), reverse=True) if R.EVAL_DIR.exists() else []:
        if not folder.is_dir() or folder.name.startswith(("_", "batch-")):
            continue
        info = folder / "run.json"
        data = json.loads(info.read_text(encoding="utf-8")) if info.exists() else None
        if data is None:
            state = reporting.badge("running" if folder.name in JOBS and JOBS[folder.name]["state"] in ("queued", "running") else "incomplete", "warn")
            rows.append(f"<tr><td>{reporting.e(folder.name)}</td><td>–</td><td>–</td><td>{state}</td><td><a href='/runs/{reporting.e(folder.name)}'>open</a></td></tr>")
            continue
        run = data.get("run") or {}
        rows.append(f"<tr><td>{reporting.e(folder.name)}</td><td>{reporting.e((data.get('statement') or {}).get('id'))}</td>"
                    f"<td>{reporting.e(run.get('mode'))} · {reporting.e((data.get('model') or {}).get('name') or 'no model')}</td>"
                    f"<td>{reporting.status_badge(run.get('status'))}</td><td><a href='/runs/{reporting.e(folder.name)}/report.html'>report</a></td></tr>")
    body = ("<section><h2>All run folders</h2><div class='scroll'><table><tr><th>Run</th><th>Statement</th><th>Mode · model</th>"
            "<th>Status</th><th></th></tr>" + "".join(rows) + "</table></div></section>")
    return ui_page("PoC · all runs", body)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_status(run_id: str) -> Response:
    if not RUN_ID.match(run_id) or not (R.EVAL_DIR / run_id).is_dir():
        return ui_page("Not found", "<section><h2>No such run</h2></section>")
    with LOCK:
        job = dict(JOBS.get(run_id) or {})
        stages = [dict(s) for s in job.get("stages", [])]
    report = R.EVAL_DIR / run_id / "report.html"
    if job.get("state") in ("done", "failed") or (not job and report.exists()):
        if report.exists():
            return RedirectResponse(f"/runs/{run_id}/report.html", status_code=303)
        return ui_page("Run failed", f"<section><h2>The run did not produce a report</h2><p>{reporting.badge('failed', 'bad')} "
                       f"{reporting.e(job.get('error'))}</p><p><a href='/'>Back to upload</a></p></section>")
    if not job:
        return ui_page("Incomplete run", "<section><h2>This run did not finish</h2><p>The server stopped before the run ended; "
                       "the folder holds whatever was written until then.</p></section>")
    now = time.time()
    if job["state"] == "queued":
        with LOCK:
            ahead = sum(1 for j in JOBS.values() if j["state"] in ("queued", "running") and j["created"] < job["created"])
        detail = f"<p class='big'>{reporting.badge('queued', 'info')} {ahead} upload(s) ahead of this one.</p>"
    else:
        items = []
        for s in stages:
            took = _fmt((s["ended"] or now) - s["started"])
            mark = "✓" if s["ended"] else "▶"
            items.append(f"<li>{mark} {reporting.e(s['name'])} <span class='muted'>{took}{'' if s['ended'] else ' so far'}</span></li>")
        detail = (f"<p class='big'>{reporting.badge('running', 'warn')} {_fmt(now - job['started'])} elapsed</p>"
                  f"<ul class='stages'>{''.join(items)}</ul><p class='muted'>Each language-model pass writes a few thousand "
                  "tokens at a few tokens per second on this CPU, so a pass takes several minutes. This page refreshes every "
                  "5 seconds and opens the report when the run is finished.</p>")
    body = (f"<section><h2>{reporting.e(job['label'])}</h2>"
            + reporting.kv([("Run", reporting.e(run_id)), ("Mode", "Markdown only" if job["markdown_only"] else "Full comparison"),
                            ("Model", reporting.e(job["model"] or "none")), ("Pin", reporting.e(job["pin"] or "–"))])
            + detail + "</section>")
    return ui_page("PoC · running", body, refresh=5)


TEXT_TYPES = {".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8",
              ".html": "text/html; charset=utf-8"}


@app.get("/runs/{run_id}/{rel:path}")
def run_file(run_id: str, rel: str) -> Response:
    if not RUN_ID.match(run_id):
        return Response("not found", status_code=404)
    base = (R.EVAL_DIR / run_id).resolve()
    target = (base / rel).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        return Response("not found", status_code=404)
    media = TEXT_TYPES.get(target.suffix.lower())
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    if target.name == "report.html" and target.parent == base:
        # The stored report stays untouched; the served copy gets a way back to the upload page.
        bar = ("<div style='background:#0f1820;padding:8px 28px;font:14px system-ui,sans-serif'>"
               "<a style='color:#cfe3f7;margin-right:18px' href='/'>&larr; Upload another statement</a>"
               "<a style='color:#cfe3f7' href='/runs'>All runs</a></div>")
        return HTMLResponse(target.read_text(encoding="utf-8").replace("<body>", "<body>" + bar, 1), headers=headers)
    return FileResponse(target, media_type=media, headers=headers)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port on 127.0.0.1 (default 8765)")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="Ollama endpoint (loopback only)")
    args = parser.parse_args(argv)
    R.install_file_guard()
    startup_checks(args.endpoint)
    for name, info in STATE["models"].items():
        print(f"model {name}: " + (f"digest {info['identity']['digest'][:12]} verified" if info["identity"] else f"unavailable ({info['error']})"))
    threading.Thread(target=worker, daemon=True, name="poc-worker").start()
    print(f"PoC upload UI: http://{HOST}:{args.port}  (Ctrl+C to stop)", flush=True)
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
