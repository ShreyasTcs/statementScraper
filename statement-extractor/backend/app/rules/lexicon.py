"""Multilingual banking vocabulary.

Every label the extractor recognises lives here as data: one concept, many
phrases across languages. Adding a language or a bank's wording is an edit to a
table, not to code. Phrases are written naturally and folded at load time
(lower-case, accents removed, punctuation -> spaces), so `Libellé`, `LIBELLE`
and `libelle` are one phrase.

Languages with phrases below: English, Italian, German, French, Spanish,
Portuguese, Dutch, Norwegian / Danish / Swedish, Finnish, Polish, Turkish.
Italian, English, Norwegian and Finnish were checked against real statements;
the rest come from published statement conventions and have not been verified
against real documents - treat a miss there as "add the bank's wording here".

Matching is tolerant of OCR: with `ocr=True` it also matches when spaces were
lost (`ACCOUNTCODING`) or one character was misread.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache


def fold(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


#: Scripts written without spaces between words: each character is its own token,
#: so "借方金额" is found by the phrase "借方" wherever it sits.
_CJK = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_TOKEN_RE = re.compile(rf"[{_CJK}]|[^\W_{_CJK}]+")
_HAS_CJK = re.compile(rf"[{_CJK}]")


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    """Lower-case, strip accents and marks; map every folded char to its source index."""
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        for c in unicodedata.normalize("NFKD", ch.lower()):
            if not unicodedata.combining(c):
                out.append(c)
                index.append(i)
    return "".join(out), index


def token_spans(text: str) -> list[tuple[str, int, int]]:
    """(token, start, end) in the *original* text's coordinates."""
    folded, index = _fold_with_map(text)
    return [(m.group(0), index[m.start()], index[m.end() - 1] + 1) for m in _TOKEN_RE.finditer(folded)]


def tokens(text: str) -> list[str]:
    return [t for t, _, _ in token_spans(text)]


# --------------------------------------------------------------------------
# Table columns
# --------------------------------------------------------------------------

COLUMN: dict[str, str] = {
    "date": (
        "date|dates|booking date|transaction date|trans date|date trans|txn date|tran date|posting date|post date|entry date|"
        "date of transaction|data|data operazione|data contabile|data registrazione|data movimento|data mov|data lancamento|"
        "data transacao|buchungstag|buchungsdatum|buchung|belegdatum|datum|boekingsdatum|transactiedatum|datum boeking|"
        "date operation|date comptable|date de l operation|fecha|fecha operacion|fecha movimiento|f operacion|fecha contable|"
        "dato|bokforingsdato|bokforingsdag|posteringsdato|transaktionsdag|bokforingsdatum|transaktionsdatum|"
        "pvm|kirjauspaiva|tapahtumapaiva|data ksiegowania|data transakcji|islem tarihi|tarih"
    ),
    "value_date": (
        "value date|val date|value dt|valuta|data valuta|wertstellung|wert|valutadatum|valeurdatum|date de valeur|date valeur|"
        "fecha valor|f valor|data valor|valuteringsdato|valordato|arvopaiva|valutadag|interest date|rentedato|posted|"
        "date posted|posted date|posting|data efetiva"
    ),
    "description": (
        "description|narration|particulars|details|transaction details|transaction description|remarks|transaction remarks|"
        "narrative|payee payer message|payee|message|text|descrizione|causale|descrizione operazione|dettagli|"
        "verwendungszweck|buchungstext|beschreibung|libelle|detail de l operation|operations|designation|concepto|"
        "descripcion|detalle|descricao|historico|omschrijving|naam omschrijving|mededelingen|beskrivelse|tekst|forklaring|"
        "beskrivning|selite|viesti|opis|tytul|aciklama"
    ),
    "reference": (
        "reference|ref|ref no|cheque no|chq no|chq ref no|cheque ref no|chq ref|instrument no|utr|transaction id|txn id|"
        "archive ref|archiv ref|filing code|riferimento|rif|numero assegno|referenz|belegnummer|kundenreferenz|"
        "numero de cheque|referencia|n documento|nr doc|referentie|kenmerk|referanse|arkivref|arkivreferanse|referens|"
        "viite|tositenumero|numer referencyjny|referans"
    ),
    "debit": (
        "debit|debits|debit amount|withdrawal|withdrawals|withdrawal amt|withdrawal amount|paid out|money out|outgoing|"
        "dr amount|uscite|dare|addebiti|addebito|soll|belastung|lastschrift|ausgang|debito|debe|cargos|cargo|debitos|"
        "saidas|af|uit|uttak|debet|debetbelob|uttag|nosto|veloitus|wyplaty|obciazenia|borc|cikis"
    ),
    "credit": (
        "credit|credits|credit amount|deposit|deposits|deposit amt|deposit amount|paid in|money in|incoming|cr amount|"
        "entrate|avere|accrediti|accredito|haben|gutschrift|eingang|haber|abonos|abono|creditos|credito|entradas|bij|inn|"
        "innskudd|kredit|kreditbelob|insattning|talletus|hyvitys|wplaty|uznania|alacak|giris"
    ),
    "amount": (
        "amount|amt|transaction amount|txn amount|importo|betrag|montant|importe|bedrag|belop|beloeb|belopp|summa|maara|"
        "kwota|tutar|valor"
    ),
    "balance": (
        "balance|running balance|closing balance|available balance|saldo|saldo contabile|saldo progressivo|kontostand|"
        "solde|balans|bal|saldo disponibile|saldo disponible|bakiye"
    ),
    "drcr": "dr cr|cr dr|d c|c d|s h|h s|d h|h d|af bij|bij af|soll haben|type|segno|dc|tipo|art|d k|indicator|dr or cr",
    "ignore": (
        "account coding|coding|kontering|tr no|tr|qty|quantity|rate|exchange rate|kurs|cambio|vat|iva|tax|mwst|fee|"
        "commission|commissione|interest|rente|ranta|branch|filiale|no"
    ),
}

NUMERIC_COLUMNS = ("debit", "credit", "amount", "balance")
TEXT_COLUMNS = ("date", "value_date", "description", "reference", "drcr", "ignore")


# --------------------------------------------------------------------------
# Statement-level fields (label -> value)
# --------------------------------------------------------------------------

FIELD: dict[str, str] = {
    "account_number": (
        "account number|account no|a c no|a c number|acct no|acct number|account|bank statement for|statement of account for|"
        "numero conto|n conto|numero di conto|numero|c c|conto corrente numero|kontonummer|konto nr|kontonr|konto|"
        "numero de compte|n de compte|compte|numero de cuenta|n de cuenta|cuenta|numero da conta|conta|rekeningnummer|"
        "rekening|tilinumero|tili|numer rachunku|rachunek|hesap no"
    ),
    "iban": "iban|international bank account number|iban code|codice iban",
    "bic": "bic|swift|swift code|swift address|bic swift|bic swift address|codice bic|codice swift|swift bic",
    "routing": (
        "ifsc|ifsc code|ifs code|routing|routing number|aba|sort code|bsb|transit|clabe|bank code|abi|cab|bankleitzahl|"
        "blz|code banque|clearing number|clearingnummer"
    ),
    "branch": (
        "branch|branch name|home branch|filiale|agenzia|filial|agence|sucursal|agencia|zweigstelle|geschaftsstelle|kantoor|"
        "avdeling|haara|oddzial|sube"
    ),
    "customer_id": (
        "customer id|cust id|customer number|customer no|cif|cif no|client id|client number|codice cliente|kundennummer|"
        "numero client|numero de cliente|klantnummer|kundenummer|asiakasnumero"
    ),
    "account_type": (
        "account type|type of account|a c type|tipo conto|kontotyp|kontoart|type de compte|tipo de cuenta|tipo de conta"
    ),
    "holder": (
        "account holder|account holder name|account name|customer name|name|a c holder|intestatario|intestato a|titolare|"
        "kontoinhaber|inhaber|titulaire|titular|rekeninghouder|kontohaver|kontoinnehavare|tilinhaltija|"
        "wlasciciel rachunku|hesap sahibi"
    ),
    "statement_number": (
        "statement number|statement no|statement nr|estratto n|numero estratto|auszug nr|kontoauszug nr|releve n|"
        "numero de releve|extracto n|extrato n|afschriftnummer|kontoutdrag nr|tiliote nro|statement"
    ),
    "statement_date": (
        "statement date|date of statement|estratto al|estratto conto al|data estratto|kontoauszug vom|auszug vom|releve au|"
        "extracto al|statement as of|as of|date|stand|datum|data|dato|pvm"
    ),
    "period": (
        "statement period|period|periodo|periodo di riferimento|zeitraum|abrechnungszeitraum|periode|jakso|okres|donem|"
        "billing period"
    ),
    "opening_balance": (
        "opening balance|opening|previous balance|balance brought forward|brought forward|balance b f|b f|"
        "closing balance last statement|last statement balance|balance last statement|balance previous statement|"
        "saldo iniziale|saldo precedente|saldo apertura|alter kontostand|kontostand alt|saldovortrag|vortrag|ancien solde|"
        "solde precedent|solde initial|saldo anterior|saldo inicial|beginsaldo|begin saldo|beginstand|aanvangssaldo|"
        "inngaende saldo|ingaende saldo|indgaende saldo|alkusaldo|saldo poczatkowy|devreden"
    ),
    "closing_balance": (
        "closing balance|closing|new balance|new balance as of|ending balance|balance carried forward|carried forward|"
        "balance c f|c f|final balance|saldo finale|saldo contabile finale|nuovo saldo|neuer kontostand|kontostand neu|"
        "endsaldo|schlusssaldo|nouveau solde|solde final|solde fin|saldo final|nuevo saldo|saldo actual|eindsaldo|"
        "nieuw saldo|eindstand|utgaende saldo|slutsaldo|loppusaldo|saldo koncowe|kapanis bakiyesi|available balance"
    ),
    "total_debits": (
        "total debits|total debit|debits total|total withdrawals|withdrawals total|total withdrawal|total paid out|"
        "total outgoing|totale uscite|totale dare|totale addebiti|summe soll|gesamt soll|umsatze soll|total des debits|"
        "total retraits|total debitos|total cargos|totaal af|totaal debet|sum uttak|total uttag|summa debet"
    ),
    "total_credits": (
        "total credits|total credit|credits total|total deposits|deposits total|total paid in|total incoming|totale entrate|"
        "totale avere|totale accrediti|summe haben|gesamt haben|total des credits|total depots|total creditos|total abonos|"
        "totaal bij|totaal credit|sum innskudd|total insattning|summa kredit"
    ),
    "currency": "currency|ccy|divisa|waehrung|wahrung|devise|moneda|moeda|valuuta|valuta|conto in",
}

# --------------------------------------------------------------------------
# Rows that are not transactions
# --------------------------------------------------------------------------

ROW: dict[str, str] = {
    "opening": (
        "opening balance|balance brought forward|brought forward|previous balance|balance b f|b f|saldo iniziale|"
        "saldo precedente|saldo apertura|saldo anteriore|saldo inicial|saldo anterior|alter kontostand|saldovortrag|vortrag|"
        "ancien solde|solde precedent|solde initial|report a nouveau|beginsaldo|begin saldo|aanvangssaldo|inngaende saldo|"
        "ingaende saldo|indgaende saldo|alkusaldo|saldo poczatkowy"
    ),
    "closing": (
        "closing balance|balance carried forward|carried forward|balance c f|c f|new balance|ending balance|saldo finale|"
        "saldo final|nuovo saldo|neuer kontostand|endsaldo|schlusssaldo|nouveau solde|solde final|solde fin|eindsaldo|"
        "slutsaldo|utgaende saldo|loppusaldo|saldo koncowe"
    ),
    "balance": "saldo al|saldo alla data|saldo a|balance as of|balance on|stand per|solde au",
    "totals": (
        "total|totale|totali|summe|gesamt|somme|total general|totaal|totalt|yhteensa|suma|toplam|deposits total|"
        "withdrawals total|monthly deposits|monthly withdr|monthly withdrawals|yearly deposits|yearly withdr|"
        "yearly withdrawals|transfer from beg of entry date|sum"
    ),
    "end": (
        "statement summary|end of statement|summary of|computer generated|generated on|registered office|segue|"
        "continued|continua|riporto|interest fees and charges|importante|informativa|please check the entries|"
        "reporting to the foreign exchange"
    ),
}

#: Text before a date that labels a *group* of entries, e.g. "Entry date 03.09.26".
DATE_GROUP = "entry date|booking date|value date|posting date|data operazione|buchungstag|date"

from app.rules.lexicon_intl import EXTRA_COLUMN, EXTRA_FIELD, EXTRA_ROW  # noqa: E402

for _base, _extra in ((COLUMN, EXTRA_COLUMN), (FIELD, EXTRA_FIELD), (ROW, EXTRA_ROW)):
    for _concept, _phrases in _extra.items():
        _base[_concept] = _base.get(_concept, "") + "|" + _phrases

GROUPS = {"column": COLUMN, "field": FIELD, "row": ROW}


@dataclass(frozen=True)
class Hit:
    concept: str
    phrase: str
    start: int  # token index, inclusive
    end: int  # token index, exclusive
    #: Character range of the phrase in the original text.
    char_start: int = 0
    char_end: int = 0

    @property
    def length(self) -> int:
        return self.end - self.start


@lru_cache(maxsize=None)
def _compiled(group: str) -> tuple[tuple[str, tuple[str, ...], bool], ...]:
    out: list[tuple[str, tuple[str, ...], bool]] = []
    for concept, blob in GROUPS[group].items():
        for phrase in blob.split("|"):
            toks = tuple(tokens(phrase))
            if toks:
                # Short Latin words ("date", "af") must be the whole cell; CJK phrases are
                # two characters by nature, so only a single character is "short".
                letters = len("".join(toks))
                short = letters <= (1 if _HAS_CJK.search("".join(toks)) else 4)
                out.append((concept, toks, short))
    out.sort(key=lambda item: (len(item[1]), len("".join(item[1]))), reverse=True)
    return tuple(out)


def _levenshtein_le1(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    for i in range(len(long_)):
        if long_[:i] + long_[i + 1:] == short:
            return True
    return False


def find(group: str, text: str, *, ocr: bool = False) -> list[Hit]:
    """Every phrase of `group` found in `text`, longest first, non-overlapping."""
    spans = token_spans(text)
    toks = [t for t, _, _ in spans]
    if not toks:
        return []
    whole = (0, len(text))
    hits: list[Hit] = []
    for concept, phrase, short in _compiled(group):
        n = len(phrase)
        if short and group != "row":
            # Very short words ("date", "dr", "af") count only as the whole cell.
            if tuple(toks) == phrase:
                hits.append(Hit(concept, " ".join(phrase), 0, len(toks), *whole))
            continue
        for i in range(len(toks) - n + 1):
            if tuple(toks[i:i + n]) == phrase:
                hits.append(Hit(concept, " ".join(phrase), i, i + n, spans[i][1], spans[i + n - 1][2]))

    if ocr:
        squashed = "".join(toks)
        have = {(h.concept, h.phrase) for h in hits}
        for concept, phrase, short in _compiled(group):
            joined = "".join(phrase)
            if short or len(joined) < 8 or (concept, " ".join(phrase)) in have:
                continue
            if joined in squashed:
                hits.append(Hit(concept, " ".join(phrase), 0, len(toks), *whole))
            elif len(joined) >= 12:
                width = len(joined)
                for i in range(len(squashed) - width + 2):
                    if _levenshtein_le1(squashed[i:i + width], joined) or _levenshtein_le1(squashed[i:i + width - 1], joined):
                        hits.append(Hit(concept, " ".join(phrase), 0, len(toks), *whole))
                        break

    hits.sort(key=lambda h: (-len("".join(h.phrase.split())), h.start))
    chosen: list[Hit] = []
    for hit in hits:
        if all(hit.end <= c.start or hit.start >= c.end for c in chosen):
            chosen.append(hit)
    return sorted(chosen, key=lambda h: h.start)


def classify(group: str, text: str, *, ocr: bool = False, min_coverage: float = 0.34) -> str | None:
    """The concept a *label cell* expresses, or None if it is not a label.

    The phrase must cover most of the cell, so the narration
    "TRANSFER FROM SAVINGS" is not mistaken for a heading.
    """
    toks = tokens(text)
    if not toks or len(toks) > 8:
        return None
    best: tuple[int, str] | None = None
    for hit in find(group, text, ocr=ocr):
        coverage = hit.length / len(toks)
        if coverage >= min_coverage or (ocr and hit.start == 0 and hit.end == len(toks)):
            score = len("".join(hit.phrase.split()))
            if best is None or score > best[0]:
                best = (score, hit.concept)
    return best[1] if best else None


def contains(group: str, concept: str, text: str, *, ocr: bool = False) -> bool:
    return any(h.concept == concept for h in find(group, text, ocr=ocr))


# --------------------------------------------------------------------------
# Page furniture
# --------------------------------------------------------------------------

PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:page|pag|pagina|seite|side|sida|sivu|strona|sayfa|strana|stran|oldal|lehekulg|puslapis|lapa)\.?\s*\d+\s*(?:of|di|von|de|sur|av|af|van|/|z)\s*\d+\s*$"
    r"|^\s*\d+\s*/\s*\d+\s*$",
    re.I,
)

#: Words that mark a company name (account holder detection).
COMPANY_SUFFIX = (
    "ltd|limited|llc|llp|inc|incorporated|corp|corporation|plc|gmbh|ag|kg|ug|bv|nv|sa|sas|sarl|srl|spa|s p a|s r l|ab|as|asa|"
    "oy|oyj|abp|aps|a s|sl|slu|ltda|eireli|pty|pvt|pte|bhd|sdn|co|company|group|holding|holdings|foundation|trust|"
    "kft|zrt|sp z o o|ooo|oao|ao|jsc|pjsc|fze|fzco|fzc|llc fz|se|nv sa|associates|"
    "sro|s r o|spol s r o|doo|d o o|ood|ead|uab|sia|zoo|as|nuf|kb|hb|ky|ske|gbr|ohg|ev|eg|scs|sc|snc|sprl|bvba|cvba|"
    "sapi|sab|de cv|s de rl|spa|sac|sas|eirl|pllc|lp|lllp|pc|pa|nfp|npc|cic|clg|dac|unltd|ulc|bhd|plt|pty ltd|pte ltd|wll|"
    "qpsc|psc|spc|saog|saoc|sae|sjsc|sarl au|ltd sti|sti|as|aŞ|tbk|pt|cv|sdn bhd|kk|gk|yk|co ltd|jv|oyj|osakeyhtio|aktiebolag"
)
_SUFFIXES = sorted(COMPANY_SUFFIX.split("|"), key=len, reverse=True)
COMPANY_RE = re.compile(rf"(?<![a-z0-9])(?:{'|'.join(re.escape(s) for s in _SUFFIXES)})(?![a-z0-9])", re.I)
#: Legal form at the END of a name ("... ITALIA SRL"); OCR may glue it ("ITALIASRL").
COMPANY_END_RE = re.compile(rf"(?:{'|'.join(re.escape(s) for s in _SUFFIXES if len(s) >= 3)})\.?$", re.I)


def looks_like_company(text: str) -> bool:
    stripped = re.sub(r"[\s,]+$", "", text.strip())
    if not stripped or len(stripped.split()) > 7:
        return False
    if COMPANY_END_RE.search(stripped):
        return True
    return bool(re.search(r"(?<![A-Za-z0-9])(?:as|ab|ag|sa|se|co|sl|ao)\.?$", stripped, re.I))

BANK_WORD_RE = re.compile(
    r"\b(bank|banka|banco|banca|banque|bankas|banken|sparkasse|sparbank|volksbank|raiffeisen|credit union|building society|"
    r"savings|caisse|cassa|bpm|bper|bnp|hsbc|n\.a\.)\b",
    re.I,
)
