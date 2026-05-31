from __future__ import annotations

from typing import Any

from tend.errors import BOT

from .mql import parse_ok


def Parse(q: str) -> dict | BOT:
    """Parse MQL shell query into AST-like dict or BOT on failure."""
    if not parse_ok(q):
        return BOT("parse_error")
    return {"raw": q, "ok": True}


def disabled_operator_scanner(q: str) -> bool:
    """Return True if query contains a forbidden operator."""
    from .mql import contains_forbidden_operator

    return contains_forbidden_operator(q)
