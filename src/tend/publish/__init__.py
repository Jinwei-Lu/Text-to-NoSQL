"""Release validation.

The native construction package writes release-style artifacts; this package checks
them against the 02 contracts before they are trusted: per-record constraints C1-C9,
JSON-Schema conformance, and the test-composition hard constraints. Pure/deterministic;
an optional MongoExecutor enables the executable C5 check.
"""
from __future__ import annotations

from .validate import (
    CompositionReport,
    ReleaseReport,
    validate_composition,
    validate_record,
    validate_record_jsonschema,
    validate_release,
)

__all__ = [
    "validate_record",
    "validate_record_jsonschema",
    "validate_composition",
    "validate_release",
    "CompositionReport",
    "ReleaseReport",
]
