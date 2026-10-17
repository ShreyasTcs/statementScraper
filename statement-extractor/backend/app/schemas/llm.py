"""What the language model must return for one part ("chunk") of a statement.

The model only *locates and copies*: every value is the text exactly as printed,
with the IDs of the source lines it came from. Parsing (dates, amounts, signs),
arithmetic and every decision about completeness stay deterministic and happen
afterwards in Python, where each value is checked against the cited lines.

``strict_schema()`` turns the Pydantic model into the JSON Schema form Groq's
strict structured outputs accept: every property required, no additional
properties, no ``$ref`` indirection, no titles or defaults.
"""
from __future__ import annotations

import copy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1"

FieldName = Literal[
    "bank_name", "account_holder", "account_number", "iban", "bic", "routing", "branch",
    "customer_id", "account_type", "statement_number", "statement_date", "period_from",
    "period_to", "opening_balance", "closing_balance", "total_debits", "total_credits", "currency",
]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMField(_Strict):
    name: FieldName
    value: str = Field(description="The value exactly as printed. Do not reformat, translate or compute.")
    lines: list[str] = Field(description="IDs of the lines the value was read from, e.g. p1.l4.")


class LLMAccount(_Strict):
    ref: str = Field(description="Reuse a known account id (A1, A2 ...) or use NEW1, NEW2 ... for an account first seen in this part.")
    currency: str | None = Field(description="ISO 4217 code if printed or shown by an unambiguous symbol, else null.")
    fields: list[LLMField]


class LLMTransaction(_Strict):
    account: str = Field(description="ref of the account section the row is printed in.")
    date: str | None = Field(description="Transaction / booking date exactly as printed, else null.")
    value_date: str | None = Field(description="Only if a separate value date is printed for the row, else null.")
    description: str | None = Field(description="Narration exactly as printed; join wrapped lines with a space.")
    reference: str | None
    debit: str | None = Field(description="Amount printed in a debit / withdrawal / money-out column, exactly as printed.")
    credit: str | None = Field(description="Amount printed in a credit / deposit / money-in column, exactly as printed.")
    amount: str | None = Field(description="Only for a table with a single amount column: the amount exactly as printed including any sign or Dr/Cr marker. Null when the amount is in a debit or credit column.")
    drcr_marker: str | None = Field(description="A debit/credit marker printed in its own column (Dr, Cr, D, C, S, H ...), else null.")
    balance: str | None = Field(description="Running balance printed on the row, exactly as printed, else null.")
    currency: str | None = Field(description="ISO code only if the row itself prints a currency, else null.")
    lines: list[str] = Field(description="IDs of every line of this row, including wrapped narration lines.")


class LLMBalanceLine(_Strict):
    account: str
    kind: Literal["opening", "closing", "brought_forward", "carried_forward", "other"]
    amount: str = Field(description="Exactly as printed.")
    date: str | None
    lines: list[str]


class LLMTotals(_Strict):
    account: str
    total_debits: str | None = Field(description="Printed total of debits, exactly as printed, else null.")
    total_credits: str | None = Field(description="Printed total of credits, exactly as printed, else null.")
    lines: list[str]


class LLMSkipped(_Strict):
    line: str = Field(description="ID of a line that looks like a transaction but could not be interpreted.")
    reason: str = Field(description="A few words, no document content.")


class LLMChunk(_Strict):
    accounts: list[LLMAccount]
    order: Literal["oldest_first", "newest_first", "unknown"]
    transactions: list[LLMTransaction]
    balance_lines: list[LLMBalanceLine]
    totals: list[LLMTotals]
    empty_notice_lines: list[str] = Field(description="IDs of lines stating that there are no transactions in the period.")
    skipped: list[LLMSkipped]


def strict_schema(model: type[BaseModel] = LLMChunk) -> dict[str, Any]:
    """JSON Schema for Groq strict mode (all required, closed objects, inlined refs)."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                merged = copy.deepcopy(defs[name])
                merged.update({k: v for k, v in node.items() if k != "$ref"})
                return resolve(merged)
            out: dict[str, Any] = {}
            for key, value in node.items():
                if key in ("title", "default"):
                    continue
                out[key] = resolve(value)
            if out.get("type") == "object" and "properties" in out:
                out["required"] = list(out["properties"].keys())
                out["additionalProperties"] = False
            return out
        if isinstance(node, list):
            return [resolve(v) for v in node]
        return node

    return resolve(raw)
