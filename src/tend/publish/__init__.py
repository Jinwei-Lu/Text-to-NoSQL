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
from .quality import QualityIssue, ReleaseQualityReport, run_release_quality_audit
from .llm_review import LLMReviewSummary, run_llm_nlq_review
from .nlq_rewrite import LLMRewriteSummary, run_llm_nlq_rewrite
from .repair import RepairSummary, apply_builtin_quality_repairs

__all__ = [
    "validate_record",
    "validate_record_jsonschema",
    "validate_composition",
    "validate_release",
    "run_release_quality_audit",
    "run_llm_nlq_review",
    "run_llm_nlq_rewrite",
    "apply_builtin_quality_repairs",
    "CompositionReport",
    "QualityIssue",
    "ReleaseReport",
    "ReleaseQualityReport",
    "LLMReviewSummary",
    "LLMRewriteSummary",
    "RepairSummary",
]
