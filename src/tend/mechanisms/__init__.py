"""Heterogeneity mechanisms, archetype catalog, and reference oracles (deterministic).

The deterministic engine the construction agents stand on (Session A; see COORDINATION.md):

* :mod:`tend.mechanisms.detectors` — the five DAR mechanisms recovered from *real* BIRD signal
  (03 §03-II-10): polymorphic, sparse_scalar, sparse_embed, dynamic_key, versioning. No
  synthesis fallback; discriminators are real column names; a mechanism counts only if it is
  query-bearing (referenced by the real workload SQL).
* :mod:`tend.mechanisms.archetypes` — the closed archetype catalog (04 §04-2-4): each
  ``mechanism × question-shape`` entry carries its falls-out difficulty + sql_infeasibility_class
  and names a reference-oracle template. QPS enumerates intents over this catalog.
* :mod:`tend.mechanisms.oracles` — naive, auditable reference implementations R (04 §04-2-4):
  independent Python that *defines* the answer for MS gold-lock (``NormExec(gold) ≡_rec R``).

Everything here is zero-LLM and reads BIRD via :class:`tend.source.BirdSource`.
"""
from __future__ import annotations

from .archetypes import (
    ARCHETYPES,
    MECHANISM_ALIASES,
    MECHANISMS,
    STRUCTURAL_MECHANISMS,
    Archetype,
    archetypes_for,
    get_archetype,
    normalize_mechanism,
)
from .detectors import MechanismInstance, detect_mechanisms
from .oracles import OracleError, has_oracle, reference_oracle

__all__ = [
    "MECHANISMS",
    "STRUCTURAL_MECHANISMS",
    "MECHANISM_ALIASES",
    "Archetype",
    "ARCHETYPES",
    "archetypes_for",
    "get_archetype",
    "normalize_mechanism",
    "MechanismInstance",
    "detect_mechanisms",
    "reference_oracle",
    "has_oracle",
    "OracleError",
]
