"""The statement-level values to extract.

What each is *called* lives in `rules/lexicon.py` (many languages); this file
declares only which values exist, their type, and where they are expected.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HeaderField:
    name: str
    label: str
    #: text | account_number | iban | bic | routing | date | money | currency | id
    value_type: str
    #: Lexicon FIELD concepts whose label introduces this value.
    concepts: tuple[str, ...] = ()
    required: bool = False
    order: int = 0
    #: Which page the value is expected on: first | last | any.
    page_preference: str = "first"


HEADER_FIELDS: tuple[HeaderField, ...] = (
    HeaderField("bank_name", "Bank", "text", (), required=True, order=1),
    HeaderField("account_holder", "Account holder", "text", ("holder",), required=True, order=2),
    HeaderField("account_number", "Account number", "account_number", ("account_number",), required=True, order=3),
    HeaderField("iban", "IBAN", "iban", ("iban",), order=4),
    HeaderField("bic", "BIC / SWIFT", "bic", ("bic",), order=5),
    HeaderField("routing", "Bank / routing code", "routing", ("routing",), order=6),
    HeaderField("branch", "Branch", "text", ("branch",), order=7),
    HeaderField("customer_id", "Customer ID", "id", ("customer_id",), order=8),
    HeaderField("account_type", "Account type", "text", ("account_type",), order=9),
    HeaderField("statement_number", "Statement number", "id", ("statement_number",), order=10),
    HeaderField("statement_date", "Statement date", "date", ("statement_date",), order=11),
    HeaderField("period_from", "Period from", "date", ("period",), required=True, order=12),
    HeaderField("period_to", "Period to", "date", ("period",), required=True, order=13),
    HeaderField("opening_balance", "Opening balance", "money", ("opening_balance",), order=14),
    HeaderField("closing_balance", "Closing balance", "money", ("closing_balance",), order=15, page_preference="last"),
    HeaderField("total_debits", "Total debits", "money", ("total_debits",), order=16, page_preference="last"),
    HeaderField("total_credits", "Total credits", "money", ("total_credits",), order=17, page_preference="last"),
    HeaderField("currency", "Currency", "currency", (), order=18),
)

HEADER_FIELD_BY_NAME: dict[str, HeaderField] = {f.name: f for f in HEADER_FIELDS}
