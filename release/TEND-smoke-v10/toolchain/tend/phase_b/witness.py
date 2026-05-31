"""Witness preparation for Phase B execution (MS, RA, PV)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from tend.phase_a.dm import ensure_unique_document_ids


def prepare_witness_for_exec(
    snapshot: dict[str, Any],
    *,
    max_docs: int | None = None,
) -> dict[str, Any]:
    """Dedupe ``_id`` collisions and optionally cap per-collection doc count."""
    prepared = deepcopy(snapshot)
    for key, val in prepared.items():
        if not isinstance(val, list):
            continue
        docs = ensure_unique_document_ids(val)
        if max_docs is not None:
            docs = docs[:max_docs]
        prepared[key] = docs
    return prepared
