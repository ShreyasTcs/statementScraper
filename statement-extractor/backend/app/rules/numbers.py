"""Locale-aware amount parsing.

The same figure is printed `1,234.56`, `1.234,56`, `1 234,56`, `1'234.56`,
`1,23,456.78` (lakh), `(1,234.56)`, `1.234,56-`, `1,234.56 CR` or `1.234,56 S`
depending on the bank. Nothing in the text says which, so the convention is
*inferred from the whole document* (`infer_convention`) and every cell is then
parsed against it.

A parse is strict: a cell is an amount only if *all* of it is one. That is what
lets the table engine tell a money cell from narration or a date.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.rules.currency import UNREADABLE_SYMBOLS, iso_codes


#: Arabic-Indic, Persian, Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada,
#: Malayalam, Thai, Lao, Tibetan and full-width digits -> ASCII, plus Arabic separators.
_DIGIT_BASES = (0x0660, 0x06F0, 0x0966, 0x09E6, 0x0A66, 0x0AE6, 0x0B66, 0x0BE6, 0x0C66, 0x0CE6, 0x0D66, 0x0E50, 0x0ED0, 0x0F20, 0xFF10)
_DIGIT_MAP: dict[int, str] = {base + k: str(k) for base in _DIGIT_BASES for k in range(10)}
_DIGIT_MAP.update({0x066B: ".", 0x066C: ",", 0x060C: ",", 0xFF0C: ",", 0xFF0E: "."})
#: Some fonts store a hyphen as a soft hyphen or a typographic hyphen: in a number it is a hyphen.
_DIGIT_MAP.update({0x00AD: "-", 0x2010: "-", 0x2011: "-"})


def canonical(text: str) -> str:
    """Digits and number punctuation in any script -> ASCII, so one parser serves them all."""
    return text.translate(_DIGIT_MAP)


@dataclass(frozen=True)
class NumberConvention:
    decimal: str = "."
    group: str = ","
    #: False when the document gave no evidence either way.
    confident: bool = False


DEFAULT_CONVENTION = NumberConvention()

_TOKEN = re.compile(r"(?<![\w.,])\d[\d.,'’  ]*\d(?![\w])")
#: "1 563,59" - a plain space as the thousands mark. It also settles 3-decimal
#: currencies, where "13 149,552" can only be comma-decimal.
_SPACE_TOKEN = re.compile(r"(?<![\w.,])\d{1,3}(?:[   ]\d{3})+(?:([.,])\d{1,4})?(?![\w])")
_GROUP_ONLY = "'’  "


def infer_convention(texts: Iterable[str]) -> NumberConvention:
    """Vote on decimal / group separators from every number-like token."""
    comma, dot = 0.0, 0.0
    groups: Counter[str] = Counter()
    for text in texts:
        for match in _SPACE_TOKEN.finditer(text):
            # Only "1 563,59" counts: a bare "984 851 006" is an organisation number.
            if match.group(1) == ",":
                comma += 3
                groups[" "] += 1
            elif match.group(1) == ".":
                dot += 3
                groups[" "] += 1
        for token in _TOKEN.findall(text):
            seps = [c for c in token if not c.isdigit()]
            if not seps:
                continue
            kinds = set(seps)
            last = seps[-1]
            after = len(token) - token.rfind(last) - 1
            # A group mark must really group: "1'234", not the "066'0" OCR makes of "0,66".
            for c in kinds & set(_GROUP_ONLY):
                if re.fullmatch(rf"\d{{1,3}}(?:{re.escape(c)}\d{{3}})+(?:[.,]\d+)?", token):
                    groups[c] += 1
            if "." in kinds and "," in kinds:
                if last == ",":
                    comma += 3
                else:
                    dot += 3
            elif len(kinds) == 1:
                sep = seps[0]
                if sep in _GROUP_ONLY:
                    continue
                if seps.count(sep) > 1:
                    # 1.234.567 -> '.' groups, so the other mark is the decimal.
                    if sep == ".":
                        comma += 2
                    else:
                        dot += 2
                elif after in (1, 2):
                    if sep == ",":
                        comma += 1.5
                    else:
                        dot += 1.5
                elif after == 3:
                    # 1.234 or 1,234: a thousands mark, weakly.
                    if sep == ".":
                        comma += 0.2
                    else:
                        dot += 0.2
            elif last == ",":
                comma += 2
            elif last == ".":
                dot += 2
    if comma == 0 and dot == 0:
        return DEFAULT_CONVENTION
    decimal = "," if comma > dot else "."
    default_group = "." if decimal == "," else ","
    group = groups.most_common(1)[0][0] if groups and groups.most_common(1)[0][1] >= 2 else default_group
    return NumberConvention(decimal, group, True)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

_DR = {"dr", "db", "deb", "d", "s"}
_CR = {"cr", "c", "h"}
_IND = r"cr|dr|db|deb|d|c|s|h"

_SHAPE = re.compile(
    rf"^(?P<pre>[+\-(]*)\s*(?P<i1>{_IND})?\s*(?P<num>\d[\d.,'’ ]*\d|\d)\s*(?P<post>[+\-)]*)\s*(?P<i2>{_IND})?\.?$",
    re.I,
)
_CURRENCY_WORDS = re.compile(
    r"(?<![A-Za-z])(?:rs\.?|us\$|r\$|a\$|c\$|s\$|hk\$|nz\$|kr\.?|zł|kč|eur|usd|gbp|inr|aed|chf|nok|sek|dkk)(?![A-Za-z])",
    re.I,
)
_SYMBOLS = "₹$€£¥₩₽₺₫₪฿₱₴" + "".join(UNREADABLE_SYMBOLS)


@dataclass(frozen=True)
class ParsedAmount:
    """One monetary cell. `value` is always the magnitude."""

    value: Decimal
    negative: bool = False
    #: "dr" / "cr" when the cell carried Dr/Cr, DB/CR, D/C or S/H.
    indicator: str | None = None
    #: "+" / "-" when an explicit sign was printed (before *or* after).
    sign: str | None = None
    #: True if OCR damage was repaired (e.g. 123.456.78 -> 123.456,78).
    repaired: bool = False
    #: Digits after the decimal mark; 0 when the cell had none.
    decimals: int = 0

    @property
    def signed(self) -> Decimal:
        """Dr / minus / parentheses are negative."""
        return -self.value if (self.negative or self.indicator == "dr") else self.value

    @property
    def direction(self) -> str | None:
        """'dr' / 'cr' if the cell says which way money moved, else None."""
        if self.indicator:
            return self.indicator
        if self.sign == "-" or self.negative:
            return "dr"
        if self.sign == "+":
            return "cr"
        return None


def _strip_currency(text: str) -> str:
    codes = iso_codes()

    def drop(match: re.Match[str]) -> str:
        return " " if match.group(0).upper() in codes or match.group(0).lower() in _SYMBOL_WORDS else match.group(0)

    cleaned = re.sub(r"(?<![A-Za-z])[A-Z]{3}(?![A-Za-z])", lambda m: " " if m.group(0) in codes else m.group(0), text)
    cleaned = _CURRENCY_WORDS.sub(" ", cleaned)
    for ch in _SYMBOLS:
        cleaned = cleaned.replace(ch, " ")
    return cleaned.strip()


_SYMBOL_WORDS = {"rs", "rs.", "kr", "kr."}


def _to_decimal(core: str, conv: NumberConvention) -> tuple[Decimal, int, bool] | None:
    """Return (value, decimals, repaired) for a bare numeric core, or None."""
    core = core.replace("’", "'").strip()
    dec = conv.decimal
    # The thousands mark is whatever the decimal mark is not (plus space and apostrophe).
    groups = {"." if dec == "," else ",", " ", "'"}

    if dec in core:
        head, _, frac = core.rpartition(dec)
        if dec in head or not frac.isdigit() or not 1 <= len(frac) <= 4:
            return _repair(core, conv)
    else:
        head, frac = core, ""

    # Group marks must sit between a leading 1-3 digits and 3-digit (or lakh 2-digit) groups.
    if not head:
        return None
    parts = re.split("[" + re.escape("".join(groups)) + "]", head)
    if len(parts) > 1:
        if not (1 <= len(parts[0]) <= 3 and all(len(p) in (2, 3) for p in parts[1:-1]) and len(parts[-1]) == 3):
            return _repair(core, conv)
        if any(not p.isdigit() for p in parts):
            return None
    elif not head.isdigit():
        return None
    try:
        value = Decimal("".join(parts) + ("." + frac if frac else ""))
    except InvalidOperation:
        return None
    return value, len(frac), False


def _repair(core: str, conv: NumberConvention) -> tuple[Decimal, int, bool] | None:
    """OCR often swaps the decimal mark: 123.456.78 should be 123.456,78."""
    seps = [m.start() for m in re.finditer(r"[.,]", core)]
    if len(seps) < 2:
        # Mixed marks in the wrong order for this convention: 1,234.56 in a comma-decimal file.
        other = ("." if conv.decimal == "," else ",")
        if other in core and conv.decimal in core and core.rfind(other) > core.rfind(conv.decimal):
            swapped = NumberConvention(other, conv.decimal)
            strict = _to_decimal(core, swapped)
            if strict:
                return strict[0], strict[1], False
        return None
    last = seps[-1]
    frac = core[last + 1:]
    if len(frac) != 2 or not frac.isdigit():
        return None
    head = core[:last]
    pieces = re.split(r"[.,' ]", head)
    if not (1 <= len(pieces[0]) <= 3 and all(len(p) == 3 for p in pieces[1:]) and all(p.isdigit() for p in pieces)):
        return None
    return Decimal("".join(pieces) + "." + frac), 2, True


def parse_amount(text: str, conv: NumberConvention = DEFAULT_CONVENTION) -> ParsedAmount | None:
    """Parse a cell that is *only* an amount (currency and sign allowed)."""
    cleaned = text.replace(" ", " ").replace(" ", " ").replace("−", "-").replace("–", "-").strip()
    if not cleaned or len(cleaned) > 40 or not any(ch.isdigit() for ch in cleaned):
        return None
    cleaned = _strip_currency(cleaned)
    match = _SHAPE.match(cleaned)
    if match is None:
        return None
    core = match.group("num")
    if " " in core and not re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d{1,4})?", core):
        return None
    parsed = _to_decimal(core, conv)
    if parsed is None:
        return None
    value, decimals, repaired = parsed

    pre, post = match.group("pre"), match.group("post")
    signs = pre + post
    indicator_raw = (match.group("i1") or match.group("i2") or "").lower()
    indicator = "dr" if indicator_raw in _DR else "cr" if indicator_raw in _CR else None
    sign = "-" if "-" in signs else "+" if "+" in signs else None
    negative = sign == "-" or "(" in pre or ")" in post
    return ParsedAmount(value, negative, indicator, sign, repaired, decimals)


def looks_like_money(text: str, conv: NumberConvention = DEFAULT_CONVENTION, zero_decimals: bool = False) -> bool:
    """A money cell, as opposed to a quantity, year or reference number.

    Requires a decimal part, a printed sign / Dr-Cr, or - for currencies with no
    minor unit - a grouped integer.
    """
    parsed = parse_amount(text, conv)
    if parsed is None:
        return False
    if parsed.decimals or parsed.sign or parsed.indicator or parsed.negative:
        return True
    return zero_decimals and (len(text.strip()) >= 4 or any(c in text for c in (conv.group, " ")))


def format_decimal(value: Decimal | None) -> str | None:
    """Plain, separator-free string with at least two decimals."""
    if value is None:
        return None
    if value.as_tuple().exponent >= -2:  # type: ignore[operator]
        return f"{value:.2f}"
    return f"{value:f}"
