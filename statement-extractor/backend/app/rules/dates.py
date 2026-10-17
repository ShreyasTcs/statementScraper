"""Multilingual date parsing.

Month names come from CLDR (via Babel), so "ago", "août", "Mär", "tammi",
"sept." and "Aug" all resolve without a per-language table. Formats covered:

    2026-02-11   11/02/2026   11.02.26   11 Feb 2026   Feb 11, 2026
    11-Feb-26    20260211     11/02 (year supplied by the caller)

Day/month order is *inferred per document* (`infer_order`), because `03/04/2026`
means different things in Oslo and in Chicago. A Thai Buddhist-era year
(2569) is converted to Gregorian.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import date
from functools import lru_cache

from babel import Locale, dates as babel_dates

#: Locales whose month names are loaded. Latin-script languages plus a few
#: others whose months are commonly abbreviated in Latin letters on statements.
_LOCALES = (
    "en", "it", "de", "fr", "es", "pt", "nl", "nb", "nn", "da", "sv", "fi", "pl", "cs", "sk", "hu",
    "ro", "tr", "id", "ms", "vi", "hr", "sl", "et", "lv", "lt", "ca", "gl", "is", "sq", "af", "sw",
    "en_IN", "en_GB", "en_AU", "pt_BR", "es_MX", "fr_CA", "de_CH", "de_AT",
)


def fold(text: str) -> str:
    """Lower-case and strip accents, one character in, one character out.

    Keeping the length equal means a match position in the folded text is the
    same position in the original, whatever script surrounds it.
    """
    out = []
    for ch in text:
        base = "".join(c for c in unicodedata.normalize("NFKD", ch.lower()) if not unicodedata.combining(c))
        out.append(base if len(base) == 1 else ch.lower())
    return "".join(out)


#: The languages whose reading of an ambiguous token wins ("mar" is March, not
#: whatever a smaller locale makes of it).
_PRIMARY = ("en", "en_GB", "en_IN", "de", "fr", "it", "es", "pt", "pt_BR", "nl", "sv", "nb", "da", "fi")


def _collect(codes: tuple[str, ...]) -> dict[str, set[int]]:
    seen: dict[str, set[int]] = defaultdict(set)
    for code in codes:
        try:
            locale = Locale.parse(code)
        except Exception:
            continue
        for width in ("abbreviated", "wide"):
            for context in ("format", "stand-alone"):
                names = babel_dates.get_month_names(width, context=context, locale=locale)
                for number, name in names.items():
                    token = fold(name).strip(". ")
                    if len(token) >= 3 and token.isalpha():
                        seen[token].add(number)
                        seen[token[:3]].add(number)
    return seen


@lru_cache(maxsize=1)
def month_names() -> dict[str, int]:
    """folded month token -> month number.

    Main languages are read first, and where they agree the token is settled.
    Other locales only add tokens nobody else claims.
    """
    primary = _collect(_PRIMARY)
    names = {t: next(iter(m)) for t, m in primary.items() if len(m) == 1}
    # Where the main languages disagree, English decides ("mar", "may", "jun").
    english = _collect(("en",))
    for token, months in primary.items():
        if len(months) > 1 and token in english and len(english[token]) == 1:
            names[token] = next(iter(english[token]))
    for token, months in _collect(tuple(c for c in _LOCALES if c not in _PRIMARY)).items():
        if token not in names and token not in primary and len(months) == 1:
            names[token] = next(iter(months))
    return names


@lru_cache(maxsize=1)
def _month_alternation() -> str:
    return "|".join(re.escape(t) for t in sorted(month_names(), key=len, reverse=True))


def _year(raw: str) -> int | None:
    year = int(raw)
    if len(raw) == 4 and year < 1990:
        return None
    if year < 100:
        year += 2000 if year < 70 else 1900
    if 2500 <= year <= 2700:  # Buddhist era
        year -= 543
    return year


def is_plausible(value: date) -> bool:
    today = date.today()
    return date(1990, 1, 1) <= value <= date(today.year + 2, 12, 31)


def _make(year: int | None, month: int, day: int) -> date | None:
    if year is None:
        return None
    try:
        value = date(year, month, day)
    except ValueError:
        return None
    return value if is_plausible(value) else None


@lru_cache(maxsize=1)
def _patterns() -> tuple[tuple[re.Pattern[str], str], ...]:
    months = _month_alternation()
    return (
        (re.compile(r"(?<!\d)(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})(?!\d)"), "ymd"),
        (re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4}|\d{2})(?![\d])"), "num"),
        (re.compile(rf"(?<![a-z0-9])(\d{{1,2}})[\s\-/.]*({months})\.?[\s\-/.,]*(\d{{4}}|\d{{2}})(?!\d)", re.I), "dmy_text"),
        (re.compile(rf"(?<![a-z0-9])({months})\.?[\s\-/.]*(\d{{1,2}})[\s\-/.,]+(\d{{4}}|\d{{2}})(?!\d)", re.I), "mdy_text"),
        (re.compile(rf"(?<![a-z0-9])(\d{{1,2}})[\s\-/.]*({months})\.?(?![a-z0-9])", re.I), "dm_text"),
        # 2025年3月5日, 2025\uB144 3\uC6D4 5\uC77C
        (re.compile(r"(?<!\d)(\d{4})\s*[\u5e74\ub144]\s*(\d{1,2})\s*[\u6708\uc6d4]\s*(\d{1,2})\s*[\u65e5\uc77c]?"), "ymd"),
        # Japanese eras: \u4ee4\u548c7\u5e743\u67085\u65e5, R7.03.05
        (re.compile(r"(?<![a-z0-9])(\u4ee4\u548c|\u5e73\u6210|\u662d\u548c|r|h|s)\s*(\d{1,2})\s*[\u5e74./\-]\s*(\d{1,2})\s*[\u6708./\-]\s*(\d{1,2})\s*\u65e5?"), "era"),
    )


_COMPACT = re.compile(r"(?<!\d)(20\d{2}|19\d{2})(\d{2})(\d{2})(?!\d)")
_NO_YEAR = re.compile(r"(?<![\d/.\-])(\d{1,2})[/.\-](\d{1,2})(?![\d/.\-])")


def find_dates(
    text: str,
    order: str = "dmy",
    year: int | None = None,
    allow_compact: bool = False,
) -> list[tuple[date, int, int]]:
    """Every plausible date in `text`, left to right, as (date, start, end).

    `year` supplies the year for dates printed without one (`31/07`, `03 Aug`).
    """
    names = month_names()
    folded_text = fold(text)
    found: list[tuple[date, int, int]] = []

    for pattern, kind in _patterns():
        for match in pattern.finditer(folded_text):
            groups = match.groups()
            value: date | None = None
            if kind == "ymd":
                value = _make(_year(groups[0]), int(groups[1]), int(groups[2]))
            elif kind == "num":
                a, b, y = int(groups[0]), int(groups[1]), _year(groups[2])
                first, second = ((b, a), (a, b)) if order == "dmy" else ((a, b), (b, a))  # (month, day)
                value = _make(y, *first) or _make(y, *second)
            elif kind == "dmy_text":
                month = names.get(groups[1].lower().strip("."))
                value = _make(_year(groups[2]), month, int(groups[0])) if month else None
            elif kind == "mdy_text":
                month = names.get(groups[0].lower().strip("."))
                value = _make(_year(groups[2]), month, int(groups[1])) if month else None
            elif kind == "era":
                base = {"令和": 2018, "r": 2018, "平成": 1988, "h": 1988, "昭和": 1925, "s": 1925}[groups[0]]
                value = _make(base + int(groups[1]), int(groups[2]), int(groups[3]))
            elif kind == "dm_text" and year is not None:
                month = names.get(groups[1].lower().strip("."))
                value = _make(year, month, int(groups[0])) if month else None
            if value is not None:
                found.append((value, match.start(), match.end()))

    if allow_compact:
        for match in _COMPACT.finditer(folded_text):
            value = _make(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            if value is not None:
                found.append((value, match.start(), match.end()))

    if year is not None:
        for match in _NO_YEAR.finditer(folded_text):
            a, b = int(match.group(1)), int(match.group(2))
            first, second = ((b, a), (a, b)) if order == "dmy" else ((a, b), (b, a))
            value = _make(year, *first) or _make(year, *second)
            if value is not None:
                found.append((value, match.start(), match.end()))

    # A longer match beats any shorter one it overlaps.
    found.sort(key=lambda item: (item[1], -(item[2] - item[1])))
    result: list[tuple[date, int, int]] = []
    for item in found:
        if result and item[1] < result[-1][2]:
            continue
        result.append(item)
    return result


def parse_date(text: str, order: str = "dmy", year: int | None = None, whole: bool = False) -> date | None:
    """First date in `text`; with `whole`, the date must be (nearly) all of it."""
    found = find_dates(text, order, year, allow_compact=whole)
    if not found:
        return None
    value, start, end = found[0]
    if whole and len(text.strip()) - (end - start) > 3:
        return None
    return value


def looks_like_date(text: str, order: str = "dmy") -> bool:
    """True when the text *starts with* something date-shaped (year required)."""
    stripped = text.strip()
    found = find_dates(stripped, order)
    return bool(found) and found[0][1] <= 1


def is_date_token(text: str, order: str = "dmy") -> bool:
    """The whole token is a full date: `14.09.26`, `31/07/2026`, `11-Feb-26`."""
    stripped = text.strip().rstrip(",;")
    found = find_dates(stripped, order)
    return bool(found) and found[0][1] == 0 and found[0][2] >= len(stripped)


_NUM_DATE = re.compile(r"(?<!\d)(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2}|(?:19|20)\d{2})(?!\d)")
#: "03/14" - only with slashes, since "12.50" is an amount.
_NUM_NO_YEAR = re.compile(r"(?<![\d/.\-])(\d{1,2})/(\d{1,2})(?![\d/.\-])")


def infer_order(samples: list[str]) -> str:
    """Day-first or month-first, decided by the data.

    Every value with a part above 12 is a vote (31/07 can only be day-first).
    When every value is ambiguous (03/04, 05/06 ...) statements run in date
    order, so the reading with fewer backward steps wins.
    """
    pairs: list[tuple[int, int]] = []
    day_votes = month_votes = 0
    for text in samples:
        match = _NUM_DATE.search(text) or _NUM_NO_YEAR.search(text)
        if not match:
            continue
        a, b = int(match.group(1)), int(match.group(2))
        if a > 12 and b <= 12:
            day_votes += 1
        elif b > 12 and a <= 12:
            month_votes += 1
        elif a <= 12 and b <= 12:
            pairs.append((a, b))
    if day_votes != month_votes:
        return "dmy" if day_votes > month_votes else "mdy"
    if len(pairs) < 4:
        return "dmy"

    def backward_steps(key) -> int:
        seq = [key(a, b) for a, b in pairs]
        return sum(1 for x, y in zip(seq, seq[1:]) if y < x)

    day_first = backward_steps(lambda a, b: (b, a))
    month_first = backward_steps(lambda a, b: (a, b))
    return "mdy" if month_first < day_first else "dmy"
