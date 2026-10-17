"""Starter vocabulary for scripts and languages beyond the main Latin-script set.

Russian, Chinese, Japanese, Korean, Arabic, Hebrew, Thai, Greek, Turkish, Polish,
Czech/Slovak, Hungarian, Romanian, Indonesian/Malay, Vietnamese.

**Not verified against real statements.** These phrases come from published
statement conventions and standard banking terminology; they are enough to
recognise headings in clean PDFs and to prove the plumbing (Unicode tokens,
digit normalisation, CJK dates) works, but a bank's own wording will differ.
When a heading is missed, add the bank's phrase here - no code changes needed.
Unrecognised headings still fall back to structural detection.

Each value is merged into the matching concept of `lexicon.COLUMN / FIELD / ROW`.
"""
from __future__ import annotations

EXTRA_COLUMN: dict[str, str] = {
    "date": (
        "дата|дата операции|дата проводки|дата транзакции|"
        "交易日期|日期|记账日期|交易时间|記帳日期|交易日|"
        "日付|取引日|年月日|お取引日|"
        "거래일자|거래일|일자|날짜|"
        "التاريخ|تاريخ|تاريخ العملية|تاريخ المعاملة|"
        "תאריך|תאריך פעולה|"
        "วันที่|วันที่ทำรายการ|"
        "ημερομηνια|ημ νια|"
        "işlem tarihi|tarih|"
        "data operacji|data księgowania|data ksiegowania|"
        "dátum|datum|dátum zaúčtování|"
        "dátum|könyvelés dátuma|"
        "data tranzactiei|data|"
        "tanggal|tgl|ngày|ngay giao dich|ngày giao dịch"
    ),
    "value_date": (
        "дата валютирования|дата обработки|"
        "起息日|生效日期|"
        "起算日|勘定日|"
        "تاريخ القيمة|"
        "תאריך ערך|ערך|"
        "ημερομηνια αξιας|"
        "valor tarihi|data waluty|dátum valuty|értéknap|data valutei|tanggal valuta"
    ),
    "description": (
        "описание|назначение платежа|содержание операции|наименование операции|детали операции|"
        "摘要|交易摘要|交易说明|备注|用途|附言|摘要说明|"
        "お取引内容|取引内容|内容|"
        "적요|거래내용|내용|거래기록사항|"
        "البيان|الوصف|التفاصيل|تفاصيل العملية|الشرح|"
        "תיאור|פרטים|תיאור הפעולה|"
        "รายการ|รายละเอียด|"
        "περιγραφη|αιτιολογια|"
        "açıklama|işlem açıklaması|"
        "opis operacji|tytuł operacji|opis|"
        "popis|popis transakcie|popis operace|"
        "megnevezés|közlemény|leírás|"
        "descriere|detalii|detalii tranzactie|"
        "keterangan|uraian|diễn giải|noi dung"
    ),
    "reference": (
        "номер документа|документ|референс|"
        "凭证号|参考号|流水号|交易流水号|"
        "参照番号|"
        "المرجع|رقم المرجع|رقم العملية|"
        "אסמכתא|מספר אסמכתא|"
        "αριθμος εγγραφου|"
        "numer referencyjny|referenčné číslo|hivatkozás|nr referinta|nomor referensi|số tham chiếu"
    ),
    "debit": (
        "дебет|расход|списание|сумма списания|выплаты|"
        "借方|支出|借方发生额|付款金额|取款|支取金额|"
        "お支払金額|出金|支払金額|出金金額|引出|"
        "출금|출금액|지급|찾으신금액|"
        "مدين|سحب|المدين|مسحوبات|صادر|"
        "חובה|חיוב|משיכה|"
        "ถอน|ถอนเงิน|เดบิต|ยอดถอน|"
        "χρεωση|χρεωσεις|"
        "borç|çıkan|çekilen|"
        "obciążenia|obciazenia|wypłata|wyplata|"
        "má dáti|ma dati|výdaj|vydaj|"
        "terhelés|terheles|"
        "debit|retrageri|"
        "debet|pengeluaran|ghi nợ|ghi no"
    ),
    "credit": (
        "кредит|приход|зачисление|сумма зачисления|поступления|"
        "贷方|收入|贷方发生额|收款金额|存款|存入金额|"
        "お預り金額|入金|預入金額|入金金額|預入|"
        "입금|입금액|맡기신금액|"
        "دائن|إيداع|الدائن|إيداعات|وارد|"
        "זכות|זיכוי|הפקדה|"
        "ฝาก|ฝากเงิน|เครดิต|ยอดฝาก|"
        "πιστωση|πιστωσεις|"
        "alacak|giren|yatan|"
        "uznania|wpłata|wplata|"
        "dal|príjem|prijem|"
        "jóváírás|jovairas|"
        "credit|încasări|incasari|"
        "kredit|pemasukan|ghi có|ghi co"
    ),
    "amount": (
        "сумма|сумма операции|"
        "金额|交易金额|发生额|"
        "金額|"
        "금액|거래금액|"
        "المبلغ|القيمة|"
        "סכום|"
        "ποσο|"
        "tutar|işlem tutarı|"
        "kwota|suma|"
        "összeg|osszeg|"
        "jumlah|số tiền|so tien"
    ),
    "balance": (
        "остаток|баланс|остаток по счету|сальдо|"
        "余额|账户余额|当前余额|餘額|"
        "残高|差引残高|お預り残高|"
        "잔액|거래후잔액|"
        "الرصيد|الرصيد الجاري|"
        "יתרה|"
        "ยอดคงเหลือ|คงเหลือ|"
        "υπολοιπο|"
        "bakiye|son bakiye|"
        "zůstatek|zustatek|zostatok|egyenleg|sold|saldo curent|số dư|so du"
    ),
    "drcr": "д к|дб кр|借贷|借/贷|مدين دائن|חובה זכות",
}

EXTRA_FIELD: dict[str, str] = {
    "account_number": (
        "номер счета|номер счёта|счет|расчетный счет|р с|"
        "账号|账户号码|卡号|账户|帐号|"
        "口座番号|口座|"
        "계좌번호|계좌|"
        "رقم الحساب|حساب|"
        "מספר חשבון|חשבון|"
        "เลขที่บัญชี|บัญชี|"
        "αριθμος λογαριασμου|"
        "hesap no|hesap numarası|"
        "číslo účtu|cislo uctu|číslo účtu|számlaszám|szamlaszam|nr cont|nomor rekening|số tài khoản|so tai khoan"
    ),
    "holder": (
        "владелец счета|клиент|наименование клиента|"
        "户名|账户名称|客户名称|戶名|"
        "口座名義|名義|"
        "예금주|성명|"
        "اسم الحساب|اسم العميل|صاحب الحساب|"
        "שם החשבון|בעל החשבון|"
        "ชื่อบัญชี|"
        "κατοχος λογαριασμου|"
        "hesap sahibi|müşteri adı|"
        "majitel účtu|majiteľ účtu|název účtu|számlatulajdonos|titular cont|nama pemilik|nama nasabah|chủ tài khoản"
    ),
    "period": (
        "период|за период|"
        "查询期间|账单周期|起止日期|对账期间|"
        "期間|ご利用期間|"
        "조회기간|거래기간|"
        "الفترة|فترة كشف الحساب|"
        "תקופה|"
        "ช่วงเวลา|"
        "περιοδος|"
        "dönem|tarih aralığı|"
        "okres|období|obdobie|időszak|perioada|periode|kỳ"
    ),
    "opening_balance": (
        "входящий остаток|остаток на начало|сальдо на начало периода|"
        "期初余额|上期余额|上日余额|期初結餘|"
        "前回残高|前月繰越|繰越残高|"
        "이월잔액|전일잔액|"
        "الرصيد الافتتاحي|الرصيد السابق|رصيد أول المدة|الرصيد المرحل|"
        "יתרת פתיחה|יתרה קודמת|"
        "ยอดยกมา|"
        "αρχικο υπολοιπο|"
        "devreden bakiye|açılış bakiyesi|"
        "saldo początkowe|saldo poczatkowe|počáteční zůstatek|pociatocny zostatok|nyitó egyenleg|nyito egyenleg|"
        "sold initial|saldo awal|số dư đầu kỳ|so du dau ky"
    ),
    "closing_balance": (
        "исходящий остаток|остаток на конец|сальдо на конец периода|"
        "期末余额|本期余额|当前余额|期末結餘|"
        "今回残高|期末残高|"
        "기말잔액|현재잔액|"
        "الرصيد الختامي|الرصيد النهائي|رصيد آخر المدة|الرصيد الحالي|"
        "יתרת סגירה|יתרה סופית|"
        "ยอดคงเหลือสุดท้าย|"
        "τελικο υπολοιπο|"
        "kapanış bakiyesi|güncel bakiye|"
        "saldo końcowe|saldo koncowe|konečný zůstatek|konecny zostatok|záró egyenleg|zaro egyenleg|"
        "sold final|saldo akhir|số dư cuối kỳ|so du cuoi ky"
    ),
    "total_debits": "借方合计|支出合计|累计支出|итого списаний|итого расход|סך חובה|toplam borç|suma obciążeń|összes terhelés",
    "total_credits": "贷方合计|收入合计|累计收入|итого зачислений|итого приход|סך זכות|toplam alacak|suma uznań|összes jóváírás",
    "account_type": "账户类型|口座種別|계좌종류|نوع الحساب|סוג חשבון|тип счета|hesap türü",
    "statement_number": "对账单编号|выписка номер|رقم الكشف|מספר דף חשבון",
    "statement_date": "对账日期|дата выписки|תאריך הדף|تاريخ الكشف",
    "currency": "币种|货币|валюта|通貨|통화|العملة|מטבע|สกุลเงิน|νομισμα|para birimi|waluta|mena|pénznem|moneda|mata uang|loại tiền",
}

EXTRA_ROW: dict[str, str] = {
    "opening": (
        "входящий остаток|остаток на начало|期初余额|上期余额|上日余额|前回残高|前月繰越|이월잔액|"
        "الرصيد الافتتاحي|رصيد أول المدة|יתרת פתיחה|ยอดยกมา|αρχικο υπολοιπο|devreden bakiye|"
        "saldo początkowe|počáteční zůstatek|nyitó egyenleg|saldo awal|số dư đầu kỳ"
    ),
    "closing": (
        "исходящий остаток|остаток на конец|期末余额|本期余额|今回残高|期末残高|기말잔액|"
        "الرصيد الختامي|رصيد آخر المدة|יתרת סגירה|τελικο υπολοιπο|kapanış bakiyesi|"
        "saldo końcowe|konečný zůstatek|záró egyenleg|saldo akhir|số dư cuối kỳ"
    ),
    "totals": "итого|合计|合計|총계|합계|الإجمالي|المجموع|סה\"כ|รวม|σύνολο|toplam|razem|celkem|spolu|összesen|total general|jumlah|tổng",
    "end": (
        "заявление|免责声明|本对账单|ご注意|안내|ملاحظة|הערות|ข้อควรระวัง|uwaga|upozornění|figyelem|notă"
    ),
}
