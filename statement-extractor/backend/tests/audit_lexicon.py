"""Audit the vocabulary table for phrases that mean two things.

    python tests/audit_lexicon.py

A phrase listed under two concepts of the same group is a conflict: the longer or
earlier one wins silently. It happened once already - the Swedish "Valör" (value
date) and the Portuguese/Spanish "Valor" (amount) fold to the same word. Run this
after adding wording; it exits non-zero if any conflict exists.
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rules import lexicon  # noqa: E402


def main() -> int:
    conflicts = 0
    for group, table in lexicon.GROUPS.items():
        seen: dict[str, set[str]] = defaultdict(set)
        for concept, blob in table.items():
            for phrase in blob.split("|"):
                key = " ".join(lexicon.tokens(phrase))
                if key:
                    seen[key].add(concept)
        clashes = {k: v for k, v in seen.items() if len(v) > 1}
        print(f"[{group}] {len(seen)} phrases, {len(clashes)} conflicts")
        for phrase, concepts in sorted(clashes.items()):
            print(f"    {phrase!r} -> {', '.join(sorted(concepts))}")
        conflicts += len(clashes)
    return 1 if conflicts else 0


if __name__ == "__main__":
    raise SystemExit(main())
