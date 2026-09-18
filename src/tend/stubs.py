"""Offline stub responses for active non-construction LLM entrypoints."""
from __future__ import annotations

import re
from typing import Any

from .llm import Message

_STUB_MQL = 'db.account.aggregate([{"$limit":1}])'


def stub_fn(agent: str, messages: list[Message], schema: dict | None) -> dict[str, Any]:
    """Return canned output for active stubbed LLM callers."""
    if agent.startswith("sag_"):
        return _sag_stub(messages)
    # Exact-match the react baseline before the generic baseline_ startswith branch
    # so its action stub is not shadowed by the JSON-output baseline stub.
    if agent == "baseline_react_informed":
        return {"action": "submit", "collection": "account", "pipeline": [{"$limit": 1}]}
    if agent.startswith("baseline_"):
        return _baseline_stub(agent)
    return {"_stub": True, "agent": agent}


_SAG_COLLECTIONS_RE = re.compile(r"collections: \['([^']+)'")


def _sag_stub(messages: list[Message]) -> dict[str, Any]:
    """Deterministic SAG decode: pick the first listed collection, bounded pipeline.

    The SAG system prompt enumerates the induced collections as
    ``... has EXACTLY these N collections: ['a', 'b', ...]``; the stub must return
    a member of that enum so the strict response schema validates offline.
    """
    collection = "account"
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue
        m = _SAG_COLLECTIONS_RE.search(str(message.get("content") or ""))
        if m:
            collection = m.group(1)
            break
    return {"collection": collection, "pipeline": [{"$limit": 1}]}


def _baseline_stub(agent: str) -> dict[str, Any]:
    if agent.endswith("_sql"):
        return {
            "SQL": (
                "SELECT account.* FROM account "
                "LEFT JOIN loan ON loan.account_id = account._id LIMIT 1"
            ),
            "notes": "Relational sketch used only by the SQL pivot baseline.",
        }
    if agent.endswith("_link"):
        return {
            "collections": ["account"],
            "paths": ["account._id"],
            "id_links": [],
        }
    if agent.endswith("_classify"):
        return {"label": "easy", "sub_questions": []}
    return {
        "MQL": _STUB_MQL,
        "rationale": "Deterministic stub MQL for offline baseline plumbing.",
        "assumptions": ["stub mode"],
    }
