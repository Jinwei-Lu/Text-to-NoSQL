"""Deterministic execution & equivalence layer shared by Phase B agents.

  * :mod:`tend.execution.ast_check` — parse an MQL pipeline string, scan for the 6 banned
    operators at any depth, and evaluate ``canonical_form_set`` (AST_check) + derive a
    thin cfs from a gold pipeline (RAR: idiom-invariants + output guard).
  * :mod:`tend.execution.mongo` — load witness data into a working MongoDB, run an MQL
    aggregate (NormExec), and decide result equivalence ``equiv_rec``.
  * :mod:`tend.execution.signature` — ``world_signature`` over canonicalized witness data.
"""
from __future__ import annotations

from .ast_check import (
    DISABLED_OPERATORS,
    ast_check,
    derive_canonical_form_set,
    parse_pipeline,
    scan_disabled,
)
from .signature import canonical_json, world_signature

__all__ = [
    "DISABLED_OPERATORS",
    "ast_check",
    "derive_canonical_form_set",
    "parse_pipeline",
    "scan_disabled",
    "canonical_json",
    "world_signature",
]
