"""Held-out breadth test: random combinations of statement conventions.

    python tests/fuzz.py [count] [seed]

The curated corpus (tests/corpus.py) is what the extractor was tuned against, so
passing it proves little about statements it has not seen. This draws random
styles - language, number format, sign convention, date format, header layout,
columns, balance or no balance - renders each as a PDF with its ground truth,
runs the real pipeline and reports the pass rate and which settings fail.
"""
from __future__ import annotations

import random
import sys
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import corpus  # noqa: E402
import test_pipeline as scorecard  # noqa: E402

LANG_CURRENCY = {"en": ["USD", "GBP", "AUD", "SGD"], "uk": ["GBP"], "de": ["EUR", "CHF"], "fr": ["EUR", "CHF"],
                 "es": ["EUR", "MXN"], "nl": ["EUR"], "pt": ["BRL", "EUR"]}
FORMATS = [(",", "."), (".", ","), (",", " "), (".", "'"), (".", ",")]
DATES_ANY = ["%d.%m.%Y", "%d/%m/%Y", "%d-%m-%y", "%Y-%m-%d", "%d.%m.%y", "%d/%m/%y"]
HOLDERS = ["Alpha Trading GmbH", "Maria Sanchez Lopez", "Orion Logistics Ltd", "Dupont et Fils SARL", "J. A. Brown",
           "Nova Consulting BV", "Silva & Irmaos Ltda", "Pacific Imports Pty Ltd"]
BANKS = ["First Meridian Bank", "Banco Continental", "Banque du Nord", "Rijn Handelsbank", "Atlantic Savings Bank"]


def random_style(index: int, rng: random.Random) -> corpus.Style:
    lang = rng.choice(list(LANG_CURRENCY))
    currency = rng.choice(LANG_CURRENCY[lang])
    dec, grp = rng.choice(FORMATS)
    decimals = rng.choices([2, 0, 3], weights=[8, 1, 1])[0]
    if decimals != 2:
        currency = "JPY" if decimals == 0 else "KWD"
    neg = rng.choice(["split", "sign", "paren", "trail", "drcr", "sh", "cd"])
    mode = "split" if neg == "split" else rng.choice(["signed", "signed", "typed"])
    if mode == "typed" and neg in ("sign", "paren", "trail"):
        neg = "sign"
    has_balance = rng.random() < 0.6
    show_summary = True if not has_balance else rng.random() < 0.7
    date_fmt = rng.choice(DATES_ANY + ["babel:" + {"en": "en", "uk": "en_GB", "de": "de", "fr": "fr", "es": "es",
                                                    "nl": "nl", "pt": "pt_BR"}[lang]])
    if date_fmt == "babel:de" and rng.random() < 0.5:
        date_fmt = "%d.%m.%Y"
    # Day-first numeric formats need a day above 12 to be unambiguous in a short statement.
    start = date(rng.choice([2024, 2025, 2026]), rng.randint(1, 11), 1)
    end = date(start.year, start.month + 1, 1) - (date(2000, 1, 2) - date(2000, 1, 1))
    opening = Decimal(rng.randint(2_000, 90_000))
    if decimals:
        opening += Decimal(rng.randint(0, 99)) / 100 if decimals == 2 else Decimal(rng.randint(0, 999)) / 1000
    return corpus.Style(
        name=f"fz{index:03d}_{lang}_{neg}_{mode}_{'bal' if has_balance else 'nobal'}",
        lang=lang, currency=currency, decimals=decimals, dec=dec, grp=grp, neg=neg, mode=mode,
        has_balance=has_balance, date_fmt=date_fmt, header=rng.choice(["inline", "cells", "stacked"]),
        holder=rng.choice(HOLDERS), bank=rng.choice(BANKS),
        iban=rng.choice([None] + [i for i, c in (("DE89 3704 0044 0532 0130 00", "EUR"), ("FR14 2004 1010 0505 0001 3M02 606", "EUR"),
                                                  ("NL91 ABNA 0417 1643 00", "EUR")) if c == currency]),
        account=rng.choice([None, "123456789012", "40-47-84 70872490"]),
        lakh=(dec, grp) == (".", ",") and rng.random() < 0.2,
        value_date=rng.random() < 0.3, stacked_headings=rng.random() < 0.25,
        show_summary=show_summary, show_totals=rng.random() < 0.3 and not has_balance,
        balance_rows=rng.random() < 0.2, currency_show=rng.choice(["code", "code", "none"]),
        rows=rng.randint(8, 45), start=start, end=end, opening=opening, seed=rng.randint(1, 10_000),
    )


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else 2026
    rng = random.Random(seed)
    corpus.OUT_DIR = HERE.parent.parent / "data" / "fuzz"
    corpus.OUT_DIR.mkdir(parents=True, exist_ok=True)
    for stale in corpus.OUT_DIR.glob("*"):
        stale.unlink()

    failures: list[tuple[corpus.Style, list[str]]] = []
    for i in range(count):
        style = random_style(i, rng)
        corpus.render(style)
        path = corpus.OUT_DIR / f"{style.name}.pdf"
        try:
            ok, _, problems = scorecard.run(path, False)
        except Exception as exc:
            ok, problems = False, [f"CRASH {type(exc).__name__}: {exc}"]
        if not ok:
            failures.append((style, problems))

    passed = count - len(failures)
    print(f"\nfuzz seed={seed}: {passed}/{count} statements passed ({100 * passed / count:.0f}%)")
    axes: dict[str, Counter] = defaultdict(Counter)
    for style, _ in failures:
        for axis, value in (("lang", style.lang), ("neg", style.neg), ("mode", style.mode), ("balance", style.has_balance),
                            ("decimals", style.decimals), ("grouping", repr((style.dec, style.grp))),
                            ("date", style.date_fmt), ("header", style.header)):
            axes[axis][value] += 1
    if failures:
        print("\nfailures by setting:")
        for axis, counter in axes.items():
            print(f"  {axis:9}", ", ".join(f"{k}: {v}" for k, v in counter.most_common(6)))
        print("\nfirst problems:")
        for style, problems in failures[:12]:
            print(f"  {style.name}  [{style.date_fmt} | {style.dec}{style.grp} | header={style.header} | summary={style.show_summary}]")
            for problem in problems[:2]:
                print(f"      - {problem}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
