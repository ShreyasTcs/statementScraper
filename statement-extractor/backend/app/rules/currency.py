"""Currency detection, backed by CLDR (Babel) rather than a hand-kept list.

Evidence is ranked, because a single glyph is rarely enough:

    adjacent ISO code ("NOK 123,456.78")  >  unambiguous symbol / name
    >  column heading ("Amount (AED)")      >  IBAN country (fallback)

The file name is never evidence. `€` often extracts as `�` or `¤` when a bank
embeds the glyph oddly, so unreadable symbols are treated as "unknown symbol",
not as a currency.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from babel import Locale
from babel.numbers import get_currency_precision, get_territory_currencies

from app.rules.dates import fold


_NOT_MONEY = frozenset({"XXX", "XTS", "XAU", "XAG", "XPT", "XPD", "XDR", "XBA", "XBB", "XBC", "XBD", "XSU", "XUA", "XFU", "XFO"})


@lru_cache(maxsize=1)
def iso_codes() -> frozenset[str]:
    return frozenset(c for c in Locale.parse("en").currencies if len(c) == 3 and c.isalpha() and c not in _NOT_MONEY)


#: Codes that are also ordinary uppercase words in statements. They count only
#: when glued to an amount, never from a bare word.
_WORD_LIKE = frozenset({"ALL", "TRY", "MAD", "PEN", "TOP", "AND", "CAD", "BAT", "PAB", "MOP", "ANG", "COP"})

#: Symbols with one clear meaning. `$`, `kr`, `Rs` are ambiguous and returned
#: with low confidence.
UNAMBIGUOUS_SYMBOLS: dict[str, str] = {
    "₹": "INR", "€": "EUR", "£": "GBP", "₩": "KRW", "₽": "RUB",
    "₺": "TRY", "₫": "VND", "₪": "ILS", "฿": "THB", "zł": "PLN",
    "r$": "BRL", "us$": "USD", "a$": "AUD", "c$": "CAD", "s$": "SGD", "hk$": "HKD", "nz$": "NZD",
    "₱": "PHP", "₴": "UAH", "kc": "CZK", "kč": "CZK", "ft": "HUF",
}
AMBIGUOUS_SYMBOLS: dict[str, tuple[str, ...]] = {
    "$": ("USD", "CAD", "AUD", "SGD", "NZD", "MXN", "HKD"),
    "¥": ("JPY", "CNY"),
    "kr": ("NOK", "SEK", "DKK", "ISK"),
    "rs": ("INR", "PKR", "LKR", "NPR"),
    "rs.": ("INR", "PKR", "LKR", "NPR"),
}
#: Glyphs that mean "a currency symbol was here" but not which one.
UNREADABLE_SYMBOLS = ("¤", "�")

#: Currency named in words, in several languages. Matched on folded text.
_WORDS: tuple[tuple[str, str], ...] = (
    (r"人民币|人民幣|元(?!旦)", "CNY"),
    (r"円|日本円", "JPY"),
    (r"원(?!행)|대한민국", "KRW"),
    (r"руб(?:\.|лей|ль|\b)", "RUB"),
    (r"грн|гривн", "UAH"),
    (r"ש\"ח|שקל", "ILS"),
    (r"บาท", "THB"),
    (r"درهم إماراتي|الدرهم الإماراتي", "AED"),
    (r"دينار كويتي", "KWD"),
    (r"ريال سعودي", "SAR"),
    (r"\blei\b", "RON"),
    (r"лв\b", "BGN"),
    (r"dollaro\s+(?:usa|us|americano|statunitense)|dollari\s+(?:usa|us)|us\s*dollars?|united states dollars?|dolar(?:es)?\s+(?:usa|us|estadounidense|americano)|dollars?\s+(?:us|usa|americains?)|us[- ]dollar", "USD"),
    (r"\beuros?\b|\beuro\b", "EUR"),
    (r"pound(?:s)?\s+sterling|sterlina|livres?\s+sterling|pfund\s+sterling|libras?\s+esterlinas?|british pounds?", "GBP"),
    (r"indian rupees?|rupie?\s+indian[ae]|roupies?\s+indiennes?", "INR"),
    (r"emirati dirhams?|uae dirhams?|dirham\w*\s+(?:emirat\w+|degli emirati)|aed dirham", "AED"),
    (r"saudi riyals?|riyals?\s+saudi\w*", "SAR"),
    (r"swiss francs?|franchi svizzeri|franco svizzero|schweizer franken|francs? suisses?", "CHF"),
    (r"yen\b|japanese yen", "JPY"),
    (r"yuan\b|renminbi", "CNY"),
    (r"norwegian kron\w+|norske kroner|norwegische kron\w+|corone norvegesi|couronnes? norv\w+", "NOK"),
    (r"swedish kron\w+|svenska kronor|schwedische kron\w+|corone svedesi", "SEK"),
    (r"danish kron\w+|danske kroner|dänische kron\w+|corone danesi", "DKK"),
    (r"zloty|złoty|zloti", "PLN"),
    (r"australian dollars?|dollari australiani", "AUD"),
    (r"canadian dollars?|dollari canadesi|dollars? canadiens?", "CAD"),
    (r"singapore dollars?|dollari di singapore", "SGD"),
    (r"kuwaiti dinars?|dinaro kuwaitiano", "KWD"),
    (r"bahraini dinars?", "BHD"),
    (r"omani rials?|rial omanita", "OMR"),
    (r"qatari rials?|riyals? qatari", "QAR"),
    (r"south african rands?", "ZAR"),
    (r"brazilian reais|reais\b|real brasileiro", "BRL"),
    (r"mexican pesos?|pesos? mexicanos?", "MXN"),
    (r"turkish lira|lira turca|türk lirası", "TRY"),
)


@dataclass(frozen=True)
class CurrencyEvidence:
    code: str
    confidence: float
    reason: str


def _fold(text: str) -> str:
    # One character in, one out: Hangul and CJK stay whole instead of decomposing.
    return fold(text)


_ADJACENT = re.compile(r"(?<![A-Za-z])([A-Z]{3})(?![A-Za-z])")
_AMOUNTISH = re.compile(r"[-+(]?\d[\d.,'   ]*\d|\d")


def adjacent_code(text: str) -> CurrencyEvidence | None:
    """An ISO code touching a number: `NOK 123,456.78`, `1.234,56 EUR`."""
    codes = iso_codes()
    for match in _ADJACENT.finditer(text):
        code = match.group(1)
        if code not in codes:
            continue
        before = text[: match.start()].rstrip()
        after = text[match.end():].lstrip()
        glued = bool(re.search(r"\d[-+]?$", before)) or bool(re.match(r"[-+(]?\d", after))
        if glued:
            return CurrencyEvidence(code, 0.95, "code next to an amount")
    return None


def heading_code(text: str) -> CurrencyEvidence | None:
    """`Amount (AED)`, `Currency: EUR`, `Valuta EUR` - a code in a labelled spot."""
    codes = iso_codes()
    lowered = _fold(text)
    labelled = re.search(r"currenc|divisa|valuta|waehrung|wahrung|devise|moneda|moeda|valuuta|valuta|ccy|amount|importo|betrag|montant|importe|belop|balance|saldo", lowered)
    paren = re.search(r"[(\[]\s*([A-Z]{3})\s*[)\]]", text)
    if paren and paren.group(1) in codes:
        return CurrencyEvidence(paren.group(1), 0.85, "code in a column heading")
    if labelled:
        for match in _ADJACENT.finditer(text):
            code = match.group(1)
            if code in codes and code not in _WORD_LIKE:
                return CurrencyEvidence(code, 0.8, "code beside a currency label")
    return None


def symbol_currency(text: str) -> CurrencyEvidence | None:
    lowered = text.lower()
    # A bare "$" or "\u00a5" is ambiguous; take the most common reading, weakly.
    weak = None
    if "$" in text and not re.search(r"(?i)(?:us|a|c|s|hk|nz)\$", text):
        weak = CurrencyEvidence("USD", 0.5, "dollar sign")
    elif "\u00a5" in text:
        weak = CurrencyEvidence("JPY", 0.4, "yen sign")
    elif re.search(r"(?i)(?<![a-z])rs\.?\s*\d", text):
        weak = CurrencyEvidence("INR", 0.45, "Rs")
    for symbol, code in UNAMBIGUOUS_SYMBOLS.items():
        if symbol.isascii() and symbol[0].isalpha():
            if re.search(rf"(?<![a-z]){re.escape(symbol)}(?=\s*\d)", lowered):
                return CurrencyEvidence(code, 0.8, f"symbol {symbol}")
        elif symbol in lowered:
            return CurrencyEvidence(code, 0.8, f"symbol {symbol}")
    return weak


def word_currency(text: str) -> CurrencyEvidence | None:
    folded = _fold(text)
    for pattern, code in _WORDS:
        if re.search(pattern, folded):
            return CurrencyEvidence(code, 0.8, "currency named in words")
    return None


def has_unreadable_symbol(text: str) -> bool:
    return any(glyph in text for glyph in UNREADABLE_SYMBOLS)


def detect(text: str) -> CurrencyEvidence | None:
    """Best single currency claim made by one piece of text, or None."""
    for finder in (adjacent_code, symbol_currency, word_currency, heading_code):
        found = finder(text)
        if found:
            return found
    return None


def territory_currency(country: str) -> str | None:
    """Current legal tender of an ISO country code (from an IBAN prefix)."""
    try:
        codes = get_territory_currencies(country.upper())
    except Exception:
        return None
    return codes[0] if codes else None


def precision(code: str | None) -> int:
    """Minor-unit digits: 2 for most, 0 for JPY/KRW, 3 for KWD/BHD/OMR."""
    if not code:
        return 2
    try:
        return int(get_currency_precision(code))
    except Exception:
        return 2
