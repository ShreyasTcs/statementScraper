"""SIMULATED language-model answers for tests.

These are NOT recorded Groq responses. No Groq API key was available while this
project was built, so nothing could be recorded. Instead, ``oracle`` plays the
part of an ideal model: it reads the numbered lines in the prompt and, using the
synthetic statement's ground truth, returns exactly the JSON the schema asks
for. ``mutate`` hooks then damage that answer the way a real model might
(invented amounts, dropped rows, wrong citations ...), which is what the tests
are about: the deterministic layer must catch it.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.services.llm_client import LLMResponse, response_json

LINE = re.compile(r"^(CONTEXT )?(p\d+\.l\d+): (.*)$")
LABELS = {
    "Account Holder": "account_holder", "Account Number": "account_number", "Statement Period": None,
    "Opening Balance": "opening_balance", "Closing Balance": "closing_balance",
    "Total Debits": "total_debits", "Total Credits": "total_credits",
}


def prompt_lines(user: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    own, context = [], []
    for raw in user.splitlines():
        m = LINE.match(raw)
        if m:
            (context if m.group(1) else own).append((m.group(2), m.group(3)))
    return own, context


DAY = re.compile(r"\d{2}/\d{2}/\d{4}")


def row_cells(text: str) -> list[str] | None:
    """A transaction row as [date, description, amounts...], from a layout line (cells two or more spaces apart)
    or a Markdown table row (cells between unescaped '|'; empty cells dropped). A value date after the date is
    skipped. None for anything else."""
    if text.startswith("|"):
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", text.strip().strip("|"))]
        cells = [c for c in cells if c]
    elif re.match(r"\d{2}/\d{2}/\d{4}\s{2,}", text):
        cells = [c.strip() for c in re.split(r"\s{2,}", text.strip())]
    else:
        return None
    if len(cells) < 2 or not DAY.fullmatch(cells[0]):
        return None
    rest = cells[1:]
    while len(rest) > 1 and DAY.fullmatch(rest[0]):
        rest = rest[1:]
    return [cells[0], *rest]


def empty_payload() -> dict[str, Any]:
    return {"accounts": [], "order": "unknown", "transactions": [], "balance_lines": [], "totals": [],
            "empty_notice_lines": [], "skipped": []}


def oracle(truth: dict, mutate: Callable[[dict, list[tuple[str, str]]], dict] | None = None,
           **response_kwargs) -> Callable[[str, str], LLMResponse]:
    rows = truth["rows"]

    def answer(_system: str, user: str) -> LLMResponse:
        own, _ = prompt_lines(user)
        known = "Known accounts: none yet." not in user
        ref = "A1" if known else "NEW1"
        payload = empty_payload()
        fields: list[dict] = []
        for line_id, text in own:
            for label, name in LABELS.items():
                if text.startswith(label + ":"):
                    value = text.split(":", 1)[1].strip()
                    if label == "Statement Period":
                        start, _, end = value.partition(" to ")
                        fields += [{"name": "period_from", "value": start, "lines": [line_id]},
                                   {"name": "period_to", "value": end, "lines": [line_id]}]
                    elif name:
                        if "Balance" in label or "Total" in label:
                            value = value.split()[0]
                        fields.append({"name": name, "value": value, "lines": [line_id]})
            if text.startswith(("Northwind", "Contoso")):
                fields.append({"name": "bank_name", "value": text.strip(), "lines": [line_id]})
            if "No transactions in this period" in text:
                payload["empty_notice_lines"].append(line_id)
            cells = row_cells(text)
            if cells is None:
                continue
            day, description, amounts = cells[0], cells[1], cells[2:]
            iso = f"{day[6:10]}-{day[3:5]}-{day[0:2]}"
            truth_row = next((r for r in rows if r["date"] == iso and r["description"] == description), None)
            if truth_row is None:
                continue
            has_balance = truth_row["balance"] is not None and len(amounts) >= 2
            movement = amounts[0] if amounts else None
            payload["transactions"].append({
                "account": ref, "date": day, "value_date": None, "description": description, "reference": None,
                "debit": movement if truth_row["debit"] else None,
                "credit": movement if truth_row["credit"] else None,
                "amount": None, "drcr_marker": None,
                "balance": amounts[-1] if has_balance else None, "currency": None, "lines": [line_id],
            })
        if fields or not known:
            payload["accounts"].append({"ref": ref, "currency": "EUR", "fields": fields})
        payload["order"] = "oldest_first"
        if mutate is not None:
            payload = mutate(payload, own)
        return response_json(payload, **response_kwargs)

    return answer
