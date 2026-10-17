"""Scorecard: run every sample through the real pipeline and compare with ground truth.

    python tests/make_samples.py      # the original three mocks   -> data/samples
    python tests/corpus.py            # the worldwide-format corpus -> data/corpus
    python tests/test_pipeline.py     # real statements (data/companySamples) + both of the above

Each PDF/image needs a sibling `<name>.expected.json`. A file passes when every
asserted header value matches, every row matches (date, debit, credit and - where
the statement prints one - balance), the opening/closing balances match, and no
reconciliation check *fails*.

Options:  --only <substring>   run files whose name contains it
          --verbose            print every mismatch, not just the first few
"""
from __future__ import annotations

import json
import re
import sys
from decimal import Decimal
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import fitz  # noqa: E402

from app.services import pipeline  # noqa: E402
from app.services.document_model import DocumentContent  # noqa: E402
from app.services.ocr_service import ocr_service  # noqa: E402
from app.services.pdf_service import TextReader  # noqa: E402

DATA = BACKEND.parent / "data"
FOLDERS = ["companySamples", "samples", "corpus"]
ALIASES = {"ifsc": "routing"}
NAME_FIELDS = {"account_holder", "bank_name", "branch", "account_type"}


def load(path: Path) -> DocumentContent:
    reader = TextReader()
    if path.suffix.lower() == ".pdf":
        content = reader.read(path, document_id=path.stem)
        if content.total_characters > 0:
            return content
        pages = [
            ocr_service.extract(reader.render_page(path, n), page_number=n, target_size=reader.page_size(path, n))
            for n in range(1, fitz.open(path).page_count + 1)
        ]
    else:
        pages = [ocr_service.extract(path, page_number=1)]
    return DocumentContent(document_id=path.stem, pages=pages, source="ocr", ocr_used=True)


def norm_text(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def same(field: str, got: str | None, want: str) -> bool:
    if got is None:
        return False
    if field in NAME_FIELDS:
        return norm_text(got) == norm_text(want)
    if field in ("opening_balance", "closing_balance", "total_debits", "total_credits"):
        try:
            return abs(Decimal(got) - Decimal(want)) < Decimal("0.0005")
        except Exception:
            return False
    return got == want


def money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:f}"


def run(path: Path, verbose: bool) -> tuple[bool, str, list[str]]:
    expected = json.loads(path.with_suffix(".expected.json").read_text(encoding="utf-8"))
    analysis = pipeline.analyze(load(path))
    problems: list[str] = []

    wanted = {ALIASES.get(k, k): v for k, v in expected["header"].items()}
    header_ok = 0
    for name, want in wanted.items():
        best = analysis.headers[name].best
        got = best.normalized if best else None
        if same(name, got, want):
            header_ok += 1
        else:
            problems.append(f"header {name}: got {got!r}, want {want!r}")
    for name in expected.get("absent", []):
        best = analysis.headers[ALIASES.get(name, name)].best
        if best is not None:
            problems.append(f"header {name}: should be absent, got {best.normalized!r}")

    rows = analysis.table.rows
    want_rows = expected["rows"]
    rows_ok = 0
    if len(rows) != len(want_rows):
        problems.append(f"row count: got {len(rows)}, want {len(want_rows)}")
    for index, (got, want) in enumerate(zip(rows, want_rows), start=1):
        actual = {
            "date": got.date.isoformat() if got.date else None,
            "debit": f"{got.debit:f}" if got.debit is not None else None,
            "credit": f"{got.credit:f}" if got.credit is not None else None,
        }
        wanted_row = {k: want.get(k) for k in ("date", "debit", "credit")}
        if "balance" in want:
            actual["balance"] = f"{got.balance:f}" if got.balance is not None else None
            wanted_row["balance"] = want["balance"]
        # amounts compare numerically, so 45.0 and 45.00 agree
        match = actual["date"] == wanted_row["date"] and all(
            (actual[k] is None) == (wanted_row[k] is None)
            and (actual[k] is None or abs(Decimal(actual[k]) - Decimal(wanted_row[k])) < Decimal("0.0005"))
            for k in wanted_row if k != "date"
        )
        if match:
            rows_ok += 1
        else:
            problems.append(f"row {index}: got {actual}, want {wanted_row}")

    totals = analysis.validation.totals
    for key, got in (("opening", totals.opening_balance), ("closing", totals.closing_balance)):
        want = expected.get(key)
        if want is not None and (got is None or abs(got - Decimal(want)) > Decimal("0.0005")):
            problems.append(f"{key} balance: got {money(got)}, want {want}")

    failing = [c for c in analysis.validation.checks if c.status == "fail"]
    for check in failing:
        problems.append(f"check FAILED - {check.label}: {check.detail}")
    if expected.get("mode") and totals.mode != expected["mode"]:
        problems.append(f"reconciliation mode: got {totals.mode}, want {expected['mode']}")

    ok = not problems
    summary = (
        f"header {header_ok}/{len(wanted)} | rows {rows_ok}/{len(want_rows)} | "
        f"mode {totals.mode} | confidence {analysis.overall:.2f}" if analysis.overall is not None else
        f"header {header_ok}/{len(wanted)} | rows {rows_ok}/{len(want_rows)} | mode {totals.mode}"
    )
    return ok, summary, problems


def run_mutation() -> list[str]:
    """A corrupted amount must be caught in both reconciliation modes."""
    failures: list[str] = []
    for label, folder, name, victim_index in (
        ("running balance", "samples", "statement_a_inr.pdf", 9),
        ("statement totals", "corpus", "de_split_nobalance.pdf", 5),
    ):
        path = DATA / folder / name
        if not path.exists():
            continue
        analysis = pipeline.analyze(load(path))
        rows = analysis.table.rows
        for row in rows:
            row.balance_check, row.issues = "unchecked", []
        victim = rows[victim_index]
        if victim.debit is not None:
            victim.debit += Decimal("100")
        else:
            victim.credit = (victim.credit or Decimal(0)) + Decimal("100")
        result = pipeline.validation_service.validate(analysis.headers, analysis.table)
        flagged = [c for c in result.checks if c.status == "fail"]
        status = "caught" if flagged else "MISSED"
        print(f"  mutation ({label}): altering row {victim_index + 1} by 100 -> {status}"
              + (f" [{flagged[0].label}]" if flagged else ""))
        if not flagged:
            failures.append(f"mutation not caught in {label} mode")
    return failures


def main() -> int:
    verbose = "--verbose" in sys.argv
    only = sys.argv[sys.argv.index("--only") + 1].lower() if "--only" in sys.argv else None
    paths: list[Path] = []
    for folder in FOLDERS:
        base = DATA / folder
        if base.exists():
            paths += sorted(p for p in base.iterdir()
                            if p.suffix.lower() in (".pdf", ".jpg", ".jpeg", ".png") and p.with_suffix(".expected.json").exists())
    if only:
        paths = [p for p in paths if only in p.name.lower()]
    if not paths:
        print("No samples with ground truth found. Run tests/make_samples.py and tests/corpus.py.")
        return 2

    results: list[tuple[str, bool]] = []
    current = None
    for path in paths:
        if path.parent.name != current:
            current = path.parent.name
            print(f"\n[{current}]")
        try:
            ok, summary, problems = run(path, verbose)
        except Exception as exc:  # a crash is a failure with a reason
            ok, summary, problems = False, "CRASHED", [f"{type(exc).__name__}: {exc}"]
        print(f"  {'PASS' if ok else 'FAIL'}  {path.name[:46]:46} {summary}")
        for problem in problems if verbose else problems[:3]:
            print(f"        - {problem}")
        if not verbose and len(problems) > 3:
            print(f"        ... and {len(problems) - 3} more (use --verbose)")
        results.append((path.name, ok))

    print("\n[mutation]")
    mutation_failures = run_mutation()

    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} statements passed; {'all' if not mutation_failures else 'NOT all'} corrupted-amount checks caught")
    return 0 if passed == len(results) and not mutation_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
