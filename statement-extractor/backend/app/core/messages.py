"""Stable message codes for everything the backend tells a user.

Every warning, issue, reason and check detail is a ``Message``: a stable
``code`` the frontend translates, the ``params`` it interpolates, and an English
``text`` kept as the fallback (and for API clients that do not translate).
Codes never change meaning; add a new code rather than reword an old one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any


def _plain(value: Any) -> Any:
    """JSON-safe parameter values: money stays an exact decimal string."""
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@dataclass(frozen=True)
class Message:
    code: str
    text: str
    params: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "params": {k: _plain(v) for k, v in self.params.items()}, "text": self.text}


def message(code: str, text: str, /, **params: Any) -> Message:
    # Positional-only, so a parameter may itself be called "text" or "code".
    return Message(code, text, params)


def add_message(texts: list[str], codes: list[dict], msg: Message) -> None:
    """Append a message to parallel (text, code) lists, once."""
    entry = msg.as_dict()
    if entry not in codes:
        codes.append(entry)
        texts.append(msg.text)


def money_text(value: Decimal | None) -> str:
    """English rendering used only in fallback texts; params carry the exact value."""
    if value is None:
        return "-"
    return f"{value:,.2f}"
