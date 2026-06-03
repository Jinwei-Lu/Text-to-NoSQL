"""Offline stub responses for active non-construction LLM entrypoints."""
from __future__ import annotations

import json
from typing import Any

from .llm import Message

_STUB_MQL = 'db.account.aggregate([{"$limit":1}])'


def stub_fn(agent: str, messages: list[Message], schema: dict | None) -> dict[str, Any]:
    """Return canned output for active stubbed LLM callers."""
    if agent == "smart_eg":
        return _smart_eg_stub(messages)
    if agent.startswith("baseline_"):
        return _baseline_stub(agent)
    return {"_stub": True, "agent": agent}


def _smart_eg_stub(messages: list[Message]) -> dict[str, Any]:
    tool_names = [
        str(message.get("name") or "")
        for message in messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    if "submit_final_mql" in tool_names:
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "stub_abandon",
                    "type": "function",
                    "function": {
                        "name": "abandon_with_failure",
                        "arguments": json.dumps(
                            {
                                "error_code": "NO_VALID_QUERY_FOUND",
                                "message": "stubbed SMART-EG run stopped after gate feedback",
                            }
                        ),
                    },
                }
            ],
        }
    if "list_collections" in tool_names:
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "stub_submit",
                    "type": "function",
                    "function": {
                        "name": "submit_final_mql",
                        "arguments": json.dumps(
                            {
                                "collection": "account",
                                "pipeline": [{"$limit": 1}],
                                "MQL": _STUB_MQL,
                                "evidence_refs": ["ev-0001"],
                            }
                        ),
                    },
                }
            ],
        }
    return {
        "content": "",
        "tool_calls": [
            {
                "id": "stub_list_collections",
                "type": "function",
                "function": {
                    "name": "list_collections",
                    "arguments": "{}",
                },
            }
        ],
    }


def _baseline_stub(agent: str) -> dict[str, Any]:
    if agent.endswith("_sql"):
        return {
            "SQL": (
                "SELECT account.* FROM account "
                "LEFT JOIN loan ON loan.account_id = account._id LIMIT 1"
            ),
            "notes": "Relational sketch used only by the SQL pivot baseline.",
        }
    if agent.endswith("_plan"):
        return {
            "target_collection": "account",
            "steps": [
                "Start from account.",
                "Preserve account documents.",
                "Return a bounded result.",
            ],
            "risks": ["Stub mode does not attempt semantic recovery."],
        }
    if agent.endswith("_think"):
        return {
            "thoughts": [
                "The query should be answered against the visible release schema.",
                "Stub mode returns a bounded representative aggregate.",
            ],
            "needed_observations": ["public schema only"],
        }
    return {
        "MQL": _STUB_MQL,
        "rationale": "Deterministic stub MQL for offline baseline plumbing.",
        "assumptions": ["stub mode"],
    }
