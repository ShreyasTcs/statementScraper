"""Compare extraction engines on statements with known ground truth.

    python tests/evaluate_engines.py                                 # local engine, synthetic scenarios
    python tests/evaluate_engines.py --engines local,groq,hybrid --client simulated
    python tests/evaluate_engines.py --folder ../data/corpus         # + the generated corpus (local only)
    python tests/evaluate_engines.py --engines groq --client replay --replay-dir recordings/
    python tests/evaluate_engines.py --engines groq --client live --i-understand-this-sends-documents-to-groq

Clients for the groq / hybrid engines:

  simulated  the SIMULATED ideal model from ``llm_fixtures`` (synthetic scenarios only).
             It measures the deterministic layer, not Groq's real accuracy.
  replay     answers recorded earlier with ``--record`` (fails loudly on a miss).
  live       real Groq calls. Needs GROQ_API_KEY and ALLOW_EXTERNAL_LLM_CALLS=true,
             plus the explicit flag above. Refuses data/companySamples unless
             --authorized-confidential is also given.

Metrics (per engine): header accuracy, transaction precision / recall, direction
accuracy, duplicated rows, omitted rows, reconciliation rate, completeness
statuses, latency, tokens and - when GROQ_PRICE_INPUT_PER_MTOK /
GROQ_PRICE_OUTPUT_PER_MTOK are set - cost. Prices are not built in: they change.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import statement_factory as sf  # noqa: E402
from llm_fixtures import oracle  # noqa: E402

from app.services import pipeline  # noqa: E402
from app.services.llm_client import GroqClient, RecordingClient, ReplayClient, ScriptedClient  # noqa: E402
from app.services.pdf_service import TextReader  # noqa: E402

CONFIDENTIAL = "companySamples"


@dataclass
class Case:
    name: str
    path: Path
    truth: dict
    simulatable: bool = False


@dataclass
class Score:
    docs: int = 0
    header_ok: int = 0
    header_total: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0
    direction_ok: int = 0
    direction_total: int = 0
    duplicates: int = 0
    reconciled: int = 0
    statuses: Counter = field(default_factory=Counter)
    seconds: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    errors: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)


def scenarios(tmp: Path) -> list[Case]:
    specs = {
        "clean": sf.Spec(), "newest_first": sf.Spec(newest_first=True), "no_balance": sf.Spec(running_balance=False),
        "duplicate_line": sf.Spec(duplicate_index=3), "omitted_line": sf.Spec(omit_index=4),
        "empty": sf.Spec(empty=True), "long": sf.Spec(end=date(2026, 12, 31)),
    }
    cases = []
    for name, spec in specs.items():
        path = tmp / f"{name}.pdf"
        txns = sf.default_txns(spec, count=60) if name == "long" else None
        truth = sf.build(path, spec, txns)
        cases.append(Case(name, path, truth, simulatable=True))
    return cases


def folder_cases(folder: Path) -> list[Case]:
    out = []
    for path in sorted(folder.glob("*.pdf")):
        expected = path.with_suffix(".expected.json")
        if expected.exists():
            data = json.loads(expected.read_text(encoding="utf-8"))
            out.append(Case(path.stem, path, {"header": data.get("header", {}), "rows": data.get("rows", [])}))
    return out


def _num(value) -> Decimal | None:
    """Compare amounts as numbers: 120407 == 120407.00, whatever the currency's decimals."""
    if value in (None, ""):
        return None
    return Decimal(str(value)).normalize()


def _key(row: dict) -> tuple:
    return (row.get("date"), _num(row.get("debit")), _num(row.get("credit")))


def _amount_key(row: dict) -> tuple:
    return (row.get("date"), _num(row.get("debit")) or _num(row.get("credit")))


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def score(case: Case, analysis, s: Score) -> None:
    got = [{"date": r.date.isoformat() if r.date else None, "debit": _fmt(r.debit), "credit": _fmt(r.credit)}
           for a in analysis.accounts for r in a.table.rows]
    want = [{"date": r["date"], "debit": r.get("debit"), "credit": r.get("credit")} for r in case.truth["rows"]]
    need, have = Counter(_key(r) for r in want), Counter(_key(r) for r in got)
    tp = sum((need & have).values())
    s.tp += tp
    s.fp += sum((have - need).values())
    s.fn += sum((need - have).values())
    # A duplicated row: extracted more often than printed-and-real.
    s.duplicates += sum(max(0, have[k] - need[k]) for k in have if k in need)
    by_amount = Counter(_amount_key(r) for r in want)
    for row in got:
        if by_amount[_amount_key(row)]:
            s.direction_total += 1
            s.direction_ok += _key(row) in need
    for name, value in case.truth.get("header", {}).items():
        if name in ("account_holder", "account_number", "period_from", "period_to", "opening_balance", "closing_balance",
                    "iban", "bic", "total_debits", "total_credits", "currency"):
            s.header_total += 1
            best = analysis.headers.get(name).best if name in analysis.headers else None
            if best is not None:
                ok = best.normalized == value
                if not ok and name.endswith(("balance", "debits", "credits")):
                    try:
                        ok = Decimal(best.normalized) == Decimal(value)
                    except Exception:
                        ok = False
                s.header_ok += ok
    s.statuses[analysis.completeness.status] += 1
    for reason in analysis.completeness.reason_codes:
        s.reasons[reason["code"]] += 1
    s.reconciled += analysis.completeness.status == "complete"
    if analysis.engine.llm is not None:
        s.tokens_in += analysis.engine.llm.input_tokens
        s.tokens_out += analysis.engine.llm.output_tokens


def client_for(kind: str, case: Case, args):
    if kind == "simulated":
        if not case.simulatable:
            return None
        return ScriptedClient([oracle(case.truth) for _ in range(64)])
    if kind == "replay":
        return ReplayClient(Path(args.replay_dir))
    if kind == "live":
        client = GroqClient()
        return RecordingClient(client, Path(args.record)) if args.record else client
    raise SystemExit(f"unknown client {kind}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--engines", default="local")
    parser.add_argument("--client", default="simulated", choices=["simulated", "replay", "live"])
    parser.add_argument("--folder", action="append", default=[])
    parser.add_argument("--replay-dir")
    parser.add_argument("--record", help="with --client live: store answers here for later replay (synthetic data only)")
    parser.add_argument("--out", help="write the metrics as JSON")
    parser.add_argument("--i-understand-this-sends-documents-to-groq", dest="live_ok", action="store_true")
    parser.add_argument("--authorized-confidential", action="store_true")
    args = parser.parse_args()
    engines = [e.strip() for e in args.engines.split(",") if e.strip()]

    if args.client == "live" and any(e != "local" for e in engines):
        if not args.live_ok:
            raise SystemExit("Refusing live Groq calls without --i-understand-this-sends-documents-to-groq.")
        if any(CONFIDENTIAL in f for f in args.folder) and not args.authorized_confidential:
            raise SystemExit("Refusing to send confidential company statements to Groq without --authorized-confidential.")

    tmp = Path(tempfile.mkdtemp(prefix="stmt-eval-"))
    cases = scenarios(tmp)
    for folder in args.folder:
        cases += folder_cases(Path(folder))
    reader = TextReader()
    contents = {c.name: reader.read(c.path, document_id=c.name) for c in cases}

    report: dict[str, dict] = {}
    for engine in engines:
        s = Score()
        for case in cases:
            client = None
            if engine != "local":
                client = client_for(args.client, case, args)
                if client is None:
                    continue  # the simulated model only knows the synthetic scenarios
            started = time.perf_counter()
            try:
                analysis = pipeline.analyze(contents[case.name], engine=engine, client=client)
            except pipeline.ProcessingError as exc:
                s.errors[exc.code] += 1
                s.docs += 1
                s.fn += len(case.truth["rows"])
                continue
            s.seconds += time.perf_counter() - started
            s.docs += 1
            score(case, analysis, s)
        price_in = os.environ.get("GROQ_PRICE_INPUT_PER_MTOK")
        price_out = os.environ.get("GROQ_PRICE_OUTPUT_PER_MTOK")
        cost = None
        if price_in and price_out:
            cost = round(s.tokens_in / 1e6 * float(price_in) + s.tokens_out / 1e6 * float(price_out), 6)
        report[engine] = {
            "documents": s.docs,
            "client": None if engine == "local" else args.client,
            "header_accuracy": round(s.header_ok / s.header_total, 4) if s.header_total else None,
            "txn_precision": round(s.tp / (s.tp + s.fp), 4) if s.tp + s.fp else None,
            "txn_recall": round(s.tp / (s.tp + s.fn), 4) if s.tp + s.fn else None,
            "direction_accuracy": round(s.direction_ok / s.direction_total, 4) if s.direction_total else None,
            "duplicated_rows": s.duplicates,
            "omitted_rows": s.fn,
            "reconciliation_rate": round(s.reconciled / s.docs, 4) if s.docs else None,
            "completeness": dict(s.statuses),
            "completeness_reasons": dict(s.reasons),
            "errors": dict(s.errors),
            "seconds_per_document": round(s.seconds / s.docs, 3) if s.docs else None,
            "tokens_in": s.tokens_in,
            "tokens_out": s.tokens_out,
            "cost": cost,
        }

    columns = ["documents", "header_accuracy", "txn_precision", "txn_recall", "direction_accuracy", "duplicated_rows",
               "omitted_rows", "reconciliation_rate", "seconds_per_document", "tokens_in", "tokens_out", "cost"]
    print("| metric | " + " | ".join(report) + " |")
    print("|---|" + "---|" * len(report))
    for column in columns:
        print(f"| {column} | " + " | ".join(str(report[e][column]) for e in report) + " |")
    print("| completeness | " + " | ".join(json.dumps(report[e]["completeness"]) for e in report) + " |")
    print("| why not complete | " + " | ".join(json.dumps(report[e]["completeness_reasons"]) for e in report) + " |")
    if args.client == "simulated" and any(e != "local" for e in engines):
        print("\nNote: groq/hybrid used the SIMULATED model - these numbers measure the deterministic layer, "
              "not Groq's accuracy.")
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
