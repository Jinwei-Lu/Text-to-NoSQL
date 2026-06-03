"""Heterogeneity mechanisms, archetype catalog, and reference oracles.

This package preserves deterministic mechanism/archetype utilities and reference
oracles used by historical audits and evaluation helpers. Active native construction
does not depend on these utilities as a generic migration fallback.

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
from .oracles import OracleError, has_oracle, oracle_param_errors, reference_oracle

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
    "oracle_param_errors",
    "OracleError",
]
