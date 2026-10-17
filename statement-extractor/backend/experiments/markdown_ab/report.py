"""Self-contained, English-only HTML for one run and for a batch.

* No scripts, no external resources: a Content Security Policy forbids every network load; collapsible sections use
  the native <details> element.
* Every value that comes from a statement or a model (text, Markdown, descriptions, raw output) is HTML-escaped.
* A failed pipeline is shown as failed; nothing is filled in for it.
"""
from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'"
ORDER = ("A", "B", "reference")
SHORT = {"A": "A · Direct OCR/Text + Local LLM", "B": "B · Markdown + Local LLM", "reference": "Reference · Deterministic local engine"}

STYLE = """
:root { --bg:#f7f7f5; --panel:#ffffff; --ink:#1d2329; --muted:#56616d; --line:#d9dde1; --accent:#1f5f99;
  --ok:#17663a; --okbg:#e3f3e8; --bad:#a3231a; --badbg:#fbe7e4; --warn:#7a5200; --warnbg:#fdf1d7;
  --info:#24527a; --infobg:#e5eef7; --a:#2f6db3; --b:#8a4fb5; --r:#4f6b55; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.5 "Segoe UI", system-ui, -apple-system, sans-serif; }
header.top { background:#16202a; color:#f2f4f6; padding:20px 28px; }
header.top h1 { margin:0 0 4px; font-size:22px; font-weight:600; }
header.top p { margin:2px 0; color:#c4ccd4; }
main { max-width:1500px; margin:0 auto; padding:18px 28px 60px; }
section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px 20px; margin:18px 0; }
h2 { font-size:18px; margin:0 0 10px; }
h3 { font-size:15px; margin:16px 0 8px; }
table { border-collapse:collapse; width:100%; margin:6px 0 10px; font-size:13.5px; }
th, td { border-bottom:1px solid var(--line); padding:5px 8px; text-align:left; vertical-align:top; }
th { background:#eef1f4; font-weight:600; position:sticky; top:0; }
td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
.muted { color:var(--muted); }
.badge { display:inline-block; padding:1px 8px; border-radius:10px; font-size:12px; font-weight:600; border:1px solid transparent; }
.ok { background:var(--okbg); color:var(--ok); border-color:#b9dfc6; }
.bad { background:var(--badbg); color:var(--bad); border-color:#efc1ba; }
.warn { background:var(--warnbg); color:var(--warn); border-color:#ecd39b; }
.info { background:var(--infobg); color:var(--info); border-color:#bcd2e8; }
.grid3 { display:grid; grid-template-columns:repeat(auto-fit, minmax(330px, 1fr)); gap:14px; }
.card { border:1px solid var(--line); border-radius:6px; padding:10px 12px; background:#fcfcfb; overflow-x:auto; }
.card h3 { margin-top:0; }
.colA { border-top:4px solid var(--a); } .colB { border-top:4px solid var(--b); } .colR { border-top:4px solid var(--r); }
pre { background:#f2f4f6; border:1px solid var(--line); border-radius:6px; padding:10px; overflow:auto; max-height:520px;
  font:12.5px/1.45 Consolas, "Cascadia Mono", monospace; white-space:pre; }
details { margin:8px 0; } summary { cursor:pointer; font-weight:600; padding:4px 0; }
.kv { display:grid; grid-template-columns:max-content 1fr; gap:3px 16px; font-size:13.5px; }
.kv div:nth-child(odd) { color:var(--muted); }
.scroll { overflow-x:auto; }
.diff { background:#fff4cf; }
.legend span { margin-right:12px; }
footer { color:var(--muted); font-size:12.5px; margin-top:30px; }
@media (max-width: 700px) { main { padding:10px; } section { padding:12px; } }
"""


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def badge(text: str, kind: str) -> str:
    return f'<span class="badge {kind}">{e(text)}</span>'


def status_badge(status: str | None) -> str:
    if status == "completed":
        return badge("completed", "ok")
    if status in (None, ""):
        return badge("not run", "info")
    return badge(status, "bad")


def yes(ok: Any, good: str = "yes", bad: str = "no") -> str:
    return badge(good, "ok") if ok else badge(bad, "bad")


def _ms(value: Any) -> str:
    return "–" if value is None else f"{value / 1000:.1f} s"


def _pct(value: Any) -> str:
    return "–" if value is None else f"{value * 100:.1f}%"


def page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="referrer" content="no-referrer"><title>{e(title)}</title><style>{STYLE}</style></head>'
            f'<body>{body}</body></html>')


def kv(pairs: list[tuple[str, str]]) -> str:
    return '<div class="kv">' + "".join(f"<div>{e(k)}</div><div>{v}</div>" for k, v in pairs) + "</div>"


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------


def _result(data: dict, key: str) -> dict | None:
    return (data.get("pipelines", {}).get(key) or {}).get("result")


def _summary_section(data: dict) -> str:
    run, st, model = data["run"], data.get("statement") or {}, data.get("model") or {}
    ch = st.get("characteristics") or {}
    lo = data.get("local_only") or {}
    iso = lo.get("app_isolation") or {}
    net = lo.get("network_monitor") or {}
    sock = lo.get("socket_guard") or {}
    fguard = lo.get("file_guard") or {}
    endpoint = lo.get("endpoint") or {}
    per_page = ", ".join(f"p{p['page']}: {p['source']} ({p['lines']} lines)" for p in ch.get("per_page", []))
    out = ['<section id="summary"><h2>A. Run summary</h2><div class="grid3"><div class="card">', "<h3>Statement</h3>", kv([
        ("Statement", e(st.get("id"))), ("SHA-256", e((st.get("sha256") or "")[:16]) + "…"),
        ("Pages", e(st.get("pages"))), ("File type", e(ch.get("file_type"))),
        ("Text / OCR", e(ch.get("method"))), ("Per page", e(per_page) or "–"),
        ("Pages failed", e(ch.get("pages_failed") or "none")), ("Read once in", _ms(st.get("read_ms"))),
        ("Ground truth", e("yes: %s rows, %s with balance, %s with description" % (
            (data.get("ground_truth") or {}).get("rows"), (data.get("ground_truth") or {}).get("rows_with_balance"),
            (data.get("ground_truth") or {}).get("rows_with_description"))) if (data.get("ground_truth") or {}).get("available") else "none"),
    ]), '</div><div class="card"><h3>Run and model</h3>', kv([
        ("Run ID", e(run.get("run_id"))), ("Created (UTC)", e(run.get("created_at"))),
        ("Mode", e(run.get("mode"))), ("Execution", e(run.get("execution"))),
        ("Run status", status_badge(run.get("status"))),
        ("Model", e(model.get("name")) or "–"), ("Digest", e(model.get("digest")) or "–"),
        ("Model details", e(" · ".join(str(x) for x in (model.get("family"), model.get("parameter_size"), model.get("quantization")) if x)) or "–"),
        ("Ollama", e(model.get("ollama_version")) or "–"), ("Endpoint", e(endpoint.get("endpoint")) or "–"),
        ("Source tree", e((run.get("source_tree") or "")[:16]) + "…"), ("Total time", _ms(run.get("total_ms"))),
    ]), '</div><div class="card"><h3>' + ("External model: Groq" if model.get("provider") == "groq" else "Local-only verification") + '</h3>',
        kv([
        ("Model service", badge("external: Groq", "warn") + " " + e((lo.get("external_llm") or {}).get("endpoint"))),
        ("Statement text sent to Groq", badge("yes, with the uploader's consent", "warn")),
        ("Other outside connections", yes(not sock.get("blocked"), "none (in-process socket guard)", f"{len(sock.get('blocked') or [])} blocked")),
        ("Connections to Groq", e(sock.get("allowed_authorised_remote_connections", 0))),
        ("App database setting", yes(iso.get("database_url_is_sentinel"), "unused sentinel path", "NOT isolated")),
        ("File guard (audit hook)", yes(not fguard.get("blocked"), "no protected file opened", f"{len(fguard.get('blocked') or [])} blocked")),
        ("Live DB files unchanged", yes(lo.get("live_db_unchanged_since_start"), "yes", "changed (not opened by this process)")),
    ]) if model.get("provider") == "groq" else kv([
        ("Overall", yes(lo.get("ok"), "verified", "NOT verified")),
        ("Endpoint is loopback", yes(endpoint.get("loopback_only")) + " " + e(", ".join(endpoint.get("resolved", [])))),
        ("Digest matches pin", yes(model.get("digest_matches_pin")) + " " + e(model.get("pin"))),
        ("External LLM calls", yes(iso.get("allow_external_llm_calls") is False, "disabled", "ENABLED")),
        ("Groq key in process", yes(iso.get("groq_api_key_empty"), "empty", "SET")),
        ("Groq client can be built", yes(not iso.get("groq_client_constructible", True), "no (" + e(iso.get("groq_client_block_code")) + ")", "YES")),
        ("App database setting", yes(iso.get("database_url_is_sentinel"), "unused sentinel path", "NOT isolated")),
        ("Sentinel DB created", yes(not lo.get("app_db_sentinel_exists_after"), "no", "YES")),
        ("Socket guard (in process)", yes(not sock.get("blocked"), f"{sock.get('allowed_loopback_connections', 0)} loopback connections, 0 blocked",
                                          f"{len(sock.get('blocked') or [])} blocked")),
        ("File guard (audit hook)", yes(not fguard.get("blocked"), "no protected file opened", f"{len(fguard.get('blocked') or [])} blocked")),
        ("Network monitor", (yes(not lo.get("violations_during_this_run"), f"{net.get('samples', 0)} samples, no external connection",
                                 "EXTERNAL CONNECTION") if net.get("samples") is not None else badge("not used", "info"))),
        ("Ollama tray background", e(f"{len(net.get('background_tray_connections') or [])} connection(s) recorded (not attributed to the run)")
         if net.get("samples") is not None else "–"),
        ("Live DB files unchanged", yes(lo.get("live_db_unchanged_since_start"), "yes",
                                        "changed (not opened by this process; the running app may write)")),
    ]), '<p class="muted">Network sampling can miss connections shorter than its interval; the in-process socket guard '
        'refuses non-loopback connections from this process outright.</p></div></div>']

    out.append("<h3>Pipelines</h3><div class='scroll'><table><tr><th>Pipeline</th><th>Status</th><th>Error</th>"
               "<th class='num'>Total</th><th class='num'>LLM</th><th class='num'>Markdown</th><th class='num'>Calls</th>"
               "<th class='num'>Schema-valid</th><th class='num'>Chunks ok</th><th class='num'>Rows</th><th>Completeness</th></tr>")
    for key in ORDER:
        item = data.get("pipelines", {}).get(key)
        if item is None:
            out.append(f"<tr><td>{e(SHORT[key])}</td><td>{badge('not run', 'info')}</td><td colspan='9'></td></tr>")
            continue
        result = item.get("result") or {}
        llm = item.get("llm") or {}
        rows = sum(len(a.get("rows", [])) for a in result.get("accounts", []))
        error = result.get("error") or {}
        out.append(
            f"<tr><td>{e(SHORT[key])}</td><td>{status_badge(result.get('status') or item.get('skipped'))}</td>"
            f"<td>{e(error.get('code'))} {e(error.get('message'))}</td><td class='num'>{_ms(item.get('total_ms'))}</td>"
            f"<td class='num'>{_ms(llm.get('llm_ms')) if llm else '–'}</td><td class='num'>{_ms(item.get('conversion_ms')) if key == 'B' else '–'}</td>"
            f"<td class='num'>{e(llm.get('calls', '–'))}</td><td class='num'>{e(llm.get('schema_valid_calls', '–'))}</td>"
            f"<td class='num'>{e(str(llm.get('chunks_ok')) + '/' + str(llm.get('chunks')) if llm else '–')}</td>"
            f"<td class='num'>{rows if result.get('status') == 'completed' else '–'}</td>"
            f"<td>{e((result.get('completeness') or {}).get('status'))}</td></tr>")
    out.append("</table></div>")

    warnings = (data.get("markdown") or {}).get("warnings") or []
    md = data.get("markdown") or {}
    out.append("<h3>Conversion and extraction warnings</h3>")
    if md:
        out.append("<p>Markdown conversion: " + (badge("no content lost", "ok") if md.get("faithful") else badge("CONTENT LOST", "bad"))
                   + f" · {e(md.get('stats', {}).get('tokens_checked'))} printed tokens checked, "
                   f"{e(md.get('stats', {}).get('tokens_lost'))} missing.</p>")
    if warnings:
        out.append("<ul>" + "".join(f"<li>{badge(w['severity'], 'bad' if w['severity'] == 'error' else 'warn')} "
                                    f"<code>{e(w['code'])}</code> {e(w['message'])}</li>" for w in warnings) + "</ul>")
    else:
        out.append("<p class='muted'>No Markdown conversion warnings.</p>")
    for key in ("A", "B"):
        llm = ((data.get("pipelines", {}).get(key) or {}).get("llm") or {})
        if llm.get("chunk_errors") or llm.get("fatal_error"):
            out.append(f"<p>{e(SHORT[key])}: model errors {e(', '.join(llm.get('chunk_errors') or []))} {e(llm.get('fatal_error') or '')}</p>")
    if run.get("error"):
        out.append(f"<p>{badge('run error', 'bad')} {e(run['error'].get('type'))}: {e(run['error'].get('message'))}</p>"
                   f"<details><summary>Traceback</summary><pre>{e(run['error'].get('traceback'))}</pre></details>")
    out.append("</section>")
    return "".join(out)


def _read(run_dir: Path, rel: str) -> str:
    path = run_dir / rel
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _intermediate_section(data: dict, run_dir: Path) -> str:
    import json
    provenance_path = run_dir / "input" / "markdown_provenance.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8")) if provenance_path.exists() else []
    md = data.get("markdown") or {}
    stats = md.get("stats") or {}
    cols = ", ".join(f"{c['label']}{'' if c['printed'] else ' (detector name)'}" for c in md.get("columns", []))
    out = ['<section id="inputs"><h2>B. Intermediate representations</h2>',
           f"<p>Both pipelines start from the same reading result. A sends the direct layout lines; B sends the Markdown "
           f"lines below. {e(stats.get('direct_lines'))} direct lines ({e(stats.get('direct_chars'))} characters) vs "
           f"{e(stats.get('markdown_lines'))} Markdown lines ({e(stats.get('markdown_chars'))} characters); "
           f"{e(stats.get('table_rows_emitted'))} table rows, {e(stats.get('verbatim_text_lines'))} verbatim text lines, "
           f"{e(stats.get('other_text_lines'))} lines under Other text. Markdown columns: {e(cols) or 'none'}.</p>",
           "<details open><summary>Pipeline A: direct extracted layout lines (input/direct_lines.txt)</summary>"
           f"<pre>{e(_read(run_dir, 'input/direct_lines.txt'))}</pre></details>",
           "<details><summary>Pipeline B: generated Markdown (input/markdown.md)</summary>"
           f"<pre>{e(_read(run_dir, 'input/markdown.md'))}</pre></details>",
           "<details><summary>Pipeline B: numbered source lines sent to the model (input/markdown_lines.txt)</summary>"
           f"<pre>{e(_read(run_dir, 'input/markdown_lines.txt'))}</pre></details>",
           "<details><summary>Pipeline B: provenance of every Markdown line</summary><div class='scroll'><table>"
           "<tr><th>Line</th><th>Kind</th><th class='num'>Page</th><th class='num'>Table row</th><th>Direct line(s)</th></tr>"]
    for p in provenance:
        out.append(f"<tr><td>{e(p['id'])}</td><td>{e(p['kind'])}</td><td class='num'>{e(p['page'])}</td>"
                   f"<td class='num'>{e(p.get('table_row') or '')}</td><td>{e(', '.join(p.get('direct_line_ids') or []))}</td></tr>")
    out.append("</table></div></details></section>")
    return "".join(out)


FIELD_ORDER = ["bank_name", "account_holder", "account_number", "iban", "bic", "routing", "branch", "customer_id",
               "account_type", "statement_number", "statement_date", "period_from", "period_to", "currency",
               "opening_balance", "closing_balance", "total_debits", "total_credits"]


def _header(result: dict | None, name: str) -> Any:
    if not result or result.get("status") != "completed" or not result.get("accounts"):
        return None
    value = (result["accounts"][0].get("headers") or {}).get(name)
    return value.get("normalized") if value else None


def _comparison_section(data: dict) -> str:
    truth = data.get("ground_truth") or {}
    gt_fields = set(truth.get("header_fields") or [])
    results = {k: _result(data, k) for k in ORDER}
    out = ['<section id="results"><h2>C. Three-column result comparison</h2>',
           "<p class='muted'>Values are the normalised values each approach produced for its first account. "
           "Ground-truth assertions are listed separately in the scorecard.</p>",
           "<div class='scroll'><table><tr><th>Field</th>" + "".join(f"<th>{e(SHORT[k])}</th>" for k in ORDER) + "</tr>"]
    for name in FIELD_ORDER:
        values = [_header(results[k], name) for k in ORDER]
        if all(v is None for v in values) and name not in gt_fields:
            continue
        differ = len({str(v) for v in values if v is not None}) > 1
        cells = "".join(f"<td class='{'diff' if differ else ''}'>{e(v) if v is not None else '<span class=muted>–</span>'}</td>" for v in values)
        out.append(f"<tr><td>{e(name)}{' <span class=muted>(labelled)</span>' if name in gt_fields else ''}</td>{cells}</tr>")
    for label, key in (("Opening balance (validated)", "opening_balance"), ("Closing balance (validated)", "closing_balance"),
                       ("Sum of debits", "total_debits"), ("Sum of credits", "total_credits"),
                       ("Printed debits total", "printed_debits"), ("Printed credits total", "printed_credits"),
                       ("Reconciliation mode", "mode"), ("Row order", "order")):
        values = []
        for k in ORDER:
            r = results[k]
            values.append(((r.get("accounts") or [{}])[0].get("totals") or {}).get(key) if r and r.get("status") == "completed" else None)
        differ = len({str(v) for v in values if v is not None}) > 1
        out.append(f"<tr><td>{e(label)}</td>" + "".join(f"<td class='{'diff' if differ else ''}'>{e(v) if v is not None else '–'}</td>" for v in values) + "</tr>")
    rows = []
    for k in ORDER:
        r = results[k]
        if r and r.get("status") == "completed":
            rows.append(f"{len(r.get('accounts', []))} account(s), {sum(len(a.get('rows', [])) for a in r.get('accounts', []))} rows")
        else:
            rows.append("failed: " + ((r or {}).get("error") or {}).get("code", "not run") if r else "not run")
    out.append("<tr><td>Accounts / transactions</td>" + "".join(f"<td>{e(v)}</td>" for v in rows) + "</tr>")
    comp = []
    for k in ORDER:
        r = results[k]
        c = (r or {}).get("completeness") or {}
        reasons = ", ".join(x.get("code", "") for x in c.get("reasons", []) if isinstance(x, dict))
        comp.append(f"{c.get('status', '–')}" + (f" ({reasons})" if reasons else ""))
    out.append("<tr><td>Completeness verdict</td>" + "".join(f"<td>{e(v)}</td>" for v in comp) + "</tr>")
    checks = []
    for k in ORDER:
        r = results[k]
        if not r or r.get("status") != "completed":
            checks.append("–")
            continue
        items = [c for a in r.get("accounts", []) for c in a.get("checks", []) if c.get("status") in ("fail", "warn")]
        checks.append("; ".join(f"{c.get('status')}: {c.get('label')}" for c in items) or "no failed or warning checks")
    out.append("<tr><td>Validation warnings</td>" + "".join(f"<td>{e(v)}</td>" for v in checks) + "</tr></table></div>")

    out.append("<h3>Transactions as extracted</h3><div class='grid3'>")
    for k, css in zip(ORDER, ("colA", "colB", "colR")):
        r = results[k]
        out.append(f"<div class='card {css}'><h3>{e(SHORT[k])}</h3>")
        if not r or r.get("status") != "completed":
            out.append(f"<p>{status_badge((r or {}).get('status'))} {e(((r or {}).get('error') or {}).get('message'))}</p></div>")
            continue
        out.append("<details open><summary>%d rows</summary><table><tr><th>Date</th><th>Description</th>"
                   "<th class='num'>Debit</th><th class='num'>Credit</th><th class='num'>Amount</th><th class='num'>Balance</th>"
                   "<th class='num'>Pg</th></tr>" % sum(len(a.get("rows", [])) for a in r.get("accounts", [])))
        for a in r.get("accounts", []):
            for row in a.get("rows", []):
                out.append(f"<tr><td>{e(row.get('date'))}</td><td>{e(row.get('description'))}</td>"
                           f"<td class='num'>{e(row.get('debit'))}</td><td class='num'>{e(row.get('credit'))}</td>"
                           f"<td class='num'>{e(row.get('amount'))}</td><td class='num'>{e(row.get('balance'))}</td>"
                           f"<td class='num'>{e(row.get('page'))}</td></tr>")
        out.append("</table></details></div>")
    out.append("</div></section>")
    return "".join(out)


def _discrepancy_section(data: dict) -> str:
    d = data.get("discrepancies")
    out = ['<section id="discrepancies"><h2>D. Transaction discrepancy explorer</h2>']
    if not d:
        return "".join(out) + "<p class='muted'>No completed pipelines to compare.</p></section>"
    names = d.get("pipelines", [])
    out.append(f"<p class='muted'>{e(d.get('rule'))} ✓ marks a row that matches a ground-truth row (strict rule).</p>")
    out.append("<p>" + " ".join(badge(f"{k}: {v}", "ok" if k == "matched" else "warn") for k, v in sorted(d.get("summary", {}).items())) + "</p>")
    groups = d.get("groups", [])

    def table(items: list[dict], title: str, open_: bool) -> str:
        if not items:
            return ""
        head = "".join(f"<th>{e(SHORT.get(n, n))}</th>" for n in names)
        body = []
        for g in items:
            cells = []
            for n in names:
                rows = g["rows"].get(n, [])
                if not rows:
                    cells.append("<td class='muted'>–</td>")
                    continue
                parts = [f"{'✓ ' if r.get('in_ground_truth') else ''}p{e(r.get('page'))} · {e(r.get('description'))}"
                         + (f" · bal {e(r.get('balance'))}" if r.get("balance") else "")
                         + (f" <span class=muted>[{e(', '.join(r.get('lines') or []))}]</span>" if r.get("lines") else "") for r in rows]
                cells.append("<td>" + "<br>".join(parts) + "</td>")
            diffs = "; ".join(f"{x['field']}: " + " / ".join(f"{SHORT.get(k, k)[:1]}={v}" for k, v in x["values"].items()) for x in g.get("differences", []))
            body.append(f"<tr><td>{e(g['date'])}</td><td>{e(g['direction'])}</td><td class='num'>{e(g['amount'])}</td>"
                        f"<td>{e(g['status'])}</td>{''.join(cells)}<td class='{'diff' if diffs else ''}'>{e(diffs)}</td></tr>")
        return (f"<details{' open' if open_ else ''}><summary>{e(title)} ({len(items)})</summary><div class='scroll'><table>"
                f"<tr><th>Date</th><th>Dir.</th><th class='num'>Amount</th><th>Status</th>{head}<th>Differing fields</th></tr>"
                + "".join(body) + "</table></div></details>")

    unmatched = [g for g in groups if g["status"] != "matched"]
    differing = [g for g in groups if g["status"] == "matched" and g.get("differences")]
    clean = [g for g in groups if g["status"] == "matched" and not g.get("differences")]
    out.append(table(unmatched, "Not found by every approach, or ambiguous", True))
    out.append(table(differing, "Matched by every approach, with differing fields", True))
    out.append(table(clean, "Matched by every approach, no differences", False))
    incomplete = d.get("incomplete", {})
    if any(incomplete.values()):
        out.append("<h3>Rows that cannot be aligned (missing date, direction or amount)</h3>")
        for n, rows in incomplete.items():
            if rows:
                out.append(f"<details open><summary>{e(SHORT.get(n, n))}: {len(rows)}</summary><table><tr><th>Problem</th><th>Page</th>"
                           "<th>Date</th><th>Debit</th><th>Credit</th><th>Amount</th><th>Description</th></tr>"
                           + "".join(f"<tr><td>{e(r['problem'])}</td><td>{e(r['page'])}</td><td>{e(r['date'])}</td><td>{e(r['debit'])}</td>"
                                     f"<td>{e(r['credit'])}</td><td>{e(r['amount'])}</td><td>{e(r['description'])}</td></tr>" for r in rows)
                           + "</table></details>")
    out.append("</section>")
    return "".join(out)


METRIC_LABELS = {
    "transactions": "Transactions (date + direction + amount)", "amount_direction": "Amount and direction",
    "amount_only": "Amount only", "date": "Date", "running_balance": "Running balance",
    "opening_balance": "Opening balance", "closing_balance": "Closing balance",
    "statement_fields": "Statement-level fields", "description": "Description", "duplicates": "Duplicate rows",
}


def _cell(m: dict | None) -> str:
    if m is None:
        return "<td>–</td>"
    if m["metric"] == "description":
        return "<td>" + badge("not measurable", "info") + f" <span class=muted>0 of {m['eligible']} labelled</span></td>"
    if m["metric"] == "duplicates":
        return f"<td>{m['incorrect']} of {m['eligible']} rows ({_pct(m['value'])})</td>"
    if not m["labelled"]:
        return f"<td>{badge('unscored', 'info')} <span class=muted>{m['eligible']} eligible, 0 labelled</span></td>"
    extra = ""
    if m["metric"] == "transactions":
        extra = (f"<br><span class=muted>P {_pct(m.get('precision'))} · R {_pct(m.get('recall'))} · F1 {_pct(m.get('f1'))} · "
                 f"missing {m.get('missing')} · extra {m.get('extra')}</span>")
    if m["metric"] == "amount_only":
        extra = f"<br><span class=muted>direction errors {m.get('direction_errors')}</span>"
    return (f"<td><b>{m['correct']}/{m['labelled']}</b> ({_pct(m['value'])})<br><span class=muted>coverage "
            f"{_pct(m['coverage'])} · unscored {m['unscored']}</span>{extra}</td>")


def _score_section(data: dict) -> str:
    scores = data.get("scores") or {}
    truth = data.get("ground_truth") or {}
    out = ['<section id="score"><h2>E. Ground-truth scorecard</h2>']
    if not truth.get("available"):
        return "".join(out) + "<p>No ground truth exists for this statement; nothing is scored.</p></section>"
    out.append(f"<p>Ground truth: {e(truth.get('file'))} · {e(truth.get('rows'))} labelled rows · {e(truth.get('rows_with_balance'))} with "
               f"balance · {e(truth.get('rows_with_description'))} with description · statement fields: {e(', '.join(truth.get('header_fields') or [])) or 'none'}"
               f" · asserted absent: {e(', '.join(truth.get('absent_fields') or [])) or 'none'}. Each pipeline is compared with the "
               "ground truth on its own; no pipeline is used as ground truth for another. A failed pipeline counts as extracting nothing.</p>")
    keys = [k for k in ORDER if k in scores]
    out.append("<div class='scroll'><table><tr><th>Metric</th><th>Definition</th>" + "".join(f"<th>{e(SHORT[k])}</th>" for k in keys) + "</tr>")
    for metric, label in METRIC_LABELS.items():
        row = {k: next((m for m in scores[k].get("metrics", []) if m["metric"] == metric), None) for k in keys}
        definition = next((m["definition"] for m in row.values() if m), "")
        out.append(f"<tr><td>{e(label)}</td><td class='muted'>{e(definition)}</td>" + "".join(_cell(row[k]) for k in keys) + "</tr>")
    out.append("</table></div>")
    fields = {k: next((m.get("fields") for m in scores[k].get("metrics", []) if m["metric"] == "statement_fields"), []) for k in keys}
    names = [f["field"] + (" (absent)" if f["asserted"] == "absent" else "") for f in (fields.get(keys[0]) or [])] if keys else []
    if names:
        out.append("<details><summary>Statement-level fields, one by one</summary><table><tr><th>Field</th>"
                   + "".join(f"<th>{e(SHORT[k])}</th>" for k in keys) + "</tr>")
        for i, name in enumerate(names):
            out.append(f"<tr><td>{e(name)}</td>" + "".join(
                f"<td>{yes(fields[k][i]['ok'], 'correct', 'wrong / missing') if i < len(fields[k]) else '–'}</td>" for k in keys) + "</tr>")
        out.append("</table></details>")
    out.append("</section>")
    return "".join(out)


def _grid_section(data: dict) -> str:
    grid = data.get("grid")
    if not grid:
        return ""
    out = ['<section id="grid"><h2>F. Table grid check</h2>',
           "<p class='muted'>Computer vision on the page finds the amount columns (white gaps between them, ruling lines where "
           "printed). Each amount is checked against the column it sits in. For A and B the model's recorded answers are "
           "replayed with this check before validation; nothing is sent to a model again. A correction is kept only if the "
           "statement's own checks are no worse.</p>"]
    if grid.get("error"):
        return "".join(out) + f"<p>{badge('grid check failed', 'bad')} {e(grid['error'])}</p></section>"
    out.append("<div class='scroll'><table><tr><th>Approach</th><th>Amounts checked</th><th>Corrections</th><th>Result used</th>"
               "<th>Transactions without → with grid</th><th>Amount + direction without → with grid</th></tr>")
    scores = data.get("scores") or {}

    def frac(score: dict | None, name: str) -> str:
        m = next((x for x in (score or {}).get("metrics", []) if x["metric"] == name), None)
        return f"{m['correct']}/{m['labelled']}" if m and m["labelled"] else "unscored"

    for key in ORDER:
        item = grid.get(key)
        if item is None:
            continue
        if item.get("skipped"):
            out.append(f"<tr><td>{e(SHORT[key])}</td><td colspan='5' class='muted'>{e(item['skipped'])}</td></tr>")
            continue
        corrections = item.get("corrected", item.get("moved", 0))
        moves = item.get("moves") or {}
        out.append(f"<tr><td>{e(SHORT[key])}</td><td>{e(item.get('checked', '–'))}</td>"
                   f"<td>{e(corrections)} {e(', '.join(f'{k}: {v}' for k, v in moves.items()))}</td><td>{e(item.get('used'))}</td>"
                   f"<td>{frac(scores.get(key), 'transactions')} → {frac(item.get('score'), 'transactions')}</td>"
                   f"<td>{frac(scores.get(key), 'amount_direction')} → {frac(item.get('score'), 'amount_direction')}</td></tr>")
    out.append("</table></div>")
    pages = [p for p in grid.get("pages") or [] if p]
    if pages:
        out.append("<details><summary>Amount columns found per page</summary><ul>" + "".join(
            f"<li>page {e(p['page'])}: {len(p['bands'])} column(s) · {e(p['note'])}</li>" for p in pages) + "</ul></details>")
    out.append("</section>")
    return "".join(out)


def _notes_section(data: dict, run_dir: Path) -> str:
    files = "".join(f"<li><a href='{e(m['path'])}'>{e(m['path'])}</a> <span class=muted>{m['bytes']} bytes</span></li>"
                    for m in data.get("manifest_preview", []))
    return (
        '<section id="notes"><h2>Method notes and limitations</h2><ul>'
        "<li>The same reading result (PyMuPDF text layer, RapidOCR where a page has no text) is used by A, B and the reference; "
        "nothing is OCR'd twice.</li>"
        "<li>A runs the application's analyze() with engine=groq and the injected local client. B runs the same steps with the "
        "Markdown-derived lines in place of the direct lines (same prompt, schema, chunk budget, temperature 0, seed, "
        "result mapping, cited-value verification, validation and completeness). The reference is analyze(engine=local).</li>"
        "<li>Unavoidable differences: the prompt describes horizontally aligned lines, which fits A's input better than "
        "Markdown; B's table columns come from the existing table detector (fixed column order, printed labels where "
        "the detector recorded them), so B inherits the detector's row and column decisions; B's provenance is per row, not "
        "per cell.</li>"
        "<li>The local client shows the JSON schema to the model in the system message (Groq's API does this for the "
        "hosted model); the text is identical for A and B.</li>"
        "<li>Results come from one model at temperature 0 with a fixed seed on one machine; other models, settings or "
        "statements can give different results. Synthetic statements do not establish real-world accuracy.</li>"
        "</ul>"
        f"<h3>Artifacts in this run folder</h3><ul>{files}</ul></section>")


def render_run(data: dict) -> str:
    run_dir = Path(data["run"]["run_dir"])
    data = dict(data)
    data["manifest_preview"] = [{"path": p.relative_to(run_dir).as_posix(), "bytes": p.stat().st_size}
                                for p in sorted(run_dir.rglob("*")) if p.is_file()]
    st = data.get("statement") or {}
    statuses = " ".join(f"{e(SHORT[k].split(' · ')[0])} {status_badge(((data.get('pipelines', {}).get(k) or {}).get('result') or {}).get('status'))}"
                        for k in ORDER)
    body = (f"<header class='top'><h1>Bank statement extraction PoC · side-by-side comparison</h1>"
            f"<p>{e(st.get('id'))} · run {e(data['run']['run_id'])}</p><p>{statuses}</p>"
            "<p>Evaluation tool · English only · separate from the application · results are actual measurements of this run</p></header><main>"
            + _summary_section(data) + _intermediate_section(data, run_dir) + _comparison_section(data)
            + _discrepancy_section(data) + _score_section(data) + _grid_section(data) + _notes_section(data, run_dir)
            + "<footer>Generated by experiments/markdown_ab (PoC). No scripts and no external resources are used by this page.</footer></main>")
    html_text = page(f"PoC comparison · {st.get('id')}", body)
    if (data.get("model") or {}).get("provider") == "groq":
        html_text = html_text.replace("Local LLM", "Groq LLM")
    return html_text


# --------------------------------------------------------------------------
# Batch summary
# --------------------------------------------------------------------------


def _metric(data: dict, key: str, name: str) -> dict | None:
    return next((m for m in (data.get("scores", {}).get(key) or {}).get("metrics", []) if m["metric"] == name), None)


def render_batch(batch: dict, runs: list[dict], batch_dir: Path) -> str:
    model = batch.get("model") or {}
    out = [f"<header class='top'><h1>Bank statement extraction PoC · batch summary</h1>"
           f"<p>{e(batch.get('set'))} · {len(runs)} of {len(batch.get('statements', []))} statements processed · {e(batch['batch_id'])}</p>"
           f"<p>Model {e(model.get('name') or 'none (Markdown inspection only)')} {e((model.get('digest') or '')[:12])} · "
           f"created {e(batch.get('created'))} UTC</p></header><main>"]
    net = batch.get("network_monitor") or {}
    state = batch.get("state") or "final"
    out.append("<section><h2>Run conditions</h2>" + kv([
        ("Summary state", badge(state, "ok" if state == "final" else "warn")),
        ("Statements", e(len(batch.get("statements", [])))), ("Processed", e(len(runs))),
        ("Not processed", e(", ".join(batch.get("unprocessed") or []) or "none")),
        ("Model", e(model.get("name") or "–")), ("Digest", e(model.get("digest") or "–")),
        ("Execution", "one statement at a time; A, then B, then the reference"),
        ("Network monitor", (yes(not net.get("violations"), f"{net.get('samples')} samples, no external connection", "EXTERNAL CONNECTION")
                             if net else badge("not used", "info"))),
        ("Live DB files unchanged", yes(batch.get("live_db_unchanged"), "yes", "changed (not opened by this process; the running app may write)")),
        ("App DB sentinel created", yes(not batch.get("app_db_sentinel_exists"), "no", "YES")),
    ]) + "</section>")

    keys = [k for k in ORDER]
    out.append("<section><h2>Per statement</h2><div class='scroll'><table><tr><th>Statement</th><th class='num'>Pages</th><th>Text / OCR</th>"
               "<th>Markdown</th>" + "".join(f"<th>{e(SHORT[k])}</th>" for k in keys) + "<th>Report</th></tr>")
    for data in runs:
        st = data.get("statement") or {}
        ch = st.get("characteristics") or {}
        md = data.get("markdown") or {}
        cells = []
        for k in keys:
            item = (data.get("pipelines") or {}).get(k) or {}
            result = item.get("result") or {}
            m = _metric(data, k, "transactions")
            if not item:
                cells.append("<td>" + badge("not run", "info") + "</td>")
                continue
            if item.get("skipped"):
                cells.append("<td>" + badge("skipped", "info") + "</td>")
                continue
            text = status_badge(result.get("status"))
            if m and m.get("labelled"):
                text += f"<br>F1 {_pct(m.get('f1'))} · recall {m['correct']}/{m['labelled']}"
            text += f"<br><span class=muted>{_ms(item.get('total_ms'))}</span>"
            if result.get("status") != "completed":
                text += f"<br><span class=muted>{e((result.get('error') or {}).get('code'))}</span>"
            cells.append(f"<td>{text}</td>")
        rel = os.path.relpath(Path(data["run"]["run_dir"]) / "report.html", batch_dir).replace(os.sep, "/")
        md_cell = ((badge("faithful", "ok") if md.get("faithful") else badge("content lost", "bad"))
                   + f"<br><span class=muted>{len(md.get('warnings') or [])} warning(s)</span>") if md else "–"
        run_status = data["run"].get("status")
        out.append(f"<tr><td>{e(st.get('id'))}{'' if run_status == 'completed' else '<br>' + status_badge(run_status)}</td>"
                   f"<td class='num'>{e(st.get('pages'))}</td><td>{e(ch.get('method'))}</td><td>{md_cell}</td>{''.join(cells)}"
                   f"<td><a href='{e(rel)}'>open</a></td></tr>")
    out.append("</table></div></section>")

    if not batch.get("markdown_only"):
        out.append("<section><h2>Aggregate metrics (processed statements with ground truth)</h2><p class='muted'>Counts are summed "
                   "over statements (micro-average). A failed pipeline counts as extracting nothing for that statement. No composite "
                   "score is computed.</p><div class='scroll'><table><tr><th>Metric</th>" + "".join(f"<th>{e(SHORT[k])}</th>" for k in keys) + "</tr>")
        for metric, label in METRIC_LABELS.items():
            cells = []
            for k in keys:
                ms = [m for d in runs if (m := _metric(d, k, metric))]
                if not ms:
                    cells.append("<td>–</td>")
                    continue
                eligible = sum(m["eligible"] for m in ms)
                labelled = sum(m["labelled"] for m in ms)
                if metric == "description":
                    cells.append("<td>" + badge("not measurable", "info") + f" <span class=muted>0 of {eligible} labelled</span></td>")
                    continue
                if metric == "duplicates":
                    dup = sum(m["incorrect"] for m in ms)
                    cells.append(f"<td>{dup} of {eligible} rows ({_pct(dup / eligible if eligible else None)})</td>")
                    continue
                correct = sum(m["correct"] for m in ms)
                extra = ""
                if metric == "transactions":
                    extracted = sum(m.get("extracted", 0) for m in ms)
                    p = correct / extracted if extracted else None
                    r = correct / labelled if labelled else None
                    f1 = 2 * p * r / (p + r) if p and r else 0.0
                    extra = f"<br><span class=muted>P {_pct(p)} · R {_pct(r)} · F1 {_pct(f1)} · extracted {extracted}</span>"
                value = correct / labelled if labelled else None
                cells.append(f"<td><b>{correct}/{labelled}</b> ({_pct(value)})<br><span class=muted>eligible {eligible} · coverage "
                             f"{_pct(labelled / eligible if eligible else None)}</span>{extra}</td>")
            out.append(f"<tr><td>{e(label)}</td>{''.join(cells)}</tr>")
        out.append("</table></div>")
        out.append("<h3>Run outcomes and time</h3><table><tr><th>Approach</th><th class='num'>Completed</th><th class='num'>Failed</th>"
                   "<th class='num'>Total time</th><th class='num'>Median per statement</th><th class='num'>LLM calls</th>"
                   "<th class='num'>Schema-valid calls</th></tr>")
        for k in keys:
            items = [(d.get("pipelines") or {}).get(k) or {} for d in runs]
            done = sum(1 for i in items if (i.get("result") or {}).get("status") == "completed")
            failed = sum(1 for i in items if i and (i.get("result") or {}).get("status") not in ("completed", None))
            times = sorted(i["total_ms"] for i in items if i.get("total_ms") is not None)
            median = times[len(times) // 2] if times else None
            calls = sum((i.get("llm") or {}).get("calls", 0) for i in items)
            valid = sum((i.get("llm") or {}).get("schema_valid_calls", 0) for i in items)
            out.append(f"<tr><td>{e(SHORT[k])}</td><td class='num'>{done}</td><td class='num'>{failed}</td><td class='num'>{_ms(sum(times))}</td>"
                       f"<td class='num'>{_ms(median)}</td><td class='num'>{calls if k != 'reference' else '–'}</td>"
                       f"<td class='num'>{valid if k != 'reference' else '–'}</td></tr>")
        out.append("</table></section>")
    out.append("<footer>Generated by experiments/markdown_ab (PoC). Links are relative paths to the per-statement reports. "
               "No scripts and no external resources are used by this page.</footer></main>")
    return page(f"PoC batch · {batch.get('set')}", "".join(out))


# --------------------------------------------------------------------------
# Index of all runs and batches
# --------------------------------------------------------------------------


def render_index(runs: list[dict], batches: list[dict]) -> str:
    """runs: {"dir", "run" (run.json or None), "status": {key: status}}; batches: {"dir", "batch" (batch.json)}."""
    out = ["<header class='top'><h1>Bank statement extraction PoC · all runs</h1>"
           f"<p>{len(runs)} run folder(s) · {len(batches)} batch(es) · open a report to see the side-by-side comparison</p></header><main>"]
    out.append("<section><h2>How to run</h2><p>From <code>statement-extractor/backend</code>:</p><pre>"
               ".venv/Scripts/python -m experiments.markdown_ab.run --list\n"
               ".venv/Scripts/python -m experiments.markdown_ab.run --statement ../data/corpus/uk_split_balance_bfwd.pdf "
               "--model llama3.2:3b --pin a80c4f17acd5\n"
               ".venv/Scripts/python -m experiments.markdown_ab.run --set corpus --markdown-only</pre></section>")
    out.append("<section><h2>Comparison runs (with the local model)</h2><div class='scroll'><table><tr><th>Run</th><th>Statement</th>"
               "<th>Model</th>" + "".join(f"<th>{e(SHORT[k])}</th>" for k in ORDER) + "<th>Local-only</th><th>Report</th></tr>")
    full = [r for r in runs if r["run"] and (r["run"].get("run") or {}).get("mode") == "full"]
    other = [r for r in runs if r not in full]
    for r in full:
        data = r["run"]
        cells = []
        for k in ORDER:
            status = r["status"].get(k)
            m = next((x for x in (data.get("scores", {}).get(k) or {}).get("metrics", []) if x["metric"] == "transactions"), None)
            text = status_badge(status)
            if m and m.get("labelled") and status == "completed":
                text += f"<br>F1 {_pct(m.get('f1'))} · {m['correct']}/{m['labelled']}"
            ms = ((data.get("pipelines") or {}).get(k) or {}).get("total_ms")
            text += f"<br><span class=muted>{_ms(ms)}</span>"
            cells.append(f"<td>{text}</td>")
        model = data.get("model") or {}
        lo = (data.get("local_only") or {}).get("ok")
        out.append(f"<tr><td>{e(r['dir'])}</td><td>{e((data.get('statement') or {}).get('id'))}</td>"
                   f"<td>{e(model.get('name'))}<br><span class=muted>{e((model.get('digest') or '')[:12])}</span></td>{''.join(cells)}"
                   f"<td>{yes(lo, 'verified', 'not verified')}</td><td><a href='{e(r['dir'])}/report.html'>open</a></td></tr>")
    if not full:
        out.append("<tr><td colspan='8' class='muted'>No comparison run yet.</td></tr>")
    out.append("</table></div></section>")
    out.append("<section><h2>Other run folders</h2><p class='muted'>Markdown inspection runs (no model calls) and folders of runs that "
               "did not finish (for example a stopped process): these have no comparison results.</p><details><summary>"
               f"{len(other)} folder(s)</summary><table><tr><th>Folder</th><th>Statement</th><th>State</th><th>Report</th></tr>")
    for r in other:
        data = r["run"]
        if data is None:
            out.append(f"<tr><td>{e(r['dir'])}</td><td>–</td><td>{badge('incomplete: no run.json (stopped before the end)', 'warn')}</td><td>–</td></tr>")
            continue
        mode = (data.get("run") or {}).get("mode")
        out.append(f"<tr><td>{e(r['dir'])}</td><td>{e((data.get('statement') or {}).get('id'))}</td><td>{e(mode)}</td>"
                   f"<td><a href='{e(r['dir'])}/report.html'>open</a></td></tr>")
    out.append("</table></details></section>")
    out.append("<section><h2>Batches</h2><table><tr><th>Batch</th><th>Set</th><th>Model</th><th>State</th><th>Summary</th></tr>")
    for b in batches:
        batch = b["batch"]
        if batch is None:
            out.append(f"<tr><td>{e(b['dir'])}</td><td colspan='3'>{badge('incomplete: no batch.json', 'warn')}</td><td>–</td></tr>")
            continue
        out.append(f"<tr><td>{e(b['dir'])}</td><td>{e(batch.get('set'))}</td><td>{e((batch.get('model') or {}).get('name') or 'none (Markdown only)')}</td>"
                   f"<td>{e(batch.get('state') or 'final')}</td><td><a href='{e(b['dir'])}/summary.html'>open</a></td></tr>")
    out.append("</table></section><footer>Generated by experiments/markdown_ab (PoC). No scripts and no external resources.</footer></main>")
    return page("PoC · all runs", "".join(out))
