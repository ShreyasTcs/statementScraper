"""Small shared text helpers.

Amounts live in `numbers.py`, dates in `dates.py`, currencies in `currency.py`
and vocabulary in `lexicon.py`; this module keeps only what is left over.
"""
from __future__ import annotations

import re

_LABEL_TRAIL_RE = re.compile(r"^[\s:\-\u2013\u2014.#=]+")
_LABEL_LEAD_RE = re.compile(r"[\s:\-\u2013\u2014.#=]+$")


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def clean_value(value: str) -> str:
    """Trim the separators that sit between a label and its value."""
    return normalize_text(_LABEL_LEAD_RE.sub("", _LABEL_TRAIL_RE.sub("", value)))


def contains_anchor(text: str, anchor: str) -> bool:
    """Whole-word anchor match, tolerant of ':' and punctuation."""
    pattern = rf"(?<![a-z0-9]){re.escape(anchor.lower())}(?![a-z0-9])"
    return re.search(pattern, text.lower()) is not None
