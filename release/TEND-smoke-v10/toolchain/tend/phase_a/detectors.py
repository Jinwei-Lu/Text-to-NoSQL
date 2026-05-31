"""Audit-only phenomenon detectors (stub implementations)."""

from __future__ import annotations

from typing import Any

from tend.core import logging as log_module
from tend.core.signatures import detector_signature


DETECTOR_VERSION = "0.1.0-stub"


def scan_phenomena(db_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """Read-only stub scan over witness data."""
    log_module.emit("detectors.scan", db_id=db_id, agent="detectors", stage="phase_a", stub=True)
    return {
        "db_id": db_id,
        "detector_signature": detector_signature("phenomena_stub", DETECTOR_VERSION),
        "detectors": {
            "sparse_field": {"status": "stub", "findings": []},
            "type_drift": {"status": "stub", "findings": []},
            "outlier_value": {"status": "stub", "findings": []},
            "cardinality_boundary": {"status": "stub", "findings": []},
            "empty_vs_missing": {"status": "stub", "findings": []},
        },
        "document_count": sum(len(docs) for docs in data.values()),
    }
