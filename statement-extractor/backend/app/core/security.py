"""Who is calling.

``AUTH_MODE=none`` (default): a single local user called ``local``. Only for a
trusted machine - anyone who can reach the API can read every statement.

``AUTH_MODE=token``: every request needs ``Authorization: Bearer <token>``;
tokens and their user names come from ``API_TOKENS``. Documents are owned by
the user who uploaded them and are invisible (404) to everyone else.
"""
from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from app.core.config import settings


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "unauthorized", "message": "A valid access token is required.", "params": {}},
        headers={"WWW-Authenticate": "Bearer"},
    )


def user_for_token(token: str) -> str | None:
    found: str | None = None
    for known, user in settings.token_map().items():
        # Compare against every token so timing does not reveal which one matched.
        if hmac.compare_digest(known.encode("utf-8"), token.encode("utf-8")):
            found = user
    return found


def current_user(authorization: str | None = Header(default=None)) -> str:
    if settings.auth_mode == "none":
        return "local"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized()
    user = user_for_token(authorization[7:].strip())
    if user is None:
        raise _unauthorized()
    return user
