"""A corpus of synthetic statements spanning the conventions found worldwide.

    python tests/corpus.py        # writes ../data/corpus/*.pdf + .expected.json

Each style differs on the axes that broke version 1 of this tool:

  language of labels      de fr es nl pt-br en, and one *unknown* language
  number format           1.234,56   1 234,56   1'234.56   1,234.56   1,23,456.78   1234 (no decimals)   1,234.567
  sign convention         -45,20   (45,20)   45,20-   45,20 D / C   45,20 S / H   split debit & credit columns
  running balance         printed on every row, or absent (statement-level proof only)
  dates                   dd.mm.yyyy  yyyy/mm/dd  d MMM yy  localised month names  mm/dd with no year
  header layout           "Label: value" lines, label | value cells, label above value
  extras                  two-line column headings, opening / closing printed as rows, totals rows

Ground truth is generated with the document, so a miss is a real miss.
These are *my renderings* of how banks lay statements out - they widen coverage
but they are not a substitute for real files.
"""
from __future__ import annotations

import json
import random
import textwrap
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import fitz
from babel.dates import format_date

OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "corpus"
FONTS = {
    "arial": r"C:\Windows\Fonts\arial.ttf",
    "yahei": r"C:\Windows\Fonts\msyh.ttc",
    "malgun": r"C:\Windows\Fonts\malgun.ttf",
    "yugoth": r"C:\Windows\Fonts\YuGothR.ttc",
    "leelawui": r"C:\Windows\Fonts\LeelawUI.ttf",
}
ARABIC_DIGITS = str.maketrans("0123456789", "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669")
PAGE_W, PAGE_H = 595, 842
FONT, BOLD = "helv", "hebo"
SIZE, LINE_H, BOTTOM = 7.5, 11.0, 770


# --------------------------------------------------------------------------
# Vocabulary per language
# --------------------------------------------------------------------------

LANGS: dict[str, dict[str, str]] = {
    "en": dict(title="Account Statement", holder="Account Holder", account="Account Number", iban="IBAN", bic="BIC",
               period="Statement Period", opening="Opening Balance", closing="Closing Balance", tdr="Total Debits",
               tcr="Total Credits", date="Date", value="Value Date", desc="Description", ref="Reference", debit="Debit",
               credit="Credit", amount="Amount", balance="Balance", page="Page {i} of {n}", to="to", drcr="Dr/Cr"),
    "uk": dict(title="Your statement", holder="Account Name", account="Account Number", iban="IBAN", bic="BIC",
               period="Statement Period", opening="Balance brought forward", closing="Closing Balance", tdr="Total Debits",
               tcr="Total Credits", date="Date", value="Value Date", desc="Description", ref="Reference", debit="Paid out",
               credit="Paid in", amount="Amount", balance="Balance", page="Page {i} of {n}", to="to", drcr="Dr/Cr"),
    "de": dict(title="Kontoauszug", holder="Kontoinhaber", account="Kontonummer", iban="IBAN", bic="BIC",
               period="Zeitraum", opening="Alter Kontostand", closing="Neuer Kontostand", tdr="Summe Soll",
               tcr="Summe Haben", date="Buchungstag", value="Wert", desc="Verwendungszweck", ref="Referenz",
               debit="Soll", credit="Haben", amount="Betrag", balance="Saldo", page="Seite {i} von {n}", to="bis", drcr="S/H"),
    "fr": dict(title="Relevé de compte", holder="Titulaire", account="Numéro de compte", iban="IBAN", bic="BIC",
               period="Période", opening="Ancien solde", closing="Nouveau solde", tdr="Total des débits",
               tcr="Total des crédits", date="Date", value="Date de valeur", desc="Libellé", ref="Référence",
               debit="Débit", credit="Crédit", amount="Montant", balance="Solde", page="Page {i} sur {n}", to="au", drcr="D/C"),
    "es": dict(title="Extracto de cuenta", holder="Titular", account="Número de cuenta", iban="IBAN", bic="BIC",
               period="Periodo", opening="Saldo anterior", closing="Saldo final", tdr="Total cargos", tcr="Total abonos",
               date="Fecha", value="Fecha valor", desc="Concepto", ref="Referencia", debit="Debe", credit="Haber",
               amount="Importe", balance="Saldo", page="Página {i} de {n}", to="a", drcr="D/H"),
    "nl": dict(title="Rekeningafschrift", holder="Rekeninghouder", account="Rekeningnummer", iban="IBAN", bic="BIC",
               period="Periode", opening="Beginsaldo", closing="Eindsaldo", tdr="Totaal af", tcr="Totaal bij",
               date="Datum", value="Valutadatum", desc="Omschrijving", ref="Kenmerk", debit="Af", credit="Bij",
               amount="Bedrag", balance="Saldo", page="Pagina {i} van {n}", to="tot", drcr="Af/Bij"),
    "pt": dict(title="Extrato de conta", holder="Titular", account="Conta", iban="IBAN", bic="BIC",
               period="Período", opening="Saldo anterior", closing="Saldo final", tdr="Total débitos", tcr="Total créditos",
               date="Data", value="Data valor", desc="Histórico", ref="Documento", debit="Débito", credit="Crédito",
               amount="Valor", balance="Saldo", page="Página {i} de {n}", to="a", drcr="D/C"),
    # An invented language: nothing in the lexicon can read it, so the structural fallback must cope.
    "xx": dict(title="Zorvak", holder="Kelmun", account="Plax numen", iban="IBAN", bic="BIC", period="Trovid",
               opening="Aldrin", closing="Mervek", tdr="Spor qua", tcr="Spor zel", date="Quorn",
               value="Wexil", desc="Tamrel", ref="Ref", debit="Fenza", credit="Hilto", amount="Dunor",
               balance="Pelvan", page="Strana {i}/{n}", to="-", drcr="V/P"),
    # Other scripts - headings are the starter vocabulary, so these prove the plumbing
    # (Unicode tokens, digit normalisation, right-to-left geometry), not a bank's wording.
    "ru": dict(title="Выписка по счету", holder="Владелец счета", account="Номер счета", iban="IBAN", bic="BIC",
               period="Период", opening="Входящий остаток", closing="Исходящий остаток", tdr="Итого списаний",
               tcr="Итого зачислений", date="Дата операции", value="Дата обработки", desc="Описание", ref="Документ",
               debit="Расход", credit="Приход", amount="Сумма", balance="Остаток", page="Страница {i} из {n}", to="по", drcr="Д/К"),
    "zh": dict(title="账户对账单", holder="户名", account="账号", iban="IBAN", bic="BIC", period="查询期间",
               opening="期初余额", closing="期末余额", tdr="借方合计", tcr="贷方合计", date="交易日期", value="起息日",
               desc="摘要", ref="凭证号", debit="借方", credit="贷方", amount="金额", balance="余额", page="第 {i} 页 共 {n} 页",
               to="至", drcr="借贷"),
    "ja": dict(title="お取引明細", holder="口座名義", account="口座番号", iban="IBAN", bic="BIC", period="期間",
               opening="前回残高", closing="今回残高", tdr="出金合計", tcr="入金合計", date="取引日", value="起算日",
               desc="お取引内容", ref="参照番号", debit="お支払金額", credit="お預り金額", amount="金額", balance="残高",
               page="{i} / {n} ページ", to="から", drcr="区分"),
    "ko": dict(title="거래내역서", holder="예금주", account="계좌번호", iban="IBAN", bic="BIC", period="조회기간",
               opening="이월잔액", closing="기말잔액", tdr="출금합계", tcr="입금합계", date="거래일자", value="처리일",
               desc="적요", ref="참조", debit="출금", credit="입금", amount="금액", balance="잔액", page="{i} / {n} 쪽",
               to="~", drcr="구분"),
    "ar": dict(title="كشف حساب", holder="اسم الحساب", account="رقم الحساب", iban="IBAN", bic="BIC", period="الفترة",
               opening="الرصيد الافتتاحي", closing="الرصيد الختامي", tdr="إجمالي المدين", tcr="إجمالي الدائن",
               date="التاريخ", value="تاريخ القيمة", desc="البيان", ref="المرجع", debit="مدين", credit="دائن",
               amount="المبلغ", balance="الرصيد", page="صفحة {i} من {n}", to="إلى", drcr="م/د"),
    "he": dict(title="דף חשבון", holder="שם החשבון", account="מספר חשבון", iban="IBAN", bic="BIC", period="תקופה",
               opening="יתרת פתיחה", closing="יתרת סגירה", tdr="סך חובה", tcr="סך זכות", date="תאריך",
               value="תאריך ערך", desc="תיאור", ref="אסמכתא", debit="חובה", credit="זכות", amount="סכום",
               balance="יתרה", page="עמוד {i} מתוך {n}", to="עד", drcr="ח/ז"),
    "th": dict(title="รายการเดินบัญชี", holder="ชื่อบัญชี", account="เลขที่บัญชี", iban="IBAN", bic="BIC",
               period="ช่วงเวลา", opening="ยอดยกมา", closing="ยอดคงเหลือสุดท้าย", tdr="ยอดถอนรวม", tcr="ยอดฝากรวม",
               date="วันที่", value="วันที่มีผล", desc="รายการ", ref="อ้างอิง", debit="ถอน", credit="ฝาก",
               amount="จำนวนเงิน", balance="ยอดคงเหลือ", page="หน้า {i}/{n}", to="ถึง", drcr="ด/ค"),
}

NARRATIONS: dict[str, list[str]] = {
    "ru": ["Оплата по счету {n}", "Зарплата за месяц", "Перевод между счетами {n}", "Комиссия за обслуживание", "Поступление от клиента {n}"],
    "zh": ["货款 {n}", "工资发放", "转账汇款 {n}", "手续费", "客户回款 {n}"],
    "ja": ["お振込 {n}", "給与振込", "手数料", "口座振替 {n}", "ATM出金"],
    "ko": ["급여 입금", "카드대금 {n}", "수수료", "이체 {n}", "ATM 출금"],
    "ar": ["دفعة فاتورة {n}", "تحويل راتب", "رسوم الخدمة", "إيداع نقدي {n}", "سحب صراف"],
    "he": ["תשלום חשבונית {n}", "משכורת", "עמלה", "העברה בנקאית {n}", "משיכת מזומן"],
    "th": ["ชำระค่าสินค้า {n}", "เงินเดือน", "ค่าธรรมเนียม", "โอนเงิน {n}", "ถอนเงินสด"],
}


@dataclass
class Style:
    name: str
    lang: str
    currency: str
    decimals: int
    dec: str
    grp: str
    neg: str  # sign | paren | trail | drcr | sh | cd | split
    mode: str  # split | signed | typed
    has_balance: bool
    date_fmt: str  # strftime pattern, "babel:<locale>", or "noyear"
    header: str  # inline | cells | stacked
    holder: str
    bank: str
    iban: str | None = None
    bic: str | None = None
    account: str | None = None
    lakh: bool = False
    value_date: bool = False
    stacked_headings: bool = False
    show_summary: bool = True
    show_totals: bool = False
    balance_rows: bool = False  # opening/closing as rows in the table
    currency_show: str = "code"  # code | none | symbol
    rows: int = 28
    start: date = date(2025, 3, 1)
    end: date = date(2025, 3, 31)
    opening: Decimal = Decimal("5000.00")
    seed: int = 1
    notes: str = ""
    blank_labels: list[str] = field(default_factory=list)
    #: Values printed under labels in a language the lexicon cannot read.
    unlabelled: list[str] = field(default_factory=list)
    font: str | None = None  # key into FONTS; needed for non-Latin scripts
    numerals: str = "latn"  # "arab" prints Arabic-Indic digits
    mirror: bool = False  # right-to-left page
    narrations: list[str] | None = None


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


def group_digits(whole: str, grp: str, lakh: bool) -> str:
    if len(whole) <= 3 or not grp:
        return whole
    head, tail = whole[:-3], whole[-3:]
    parts: list[str] = []
    step = 2 if lakh else 3
    while len(head) > step:
        parts.insert(0, head[-step:])
        head = head[:-step]
    if head:
        parts.insert(0, head)
    return grp.join(parts + [tail])


def local_digits(text: str, st: Style) -> str:
    return text.translate(ARABIC_DIGITS) if st.numerals == "arab" else text


def number(value: Decimal, st: Style) -> str:
    quant = Decimal(1).scaleb(-st.decimals)
    text = f"{abs(value).quantize(quant):f}"
    whole, _, frac = text.partition(".")
    out = group_digits(whole, st.grp, st.lakh)
    return local_digits(out + (st.dec + frac if st.decimals else ""), st)


def amount_cell(value: Decimal, is_debit: bool, st: Style) -> str:
    n = number(value, st)
    return {
        "sign": ("-" + n) if is_debit else n,
        "paren": ("(" + n + ")") if is_debit else n,
        "trail": n + ("-" if is_debit else "+"),
        "drcr": n + (" Dr" if is_debit else " Cr"),
        "sh": n + (" S" if is_debit else " H"),
        "cd": n + (" D" if is_debit else " C"),
        "split": n,
    }[st.neg]


def balance_cell(value: Decimal, st: Style) -> str:
    n = number(value, st)
    if st.neg in ("drcr", "sh", "cd"):
        return n + {"drcr": " Cr", "sh": " H", "cd": " C"}[st.neg] if value >= 0 else n + {"drcr": " Dr", "sh": " S", "cd": " D"}[st.neg]
    return ("-" + n) if value < 0 else n


def date_text(d: date, st: Style) -> str:
    if st.date_fmt == "thai":  # Buddhist era
        return f"{d.day:02d}/{d.month:02d}/{d.year + 543}"
    if "年" in st.date_fmt:
        return d.strftime(st.date_fmt.replace("年", "{y}").replace("月", "{m}").replace("日", "{d}")).format(y="年", m="月", d="日")
    if st.numerals == "arab":
        return local_digits(d.strftime(st.date_fmt), st)
    if st.date_fmt == "noyear":
        return d.strftime("%m/%d")
    if st.date_fmt.startswith("babel:"):
        return format_date(d, format="d MMM yyyy", locale=st.date_fmt.split(":")[1])
    return d.strftime(st.date_fmt)


def money_label(value: Decimal, st: Style) -> str:
    n = number(value, st)
    if st.currency_show == "code":
        return f"{st.currency} {n}"
    return n


# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------

DESCRIPTIONS = [
    "CARD PAYMENT SUPERMARKET {n}", "SALARY {n}", "STANDING ORDER RENT", "TRANSFER TO J. SMITH {n}",
    "DIRECT DEBIT ENERGY CO {n}", "ATM WITHDRAWAL CITY CENTRE", "INTEREST", "INVOICE {n} PAYMENT",
    "REFUND ORDER {n}", "MONTHLY ACCOUNT FEE",
]


@dataclass
class Txn:
    day: date
    description: str
    reference: str
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal


def make_txns(st: Style) -> list[Txn]:
    rng = random.Random(st.seed)
    span = (st.end - st.start).days
    days = sorted(st.start + timedelta(days=rng.randint(0, span)) for _ in range(st.rows))
    balance = st.opening
    unit = Decimal(1).scaleb(-st.decimals)
    out: list[Txn] = []
    for day in days:
        magnitude = Decimal(rng.randint(500, 250_000)) / (Decimal(10) ** min(st.decimals, 2)) if st.decimals else Decimal(rng.randint(500, 250_000))
        magnitude = magnitude.quantize(unit)
        credit = rng.random() < 0.35
        if not credit and magnitude >= balance:
            credit = True
        balance += magnitude if credit else -magnitude
        ref = "".join(rng.choice("0123456789") for _ in range(rng.choice((6, 8, 10))))
        out.append(Txn(day, rng.choice(st.narrations or DESCRIPTIONS).format(n=rng.randint(100, 999)), ref,
                       None if credit else magnitude, magnitude if credit else None, balance))
    return out


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


class Canvas:
    def __init__(self, fontfile: str | None = None, mirror: bool = False) -> None:
        self.doc = fitz.open()
        self.fontfile = fontfile
        self.mirror = mirror
        self._font = fitz.Font(fontfile=fontfile) if fontfile else None
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self._prepare(self.page)

    def _prepare(self, page) -> None:
        if self.fontfile:
            page.insert_font(fontname="UF", fontfile=self.fontfile)

    def new_page(self) -> None:
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self._prepare(self.page)

    def width(self, s: str, size: float, bold: bool = False) -> float:
        if self._font is not None:
            return self._font.text_length(s, fontsize=size)
        return fitz.get_text_length(s, fontname=BOLD if bold else FONT, fontsize=size)

    @staticmethod
    def visual(s: str) -> str:
        """What a right-to-left producer stores: shaped Arabic, in left-to-right glyph order."""
        import arabic_reshaper
        from bidi.algorithm import get_display

        return get_display(arabic_reshaper.reshape(s), base_dir="R")

    def text(self, x, y, s, size=SIZE, bold=False, right=False) -> None:
        if self.mirror:  # a right-to-left page is the same layout reflected
            x, right = PAGE_W - x, not right
            s = self.visual(s)
        if right:
            x -= self.width(s, size, bold)
        if self.fontfile:
            self.page.insert_text((x, y), s, fontname="UF", fontsize=size)
        else:
            self.page.insert_text((x, y), s, fontname=BOLD if bold else FONT, fontsize=size)

    def rect(self, x0, y0, x1, y1, fill=(0.9, 0.92, 0.96)) -> None:
        self.page.draw_rect(fitz.Rect(x0, y0, x1, y1), color=None, fill=fill)

    def finish(self, path: Path, footer: str) -> None:
        total = self.doc.page_count
        for index, page in enumerate(self.doc, start=1):
            label = footer.format(i=index, n=total)
            if self.mirror:
                label = self.visual(label)
            width = self.width(label, 7)
            if self.fontfile:
                page.insert_text(((PAGE_W - width) / 2, 820), label, fontname="UF", fontsize=7)
            else:
                page.insert_text(((PAGE_W - width) / 2, 820), label, fontname=FONT, fontsize=7)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(path)
        self.doc.close()


@dataclass
class Col:
    key: str
    heading: list[str]
    x: float
    right: bool = False


def columns_for(st: Style) -> list[Col]:
    L = LANGS[st.lang]
    two = st.stacked_headings
    cols = [Col("date", [L["date"]] if not two else [L["date"], ""], 36)]
    x_desc = 36 + (136 if st.value_date else 66)
    if st.value_date:
        cols.append(Col("value", [L["value"]] if not two else [L["value"], ""], 92))
    cols.append(Col("desc", [L["desc"]], x_desc))
    if st.mode == "split":
        cols.append(Col("debit", [L["debit"]], 398, right=True))
        cols.append(Col("credit", [L["credit"]], 464, right=True))
    elif st.mode == "signed":
        cols.append(Col("amount", ([L["amount"]] if not two else [L["amount"], f"({st.currency})"]), 440, right=True))
    else:  # typed: Amount + Dr/Cr indicator column
        cols.append(Col("amount", [L["amount"]], 420, right=True))
        cols.append(Col("drcr", [L["drcr"]], 436))
    if st.has_balance:
        cols.append(Col("balance", [L["balance"]], 556, right=True))
    return cols


def cells_for(t: Txn, st: Style) -> dict[str, str]:
    cell = {"date": date_text(t.day, st), "desc": t.description, "value": date_text(t.day, st)}
    if st.mode == "split":
        cell["debit"] = number(t.debit, st) if t.debit else ""
        cell["credit"] = number(t.credit, st) if t.credit else ""
    elif st.mode == "signed":
        cell["amount"] = amount_cell(t.debit or t.credit, t.debit is not None, st)
    else:
        cell["amount"] = number(t.debit or t.credit, st)
        cell["drcr"] = "D" if t.debit else "C"
    if st.has_balance:
        cell["balance"] = balance_cell(t.balance, st)
    return cell


def header_block(cv: Canvas, st: Style, txns: list[Txn]) -> float:
    L = LANGS[st.lang]
    cv.text(36, 52, st.bank, size=15, bold=True)
    cv.text(36, 66, L["title"], size=9)
    totals_dr = sum((t.debit for t in txns if t.debit), Decimal(0))
    totals_cr = sum((t.credit for t in txns if t.credit), Decimal(0))
    closing = txns[-1].balance if txns else st.opening
    pairs: list[tuple[str, str]] = [(L["holder"], st.holder)]
    if st.account:
        pairs.append((L["account"], st.account))
    if st.iban:
        pairs.append((L["iban"], st.iban))
    if st.bic:
        pairs.append((L["bic"], st.bic))
    period = f"{date_text(st.start, st)} {L['to']} {date_text(st.end, st)}"
    if st.date_fmt == "noyear":
        period = f"{format_date(st.start, format='MMM d, yyyy', locale='en')} - {format_date(st.end, format='MMM d, yyyy', locale='en')}"
    pairs.append((L["period"], period))
    if st.show_summary:
        pairs.append((L["opening"], money_label(st.opening, st)))
        pairs.append((L["closing"], money_label(closing, st)))
    if st.show_totals:
        pairs.append((L["tdr"], money_label(totals_dr, st)))
        pairs.append((L["tcr"], money_label(totals_cr, st)))
    pairs = [p for p in pairs if p[0] not in st.blank_labels]

    y = 100.0
    for label, value in pairs:
        if st.header == "inline":
            cv.text(36, y, f"{label}: {value}", size=8)
            y += 13
        elif st.header == "cells":
            cv.text(36, y, f"{label}:")
            cv.text(190, y, value, bold=True)
            y += 13
        else:  # stacked: label above value
            cv.text(36, y, label, size=6.5)
            cv.text(36, y + 9, value, bold=True)
            y += 24
    return y + 18


def render(st: Style) -> dict:
    L = LANGS[st.lang]
    txns = make_txns(st)
    cv = Canvas(FONTS[st.font] if st.font else None, st.mirror)
    y = header_block(cv, st, txns)
    cols = columns_for(st)

    def header_row(y0: float) -> float:
        lines = max(len(c.heading) for c in cols)
        cv.rect(34, y0 - 9, 561, y0 - 9 + 4 + 9.5 * lines)
        for col in cols:
            for n, piece in enumerate(col.heading):
                if piece:
                    cv.text(col.x, y0 + 9.5 * n, piece, bold=True, right=col.right)
        return y0 + 9.5 * lines + 4

    y = header_row(y)
    if st.balance_rows:
        for label, value in ((L["opening"], st.opening),):
            if st.has_balance:
                cv.text(cols[1].x if not st.value_date else cols[2].x, y, label)
                cv.text(556, y, balance_cell(value, st), right=True)
            else:
                cv.text(cols[1].x if not st.value_date else cols[2].x, y, label)
                cv.text(398 if st.mode == "split" else 440, y, number(value, st), right=True)
            y += LINE_H

    for t in txns:
        cell = cells_for(t, st)
        lines = textwrap.wrap(cell["desc"], 38, break_long_words=False) or [""]
        height = LINE_H * len(lines)
        if y + height > BOTTOM:
            cv.new_page()
            y = 56.0
            y = header_row(y + 6)
        for col in cols:
            if col.key == "desc":
                for n, piece in enumerate(lines):
                    cv.text(col.x, y + LINE_H * n, piece)
            elif cell.get(col.key):
                cv.text(col.x, y, cell[col.key], right=col.right)
        y += height

    closing = txns[-1].balance
    if st.balance_rows:
        x_desc = cols[1].x if not st.value_date else cols[2].x
        cv.text(x_desc, y + 4, L["closing"], bold=True)
        if st.has_balance:
            cv.text(556, y + 4, balance_cell(closing, st), bold=True, right=True)
        else:
            cv.text(398 if st.mode == "split" else 440, y + 4, number(closing, st), bold=True, right=True)
        y += LINE_H + 4
    cv.finish(OUT_DIR / f"{st.name}.pdf", L["page"])

    totals_dr = sum((t.debit for t in txns if t.debit), Decimal(0))
    totals_cr = sum((t.credit for t in txns if t.credit), Decimal(0))
    quant = Decimal(1).scaleb(-st.decimals)
    rows = []
    for t in txns:
        row = {"date": t.day.isoformat(),
               "debit": f"{t.debit.quantize(quant):f}" if t.debit else None,
               "credit": f"{t.credit.quantize(quant):f}" if t.credit else None}
        if st.has_balance:
            row["balance"] = f"{t.balance.quantize(quant):f}"
        rows.append(row)
    header: dict[str, str] = {"account_holder": st.holder}
    if st.iban:
        header["iban"] = st.iban.replace(" ", "")
    if st.bic:
        header["bic"] = st.bic
    if st.account:
        header["account_number"] = st.account.replace(" ", "").replace("-", "")
    header["period_from"], header["period_to"] = st.start.isoformat(), st.end.isoformat()
    if st.show_summary or st.balance_rows:
        header_opening = st.show_summary
        if header_opening:
            header["opening_balance"] = f"{st.opening.quantize(quant):f}"
            header["closing_balance"] = f"{closing.quantize(quant):f}"
    if st.show_totals:
        header["total_debits"] = f"{totals_dr.quantize(quant):f}"
        header["total_credits"] = f"{totals_cr.quantize(quant):f}"
    # A currency is only "printed" if some amount or heading carries it.
    printed_with_amount = st.currency_show == "code" and (st.show_summary or st.show_totals)
    printed_in_heading = st.stacked_headings and st.mode == "signed"
    if printed_with_amount or printed_in_heading or (st.iban and not printed_with_amount and st.iban[:2] in ("DE", "FR", "NL", "ES", "IT", "NO", "SK", "AE", "CH", "KW") and False):
        header["currency"] = st.currency
    for key in st.unlabelled:
        header.pop(key, None)
    expected = {
        "header": header,
        "absent": [],
        "rows": rows,
        "opening": f"{st.opening.quantize(quant):f}",
        "closing": f"{closing.quantize(quant):f}",
        "mode": "running_balance" if st.has_balance else "statement_totals",
        "notes": st.notes,
    }
    (OUT_DIR / f"{st.name}.expected.json").write_text(json.dumps(expected, indent=2), encoding="utf-8")
    return expected


# --------------------------------------------------------------------------
# The styles
# --------------------------------------------------------------------------

STYLES: list[Style] = [
    Style("de_split_nobalance", "de", "EUR", 2, ",", ".", "split", "split", False, "%d.%m.%Y", "cells",
          "Maria Mustermann", "Nordstadt Sparkasse", iban="DE89 3704 0044 0532 0130 00", bic="COBADEFFXXX",
          show_totals=True, seed=11, notes="Soll/Haben columns, 1.234,56, no running balance, totals printed"),
    Style("de_sh_suffix_balance", "de", "EUR", 2, ",", ".", "sh", "signed", True, "%d.%m.%y", "inline",
          "Hans Beispiel GmbH", "Rhein Bank AG", iban="DE02 1203 0000 0000 2020 51", value_date=True, seed=12,
          notes="single Betrag with S/H suffix, running Saldo, value date"),
    Style("fr_split_thin_space", "fr", "EUR", 2, ",", " ", "split", "split", False, "babel:fr", "stacked",
          "Société Dupont SARL", "Banque Régionale", iban="FR14 2004 1010 0505 0001 3M02 606", show_totals=True,
          currency_show="code", seed=13, notes="1 234,56 (narrow space), French month names, label above value"),
    Style("es_signed_balance", "es", "EUR", 2, ",", ".", "sign", "signed", True, "%d/%m/%Y", "cells",
          "Industrias Ibéricas SL", "Banco del Sur", iban="ES91 2100 0418 4502 0005 1332", value_date=True, seed=14,
          notes="signed Importe, Saldo, Fecha valor"),
    Style("nl_afbij_nobalance", "nl", "EUR", 2, ",", ".", "split", "split", False, "%d-%m-%Y", "inline",
          "Jansen Beheer BV", "Nederlandse Handelsbank", iban="NL91 ABNA 0417 1643 00", show_totals=True, seed=15,
          notes="Af/Bij columns"),
    Style("br_cd_suffix_balance", "pt", "BRL", 2, ",", ".", "cd", "signed", True, "%d/%m/%Y", "cells",
          "Comercial Aurora Ltda", "Banco Brasil Central", account="12345-6", seed=16,
          notes="1.234,56 C/D suffix, Histórico, Saldo"),
    Style("uk_split_balance_bfwd", "uk", "GBP", 2, ".", ",", "split", "split", True, "babel:en_GB", "cells",
          "Mr J A Smith", "Albion Bank plc", account="40-47-84 70872490", balance_rows=True, seed=17,
          notes="Paid out / Paid in, 'Balance brought forward' row, text months"),
    Style("us_noyear_paren", "en", "USD", 2, ".", ",", "paren", "signed", True, "noyear", "inline",
          "Jordan Ellis", "Lakeshore Trust Bank, N.A.", account="000123456789", currency_show="none", seed=18,
          notes="mm/dd with the year only in the period; debits in parentheses"),
    Style("ch_apostrophe_split", "de", "CHF", 2, ".", "'", "split", "split", True, "%d.%m.%Y", "cells",
          "Alpen Treuhand AG", "Zürcher Privatbank", iban="CH93 0076 2011 6238 5295 7", seed=19,
          notes="1'234.56, Swiss"),
    Style("jp_no_decimals", "en", "JPY", 0, ".", ",", "split", "split", True, "%Y/%m/%d", "cells",
          "Sakura Trading KK", "Tokyo Central Bank", account="1234567", seed=20,
          notes="no decimals, yyyy/mm/dd"),
    Style("kw_three_decimals", "en", "KWD", 3, ".", ",", "split", "split", True, "%d/%m/%Y", "cells",
          "Gulf Logistics WLL", "Kuwait Commercial Bank", iban="KW81 CBKU 0000 0000 0000 1234 5601 01", seed=21,
          opening=Decimal("12500.000"), notes="3 decimal places"),
    Style("in_lakh_drcr_typed", "en", "INR", 2, ".", ",", "drcr", "typed", True, "%d-%b-%Y", "cells",
          "Bharat Exports Pvt Ltd", "Hindustan National Bank", account="004510034567", lakh=True, currency_show="none",
          opening=Decimal("1250430.55"), seed=22, notes="lakh grouping, Amount + Dr/Cr column, Cr/Dr balance"),
    Style("trailing_sign_nobalance", "en", "EUR", 2, ",", ".", "trail", "signed", False, "%d.%m.%y", "inline",
          "Nordic Holdings AB", "Fjord Bank ASA", iban="NO93 8601 1117 947", show_totals=True, seed=23,
          notes="Nordea-style trailing sign, no running balance"),
    Style("xx_unknown_language", "xx", "EUR", 2, ",", ".", "split", "split", True, "%d.%m.%Y", "cells",
          "Spolocnost Alfa sro", "Slovenska Banka", iban="SK31 1200 0000 1987 4263 7541", seed=24,
          unlabelled=["opening_balance", "closing_balance"],
          notes="headings in a language the lexicon does not know -> structural fallback"),
]


STYLES += [
    Style("ru_cyrillic", "ru", "RUB", 2, ",", " ", "split", "split", True, "%d.%m.%Y", "cells",
          "ООО Ромашка", "Банк Восточный", account="40702810900000012345", font="arial", seed=31,
          narrations=NARRATIONS["ru"], notes="Cyrillic labels and narrations"),
    Style("zh_chinese", "zh", "CNY", 2, ".", ",", "split", "split", True, "%Y年%m月%d日", "cells",
          "上海贸易有限公司", "中国示范银行", account="6222020200012345678", font="yahei", seed=32,
          narrations=NARRATIONS["zh"], notes="Chinese labels, 2025年3月5日 dates"),
    Style("ja_japanese", "ja", "JPY", 0, ".", ",", "split", "split", True, "%Y/%m/%d", "cells",
          "株式会社サクラ", "東京中央銀行", account="1234567", font="yugoth", seed=33,
          opening=Decimal("1250000"), narrations=NARRATIONS["ja"], notes="Japanese labels, no decimals"),
    Style("ko_korean", "ko", "KRW", 0, ".", ",", "split", "split", True, "%Y.%m.%d", "cells",
          "주식회사 한빛", "한국중앙은행", account="110-123-456789", font="malgun", seed=34,
          opening=Decimal("3500000"), narrations=NARRATIONS["ko"], notes="Korean labels, no decimals"),
    Style("ar_arabic_rtl", "ar", "AED", 2, ".", ",", "split", "split", True, "%d/%m/%Y", "cells",
          "شركة النور للتجارة", "بنك الخليج الدولي", iban="AE07 0331 2345 6789 0123 456", font="arial", seed=35,
          mirror=True, narrations=NARRATIONS["ar"], notes="Arabic labels, right-to-left, Latin digits"),
    Style("he_hebrew_rtl", "he", "ILS", 2, ".", ",", "split", "split", True, "%d/%m/%Y", "cells",
          "חברת אלון בע\"מ", "בנק מרכזי ישראלי", account="12-345-678901", font="arial", seed=36,
          mirror=True, narrations=NARRATIONS["he"], notes="Hebrew, right-to-left"),
    Style("th_thai_buddhist", "th", "THB", 2, ".", ",", "split", "split", True, "thai", "cells",
          "บริษัท สยาม จำกัด", "ธนาคารกลางสยาม", account="123-4-56789-0", font="leelawui", seed=37,
          start=date(2025, 3, 1), end=date(2025, 3, 31), narrations=NARRATIONS["th"], notes="Thai, Buddhist-era years (2568)"),
]


def render_variants() -> None:
    """The same statement as a scanner, a phone or a print driver might deliver it."""
    from PIL import Image

    source = OUT_DIR / "de_split_nobalance.pdf"
    expected_text = source.with_suffix(".expected.json").read_text(encoding="utf-8")
    page = fitz.open(source)[0]
    page.get_pixmap(dpi=200).save(OUT_DIR / "_straight.png")
    straight = Image.open(OUT_DIR / "_straight.png").convert("RGB")
    white = (255, 255, 255)
    variants = {
        "scan_upright": straight,
        "scan_upside_down": straight.rotate(180),
        "scan_sideways_left": straight.rotate(90, expand=True, fillcolor=white),
        "scan_sideways_right": straight.rotate(-90, expand=True, fillcolor=white),
        "scan_skewed_2deg": straight.rotate(-2.0, expand=True, fillcolor=white),
        "scan_skewed_upside_down": straight.rotate(183, expand=True, fillcolor=white),
    }
    for name, image in variants.items():
        image.save(OUT_DIR / f"{name}.png")
        (OUT_DIR / f"{name}.expected.json").write_text(expected_text, encoding="utf-8")
    (OUT_DIR / "_straight.png").unlink()

    # A landscape page produced by turning a portrait page's content: the text runs bottom-to-top.
    src = fitz.open(source)
    out = fitz.open()
    for p in src:
        sideways = out.new_page(width=p.rect.height, height=p.rect.width)
        sideways.show_pdf_page(sideways.rect, src, p.number, rotate=90)
    out.save(OUT_DIR / "pdf_content_turned_90.pdf")
    (OUT_DIR / "pdf_content_turned_90.expected.json").write_text(expected_text, encoding="utf-8")
    print(f"wrote {len(variants)} raster variants and a turned-content PDF of de_split_nobalance")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for style in STYLES:
        expected = render(style)
        print(f"wrote {style.name}.pdf: {len(expected['rows'])} rows - {style.notes}")
    render_variants()


if __name__ == "__main__":
    main()
