"""Statement-level values: holder, account numbers, balances, period, currency.

Three ways a value is found, tried in this order of trust:

    labelled     a lexicon label ("Saldo iniziale", "Closing balance last
                 statement") and its value in the same text, to the right, or below
    pattern      shape alone - a checksum-valid IBAN, a date range, a BIC
    structural   position and wording - the postal address block (holder),
                 the letterhead / footer (bank), the IBAN's country (currency)

Anything not found stays absent; nothing is guessed.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.core.logging import get_logger
from app.rules import currency as currency_rules
from app.rules import lexicon
from app.rules.dates import find_dates
from app.rules.iban import find_bic, find_ibans
from app.rules.numbers import looks_like_money, parse_amount
from app.rules.statement import HEADER_FIELDS, HeaderField
from app.services.document_model import Block, Page, StructuredDocument
from app.services.table_service import Conventions, TableResult

logger = get_logger(__name__)

#: word -> which end of a period it introduces (many languages, whole-word).
_PERIOD_FROM = ("from", "dal", "von", "vom", "du", "desde", "van", "fra", "fran", "alkaen", "od", "da", "de")
_PERIOD_TO = ("to", "till", "until", "al", "bis", "au", "hasta", "tot", "til", "asti", "do", "a", "fino al")
_RANGE_CONNECTOR = re.compile(
    r"^\s*(?:-|–|—|\.\.\.|→|to|till|until|al|a|bis|au|hasta|ate|tot|til|asti|do|fino al|and)\s*$", re.I
)
_ACCOUNT_RE = re.compile(r"(?<![\w])\d[\d .\-/\u00ad\u2010-\u2015]{3,26}\d(?![\w])")
_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9][A-Za-z0-9\-/]{0,24}(?![A-Za-z0-9])")
_LEGAL_CUT = re.compile(r"(?:\.?\s*con sede|,| - | domicile| with registered| s\.?a\.?\s+with|\s{2,})", re.I)


@dataclass
class Candidate:
    value: str
    normalized: str
    page_number: int
    bbox: tuple[float, float, float, float]
    source_text: str
    anchor: str | None
    strategy: str
    signals: dict[str, float] = field(default_factory=dict)
    score: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def as_evidence(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "page": self.page_number,
            "bbox": list(self.bbox),
            "text": self.source_text,
            "anchor": self.anchor,
            "strategy": self.strategy,
        }


@dataclass
class FieldExtraction:
    spec: HeaderField
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def is_ambiguous(self) -> bool:
        if len(self.candidates) < 2:
            return False
        first, second = self.candidates[0], self.candidates[1]
        return first.normalized != second.normalized and (first.score - second.score) < 0.12


def _source_quality(block: Block) -> float:
    return 1.0 if block.source == "pdf_text" else round(float(block.confidence or 0.6), 3)


def _page_pref(spec: HeaderField, page_number: int, page_count: int) -> float:
    if spec.page_preference == "last":
        return 1.0 if page_number == page_count else (0.85 if page_number == 1 else 0.7)
    if spec.page_preference == "first":
        return 1.0 if page_number == 1 else 0.7
    return 1.0


class HeaderExtractor:
    def __init__(self) -> None:
        self._by_concept: dict[str, list[HeaderField]] = defaultdict(list)
        for spec in HEADER_FIELDS:
            for concept in spec.concepts:
                self._by_concept[concept].append(spec)

    # ------------------------------------------------------------------ API

    def extract(
        self,
        doc: StructuredDocument,
        claimed: set[str],
        conv: Conventions,
        table: TableResult | None = None,
    ) -> dict[str, FieldExtraction]:
        results = {spec.name: FieldExtraction(spec) for spec in HEADER_FIELDS}
        page_count = len(doc.pages)
        ocr = conv.ocr

        for page in doc.pages:
            free = [b for b in page.blocks if b.id not in claimed]
            for block in free:
                self._labelled(block, page, free, results, conv, page_count)
            self._ibans(page, free, results)
            self._bics(page, free, results)
            self._periods(page, free, results, conv, page_count)

        self._account_under_type(doc, claimed, results, conv)
        self._holder(doc, claimed, results)
        self._bank(doc, claimed, results)
        self._currency(doc, results, ocr)
        self._derive_period(results, table)
        self._finalize(results)
        return results

    # --------------------------------------------------------------- labels

    def _labelled(self, block, page, free, results, conv, page_count) -> None:
        hits = [h for h in lexicon.find("field", block.text, ocr=conv.ocr) if h.concept in self._by_concept]
        for index, hit in enumerate(hits):
            next_start = hits[index + 1].char_start if index + 1 < len(hits) else len(block.text)
            inline = block.text[hit.char_end:next_start]
            label_text = block.text[hit.char_start:hit.char_end]
            prior_end = hits[index - 1].char_end if index else 0
            before = re.sub(r"[\W_]+", "", block.text[prior_end:hit.char_start]) if index else re.sub(r"[\W_]+", "", block.text[:hit.char_start])
            if len(before) > 3 and not (index and re.fullmatch(r"[\d\W_a-z]*", before.lower()) and len(before) <= 24) and not re.fullmatch(r"[\d\W_]*", before):
                continue  # a label sits at the start of its text; this is mid-sentence
            for spec in self._by_concept[hit.concept]:
                if spec.value_type == "date" and hit.concept == "period":
                    continue  # ranges are handled by _periods
                if spec.page_preference == "first" and page.page_number > 2 and spec.value_type in ("text", "id"):
                    continue  # boilerplate on later pages is not a header detail
                found = self._value_for(spec, inline, block, page, free, conv)
                if found is None:
                    continue
                text, strategy, source_block, proximity = found
                parsed = self._validate(spec, text, conv, hit.phrase)
                if parsed is None:
                    continue
                display, normalized = parsed
                ntokens = len(hit.phrase.split())
                candidate = Candidate(
                    value=display,
                    normalized=normalized,
                    page_number=page.page_number,
                    bbox=source_block.bbox,
                    source_text=source_block.text,
                    anchor=hit.phrase,
                    strategy=strategy,
                    signals={
                        "anchor_weight": round(min(1.0, 0.7 + 0.1 * ntokens), 3),
                        "spatial_proximity": round(proximity, 3),
                        "page_preference": _page_pref(spec, page.page_number, page_count),
                        "source_quality": _source_quality(block),
                    },
                )
                # A label may carry its own date: "Closing balance last statement 31.08.2026".
                label_dates = find_dates(block.text[hit.char_start:next_start], conv.order, conv.year)
                if label_dates and spec.value_type == "money":
                    candidate.meta["as_of"] = label_dates[0][0].isoformat()
                if spec.value_type == "money" and not candidate.meta.get("as_of"):
                    near = find_dates(label_text + " " + inline, conv.order, conv.year)
                    if near:
                        candidate.meta["as_of"] = near[0][0].isoformat()
                candidate.score = _rank(candidate)
                results[spec.name].candidates.append(candidate)

    def _value_for(self, spec, inline, block, page, free, conv):
        """The text that should hold the value: inline, else right, else below."""
        cleaned = inline.strip(" :-=–—.#\t")
        if cleaned and self._validate(spec, cleaned, conv, None) is not None:
            return cleaned, "inline", block, 1.0
        for neighbour, proximity, strategy in _neighbours(block, page, free):
            # Names and ids sit next to their label; only amounts are pushed to the far edge.
            if spec.value_type == "text" and strategy in ("right", "left") and proximity < 0.72:
                continue
            if spec.value_type == "text" and lexicon.looks_like_company(neighbour.text) and spec.name != "account_holder":
                continue
            if self._validate(spec, neighbour.text, conv, None) is not None:
                return neighbour.text, strategy, neighbour, proximity
        return None

    # ----------------------------------------------------------- validation

    def _validate(self, spec: HeaderField, text: str, conv: Conventions, anchor: str | None) -> tuple[str, str] | None:
        text = re.sub(r"\s+", " ", text).strip(" :-=–—.#")
        if not text:
            return None
        kind = spec.value_type

        if kind == "money":
            for word_count in range(min(4, len(text.split())), 0, -1):
                tail = " ".join(text.split()[-word_count:])
                parsed = parse_amount(tail, conv.number)
                if parsed is not None and looks_like_money(tail, conv.number, True):
                    number = parsed.value if spec.name in ("total_debits", "total_credits") else parsed.signed
                    return tail, f"{number:.2f}" if parsed.decimals <= 2 else f"{number}"
            return None

        if kind == "date":
            dates = find_dates(text, conv.order, conv.year)
            if not dates:
                return None
            value, start, end = dates[0]
            return text[start:end], value.isoformat()

        if kind == "iban":
            found = find_ibans(text)
            return (text, found[0].value) if found else None

        if kind == "bic":
            code = find_bic(re.sub(r"(?i)^(bic|swift)\W*", "", text))
            return (code, code) if code else None

        if kind == "account_number":
            match = _ACCOUNT_RE.search(text)
            if match:
                compact = re.sub(r"[ .\-/\u00ad\u2010-\u2015]", "", match.group(0))
                if 6 <= len(compact) <= 24:
                    return match.group(0).strip(), compact
            return None

        if kind == "routing":
            match = re.search(r"(?<![A-Za-z0-9])[A-Za-z]{4}0[A-Za-z0-9]{6}(?![A-Za-z0-9])|(?<!\d)\d{2}-\d{2}-\d{2}(?!\d)|(?<!\d)\d{5,11}(?!\d)", text)
            return (match.group(0).upper(), match.group(0).upper()) if match else None

        if kind == "id":
            match = _ID_RE.search(text)
            if match and any(ch.isdigit() for ch in match.group(0)) and not find_dates(match.group(0), conv.order):
                return match.group(0), match.group(0)
            return None

        if kind == "currency":
            evidence = currency_rules.detect(text)
            if evidence is None and re.fullmatch(r"[A-Z]{3}", text) and text in currency_rules.iso_codes():
                return text, text
            return (evidence.code, evidence.code) if evidence else None

        if kind == "text":
            letters = sum(ch.isalpha() for ch in text)
            if letters < 2 or letters / len(text) < 0.5 or len(text) > 90:
                return None
            hits = lexicon.find("field", text)
            if hits and sum(h.char_end - h.char_start for h in hits) >= 0.6 * len(text):
                return None  # the "value" is itself a label (e.g. IBAN)
            return text, text
        return None

    # -------------------------------------------------------------- patterns

    def _ibans(self, page: Page, free: list[Block], results) -> None:
        for block in free:
            for found in find_ibans(block.text):
                candidate = Candidate(
                    value=found.value, normalized=found.value, page_number=page.page_number,
                    bbox=block.bbox, source_text=block.text, anchor=None, strategy="pattern",
                    signals={"anchor_weight": 0.95, "spatial_proximity": 1.0, "page_preference": 1.0 if page.page_number == 1 else 0.8,
                             "source_quality": _source_quality(block)},
                )
                candidate.score = _rank(candidate)
                candidate.meta["country"] = found.country
                results["iban"].candidates.append(candidate)

    def _bics(self, page: Page, free: list[Block], results) -> None:
        pattern = re.compile(r"(?:BIC|SWIFT)(?:\s*(?:CODE|ADDRESS))?\W*([A-Z]{4}[A-Z]{2}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)", re.I)
        for block in free:
            for match in pattern.finditer(block.text.upper()):
                code = find_bic(match.group(1))
                if not code:
                    continue
                candidate = Candidate(
                    value=code, normalized=code, page_number=page.page_number, bbox=block.bbox,
                    source_text=block.text, anchor="bic", strategy="inline",
                    signals={"anchor_weight": 0.9, "spatial_proximity": 1.0, "page_preference": 1.0 if page.page_number == 1 else 0.8,
                             "source_quality": _source_quality(block)},
                )
                candidate.score = _rank(candidate)
                results["bic"].candidates.append(candidate)

    def _periods(self, page: Page, free: list[Block], results, conv: Conventions, page_count: int) -> None:
        for block in free:
            text = block.text
            dates = find_dates(text, conv.order, conv.year)
            labelled = any(h.concept == "period" for h in lexicon.find("field", text, ocr=conv.ocr))
            if len(dates) >= 2:
                a, b = dates[0], dates[1]
                if _RANGE_CONNECTOR.match(text[a[2]:b[1]]) or labelled:
                    span = (b[0] - a[0]).days
                    if 0 < span <= 400:
                        weight = 0.92 if labelled else 0.6
                        self._period_candidates(results, page, block, a[0], b[0], weight, text[a[1]:b[2]], conv)
                        continue
            # Label in one cell, dates in the next: "Period" | "01/02/2025 - 28/02/2025",
            # "From:" | "01-Feb-2025".
            stripped_label = re.sub(r"[\W_]+", " ", lexicon.fold(text)).strip()
            if not dates and stripped_label:
                is_period_label = bool(lexicon.find("field", text)) and any(
                    h.concept == "period" for h in lexicon.find("field", text, ocr=conv.ocr)
                )
                end_for = "period_from" if stripped_label in _PERIOD_FROM else "period_to" if stripped_label in _PERIOD_TO else None
                if is_period_label or end_for:
                    for neighbour, proximity, _ in _neighbours(block, page, free):
                        ndates = find_dates(neighbour.text, conv.order, conv.year)
                        if is_period_label and len(ndates) >= 2:
                            self._period_candidates(results, page, neighbour, ndates[0][0], ndates[1][0], 0.9, neighbour.text, conv)
                            break
                        if end_for and len(ndates) == 1:
                            spec = results[end_for].spec
                            candidate = Candidate(
                                value=neighbour.text[ndates[0][1]:ndates[0][2]], normalized=ndates[0][0].isoformat(),
                                page_number=page.page_number, bbox=neighbour.bbox, source_text=neighbour.text,
                                anchor=stripped_label, strategy="right",
                                signals={"anchor_weight": 0.7, "spatial_proximity": proximity,
                                         "page_preference": _page_pref(spec, page.page_number, page_count),
                                         "source_quality": _source_quality(neighbour)},
                            )
                            candidate.score = _rank(candidate)
                            results[end_for].candidates.append(candidate)
                            break
            # "From: 01-Feb-2025" / "To: 28-Feb-2025" in one cell
            folded = lexicon.fold(text).strip()
            for end, words in (("period_from", _PERIOD_FROM), ("period_to", _PERIOD_TO)):
                for word in words:
                    if re.match(rf"^{re.escape(word)}\W", folded + " ") and len(dates) == 1 and dates[0][1] <= len(word) + 3:
                        spec = results[end].spec
                        candidate = Candidate(
                            value=text[dates[0][1]:dates[0][2]], normalized=dates[0][0].isoformat(),
                            page_number=page.page_number, bbox=block.bbox, source_text=text, anchor=word,
                            strategy="inline",
                            signals={"anchor_weight": 0.7, "spatial_proximity": 1.0,
                                     "page_preference": _page_pref(spec, page.page_number, page_count),
                                     "source_quality": _source_quality(block)},
                        )
                        candidate.score = _rank(candidate)
                        results[end].candidates.append(candidate)
                        break

    @staticmethod
    def _period_candidates(results, page, block, start: date, end: date, weight: float, shown: str, conv) -> None:
        start, end = min(start, end), max(start, end)
        for name, value in (("period_from", start), ("period_to", end)):
            candidate = Candidate(
                value=value.isoformat(), normalized=value.isoformat(), page_number=page.page_number,
                bbox=block.bbox, source_text=block.text, anchor="period" if weight > 0.8 else None,
                strategy="range",
                signals={"anchor_weight": weight, "spatial_proximity": 1.0, "page_preference": 1.0 if page.page_number == 1 else 0.8,
                         "source_quality": _source_quality(block)},
            )
            candidate.score = _rank(candidate)
            results[name].candidates.append(candidate)

    # ----------------------------------------------------------- structural

    _ACCOUNT_TYPES = re.compile(
        r"(?i)^(?:current|checking|savings?|deposit|currency|corporate|business|salary|call|time)\s+account$|^conto\s+\w+$|"
        r"^(?:giro|spar|gehalts|geschafts)konto$|^compte\s+\w+$|^cuenta\s+\w+$"
    )

    def _account_under_type(self, doc, claimed, results, conv) -> None:
        """"Current account" with the number on the line below (Nordea, DNB layouts)."""
        spec = results["account_number"].spec
        for page in doc.pages[:1]:
            free = [b for b in page.blocks if b.id not in claimed]
            for block in free:
                if not self._ACCOUNT_TYPES.match(re.sub(r"\s+", " ", block.text).strip()):
                    continue
                if not results["account_type"].candidates:
                    cand = Candidate(
                        block.text.strip(), block.text.strip(), page.page_number, block.bbox, block.text, None, "pattern",
                        {"anchor_weight": 0.8, "spatial_proximity": 1.0, "page_preference": 1.0,
                         "source_quality": _source_quality(block)},
                    )
                    cand.score = _rank(cand)
                    results["account_type"].candidates.append(cand)
                for neighbour, proximity, strategy in _neighbours(block, page, free):
                    parsed = self._validate(spec, neighbour.text, conv, None)
                    if parsed and strategy == "below":
                        cand = Candidate(
                            parsed[0], parsed[1], page.page_number, neighbour.bbox, neighbour.text, "account type", "below",
                            {"anchor_weight": 0.62, "spatial_proximity": proximity, "page_preference": 1.0,
                             "source_quality": _source_quality(neighbour)},
                        )
                        cand.score = _rank(cand)
                        results["account_number"].candidates.append(cand)
                        break

    def _holder(self, doc: StructuredDocument, claimed: set[str], results) -> None:
        """The account holder is rarely labelled: it is the postal address block."""
        spec = results["account_holder"].spec
        page = doc.pages[0] if doc.pages else None
        if page is None:
            return
        zone = page.height * 0.45
        free = sorted((b for b in page.blocks if b.id not in claimed and b.y0 <= zone), key=lambda b: (b.y0, b.x0))
        best: Candidate | None = None
        for index, block in enumerate(free):
            text = re.sub(r"\s+", " ", block.text).strip()
            if not 5 <= len(text) <= 80 or lexicon.BANK_WORD_RE.search(text) or any(ch.isdigit() for ch in text[:3]):
                continue
            if lexicon.find("field", text) or lexicon.find("column", text):
                continue
            letters = sum(ch.isalpha() for ch in text)
            if letters / len(text) < 0.7:
                continue
            company = lexicon.looks_like_company(text)
            squashed_company = False
            nxt = free[index + 1].text if index + 1 < len(free) else ""
            looks_addressed = bool(re.search(r"\d", nxt)) or index == 0
            if not (company or squashed_company or (text.isupper() and 2 <= len(text.split()) <= 6 and looks_addressed)):
                continue
            weight = 0.9 if company else 0.55
            candidate = Candidate(
                value=text, normalized=text, page_number=1, bbox=block.bbox, source_text=block.text,
                anchor=None, strategy="address_block",
                signals={"anchor_weight": weight, "spatial_proximity": 1.0, "page_preference": 1.0,
                         "source_quality": _source_quality(block)},
            )
            candidate.score = _rank(candidate) + (0.15 if (company or squashed_company) else 0.0) - 0.004 * index
            if best is None or candidate.score > best.score:
                best = candidate
        if best is not None:
            results[spec.name].candidates.append(best)

    def _bank(self, doc: StructuredDocument, claimed: set[str], results) -> None:
        """Bank name from the letterhead / legal footer; domain as a weak fallback."""
        spec = results["bank_name"].spec
        found: list[Candidate] = []
        for page in doc.pages:
            for block in page.blocks:
                text = re.sub(r"\s+", " ", block.text).strip()
                if not lexicon.BANK_WORD_RE.search(text):
                    continue
                if lexicon.find("field", text) or lexicon.find("row", text) or re.match(r"(?i)bank statement|bank details", text):
                    continue
                name = _LEGAL_CUT.split(text)[0].strip(" .,:;-")
                name = re.sub(r"(?i)\s+(?:con sede|domicile|registered).*$", "", name)
                name = re.sub(r"(?i)(S\.?p\.?A)\.?con.*$", r"\1.", name)
                if not 3 <= len(name) <= 60 or sum(ch.isdigit() for ch in name) > 2 or len(name.split()) > 5:
                    continue
                if re.search(r"[@:/]|www\.|\.(?:com|it|no|de|fr|es|nl|fi|se|dk)\b", name, re.I):
                    continue  # an address or contact line, not a name
                if not lexicon.BANK_WORD_RE.search(name):
                    continue
                top = block.y0 <= page.height * 0.2
                bottom = block.y0 >= page.height * 0.9
                weight = 0.55 + 0.25 * (top or bottom) + 0.15 * bool(lexicon.COMPANY_RE.search(name))
                candidate = Candidate(
                    value=name, normalized=name, page_number=page.page_number, bbox=block.bbox,
                    source_text=block.text, anchor=None, strategy="letterhead",
                    signals={"anchor_weight": round(min(weight, 0.95), 3), "spatial_proximity": 1.0,
                             "page_preference": 1.0 if page.page_number == 1 else 0.85,
                             "source_quality": _source_quality(block)},
                )
                candidate.score = _rank(candidate)
                found.append(candidate)
        if not found:
            for page in doc.pages[:1]:
                for block in page.blocks:
                    match = re.search(r"(?<![\w.])(?:www\.)?([a-z0-9\-]{2,20})\.(?:no|se|dk|fi|de|fr|it|es|nl|ch|at|com|co\.uk|ae|in|sg)\b", block.text.lower())
                    if match and "bank" not in match.group(1) and (block.y0 < page.height * 0.2 or block.y0 > page.height * 0.8):
                        name = match.group(1).upper()
                        candidate = Candidate(
                            value=name, normalized=name, page_number=1, bbox=block.bbox, source_text=block.text,
                            anchor=None, strategy="domain",
                            signals={"anchor_weight": 0.45, "spatial_proximity": 1.0, "page_preference": 1.0,
                                     "source_quality": _source_quality(block)},
                        )
                        candidate.score = _rank(candidate)
                        found.append(candidate)
                        break
        results[spec.name].candidates.extend(found)

    def _currency(self, doc: StructuredDocument, results, ocr: bool) -> None:
        """Weigh every piece of currency evidence; the file name is never one."""
        votes: dict[str, float] = defaultdict(float)
        first: dict[str, tuple[Page, Block, currency_rules.CurrencyEvidence]] = {}
        for page in doc.pages:
            for block in page.blocks:
                evidence = currency_rules.detect(block.text)
                if evidence:
                    prose = len(block.text.split()) > 8
                    votes[evidence.code] += evidence.confidence * (0.3 if prose else 1.0)
                    first.setdefault(evidence.code, (page, block, evidence))
        # An amount's own currency is the best evidence there is: the balances.
        for name in ("opening_balance", "closing_balance", "total_debits", "total_credits"):
            best = results[name].best
            if best is not None:
                evidence = currency_rules.adjacent_code(best.source_text) or currency_rules.symbol_currency(best.source_text)
                if evidence:
                    votes[evidence.code] += 1.0
                    for p in doc.pages:
                        if p.page_number == best.page_number:
                            first.setdefault(evidence.code, (p, Block("bal", best.source_text, best.bbox, "pdf_text"), evidence))
        ibans = results["iban"].candidates
        if ibans:
            code = currency_rules.territory_currency(ibans[0].meta.get("country", ibans[0].normalized[:2]))
            if code:
                votes[code] += 0.55
                page = doc.pages[0]
                first.setdefault(code, (page, Block("iban", ibans[0].source_text, ibans[0].bbox, "pdf_text"),
                                        currency_rules.CurrencyEvidence(code, 0.55, "IBAN country")))
        if not votes:
            return
        spec = results["currency"].spec
        for code, total in sorted(votes.items(), key=lambda kv: kv[1], reverse=True)[:3]:
            page, block, evidence = first[code]
            candidate = Candidate(
                value=code, normalized=code, page_number=page.page_number, bbox=block.bbox,
                source_text=block.text, anchor=None, strategy="evidence",
                signals={"anchor_weight": round(min(0.95, 0.4 + 0.15 * min(total, 3)), 3), "spatial_proximity": 0.8,
                         "page_preference": 1.0, "source_quality": _source_quality(block)},
                meta={"reason": evidence.reason, "votes": round(total, 2)},
            )
            candidate.score = _rank(candidate)
            results[spec.name].candidates.append(candidate)

    @staticmethod
    def _derive_period(results, table: TableResult | None = None) -> None:
        """No printed period: the day after the opening balance's date up to the closing date."""
        if results["period_from"].candidates and results["period_to"].candidates:
            return
        opening = results["opening_balance"].best
        closing = results["closing_balance"].best
        stmt_date = results["statement_date"].best
        start = opening.meta.get("as_of") if opening else None
        end = (closing.meta.get("as_of") if closing else None) or (stmt_date.normalized if stmt_date else None)
        source = closing or opening or stmt_date
        shift = 1
        if (not start or not end) and table is not None and len(table.balance_rows) >= 2:
            # Balances printed as table rows: "SALDO AL 01/08/26" ... "SALDO AL 31/08/26".
            first, last = table.balance_rows[0], table.balance_rows[-1]
            if first.row_date and last.row_date and first.row_date < last.row_date:
                start = start or first.row_date.isoformat()
                end = end or last.row_date.isoformat()
                shift = 0 if first.kind == "balance" else 1
                if source is None:
                    source = Candidate(first.text, first.text, first.page, first.bbox, first.text, None, "table", {})
        if not start or not end or source is None:
            return
        try:
            begin = date.fromisoformat(start) + timedelta(days=shift)
            finish = date.fromisoformat(end)
        except ValueError:
            return
        if begin > finish:
            return
        for name, value in (("period_from", begin), ("period_to", finish)):
            if results[name].candidates:
                continue
            candidate = Candidate(
                value=value.isoformat(), normalized=value.isoformat(), page_number=source.page_number,
                bbox=source.bbox, source_text=source.source_text, anchor=None, strategy="derived",
                signals={"anchor_weight": 0.5, "spatial_proximity": 0.6, "page_preference": 1.0, "source_quality": 1.0},
            )
            candidate.score = _rank(candidate)
            results[name].candidates.append(candidate)

    @staticmethod
    def _finalize(results) -> None:
        for extraction in results.values():
            best: dict[str, Candidate] = {}
            for candidate in extraction.candidates:
                current = best.get(candidate.normalized)
                if current is None or candidate.score > current.score:
                    best[candidate.normalized] = candidate
            extraction.candidates = sorted(best.values(), key=lambda c: c.score, reverse=True)


# --------------------------------------------------------------------------


_RTL_RE = re.compile("[\u0590-\u05ff\u0600-\u06ff\u0750-\u077f\ufb1d-\ufdff\ufe70-\ufefc]")


def _neighbours(block: Block, page: Page, blocks: list[Block]):
    right: list[tuple[float, Block]] = []
    left: list[tuple[float, Block]] = []
    below: list[tuple[float, Block]] = []
    height = block.height
    for other in blocks:
        if other.id == block.id:
            continue
        overlap = min(block.y1, other.y1) - max(block.y0, other.y0)
        if overlap >= 0.4 * min(height, other.height) and other.x0 >= block.x1 - 3:
            right.append((other.x0 - block.x1, other))
        elif overlap >= 0.4 * min(height, other.height) and other.x1 <= block.x0 + 3:
            left.append((block.x0 - other.x1, other))
        elif (
            other.y0 >= block.y1 - 2
            and other.y0 - block.y1 <= 6 * height
            and (abs(other.x0 - block.x0) <= 30 or abs(other.x1 - block.x1) <= 30)
        ):
            below.append((other.y0 - block.y1, other))
    # In a right-to-left language the value follows its label to the *left*.
    sides = [(left, "left"), (right, "right")] if _RTL_RE.search(block.text) else [(right, "right"), (left, "left")]
    for candidates, name in sides:
        if name == "left" and not _RTL_RE.search(block.text):
            continue
        for gap, other in sorted(candidates, key=lambda t: t[0])[:3]:
            yield other, max(0.2, 1.0 - gap / (0.9 * page.width)), name
    for gap, other in sorted(below, key=lambda t: t[0])[:2]:
        yield other, max(0.2, 1.0 - gap / (4 * height)) * 0.9, "below"


def _rank(candidate: Candidate) -> float:
    s = candidate.signals
    return round(0.55 * s["anchor_weight"] + 0.25 * s["spatial_proximity"] + 0.20 * s["page_preference"], 4)


header_extractor = HeaderExtractor()
