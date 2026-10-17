"""IBAN and BIC recognition (ISO 13616 / ISO 9362).

An IBAN is validated with its mod-97 check, so a stray long number is never
mistaken for one - even in OCR text where spaces were lost and a label is glued
to the value (`IBANFI1234567890123456`).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

#: IBAN length per country (ISO 13616 registry).
IBAN_LENGTH: dict[str, int] = {
    "AD": 24, "AE": 23, "AL": 28, "AT": 20, "AZ": 28, "BA": 20, "BE": 16, "BG": 22, "BH": 22, "BR": 29,
    "BY": 28, "CH": 21, "CR": 22, "CY": 28, "CZ": 24, "DE": 22, "DK": 18, "DO": 28, "EE": 20, "EG": 29,
    "ES": 24, "FI": 18, "FO": 18, "FR": 27, "GB": 22, "GE": 22, "GI": 23, "GL": 18, "GR": 27, "GT": 28,
    "HR": 21, "HU": 28, "IE": 22, "IL": 23, "IQ": 23, "IS": 26, "IT": 27, "JO": 30, "KW": 30, "KZ": 20,
    "LB": 28, "LC": 32, "LI": 21, "LT": 20, "LU": 20, "LV": 21, "MC": 27, "MD": 24, "ME": 22, "MK": 19,
    "MR": 27, "MT": 31, "MU": 30, "NL": 18, "NO": 15, "PK": 24, "PL": 28, "PS": 29, "PT": 25, "QA": 29,
    "RO": 24, "RS": 22, "SA": 24, "SC": 31, "SE": 24, "SI": 19, "SK": 24, "SM": 27, "ST": 25, "SV": 28,
    "TL": 23, "TN": 24, "TR": 26, "UA": 29, "VA": 22, "VG": 24, "XK": 20,
}

_CANDIDATE = re.compile(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}")
_BIC = re.compile(r"[A-Z]{4}([A-Z]{2})[A-Z0-9]{2}(?:[A-Z0-9]{3})?")


def mod97_valid(iban: str) -> bool:
    if len(iban) < 15 or not iban.isalnum() or not iban.isascii():
        return False
    rearranged = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in rearranged)
    return int(digits) % 97 == 1


@dataclass(frozen=True)
class FoundIban:
    value: str  # compact, upper-case
    country: str


def find_ibans(text: str) -> list[FoundIban]:
    """Every checksum-valid IBAN in `text`, tolerant of spaces and glued labels."""
    squashed = re.sub(r"[\s.\-]", "", text.upper())
    found: list[FoundIban] = []
    seen: set[str] = set()
    for start in range(len(squashed) - 14):
        if not (squashed[start:start + 2].isalpha() and squashed[start + 2:start + 4].isdigit()):
            continue
        country = squashed[start:start + 2]
        length = IBAN_LENGTH.get(country)
        if not length:
            continue
        candidate = squashed[start:start + length]
        if len(candidate) == length and candidate not in seen and mod97_valid(candidate):
            seen.add(candidate)
            found.append(FoundIban(candidate, country))
    return found


def find_bic(text: str) -> str | None:
    """A BIC / SWIFT code with a plausible country, e.g. `DNBANOKKXXX`, `BPMOIT22 XXX`."""
    squashed = re.sub(r"\s", "", text.upper())
    for match in _BIC.finditer(squashed):
        code = match.group(0)
        if match.group(1) in IBAN_LENGTH or match.group(1) in {"IN", "US", "CN", "JP", "AU", "CA", "SG", "HK", "AE"}:
            if len(code) in (8, 11) and not code.isdigit():
                return code
    return None
