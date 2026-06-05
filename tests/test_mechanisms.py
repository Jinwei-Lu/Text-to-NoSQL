"""Tests for the deterministic heterogeneity layer (Session A: tend.mechanisms).

Self-contained: reads the real BIRD mini-dev via BirdSource and asserts the five-mechanism
detection + archetype catalog invariants. Skipped if the source data is absent.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tend.mechanisms import (
    ARCHETYPES,
    MECHANISMS,
    STRUCTURAL_MECHANISMS,
    archetypes_for,
    detect_mechanisms,
    get_archetype,
    has_oracle,
    normalize_mechanism,
)
from tend.mechanisms.archetypes import _BY_ID

_BIRD = Path("minidev/MINIDEV")
_needs_bird = pytest.mark.skipif(not _BIRD.exists(), reason="BIRD mini-dev not present")


# --------------------------------------------------------------------------- #
# archetype catalog invariants (pure)
# --------------------------------------------------------------------------- #
def test_archetype_catalog_wellformed():
    seen_ids = set()
    for mech, arches in ARCHETYPES.items():
        assert mech in MECHANISMS
        for a in arches:
            assert a.mechanism == mech
            assert a.difficulty in {"L0", "L1", "L2", "L3", "L4"}
            assert a.sql_infeasibility_class in {
                "feasible", "semantic", "performative",
                "structural_pipeline", "structural_schema_flex"}
            assert a.shape_policy in {"preserve", "reshape", "reduce"}
            assert a.id not in seen_ids, f"duplicate archetype id {a.id}"
            seen_ids.add(a.id)


def test_structural_archetypes_are_l4_ssf():
    # structural mechanisms fall out L3-L4 (04 §04-2-4); each has >=1 L4 archetype, and every
    # L4 archetype of a structural mechanism is structural_schema_flex (the census ssf supply).
    for mech in STRUCTURAL_MECHANISMS:
        l4 = [a for a in ARCHETYPES[mech] if a.difficulty == "L4"]
        assert l4, f"structural mechanism {mech} has no L4 archetype"
        for a in l4:
            assert a.sql_infeasibility_class == "structural_schema_flex"


def test_every_archetype_has_a_reference_oracle_or_is_baseline():
    # implemented oracles cover the catalog templates we schedule; flag any gap explicitly
    missing = [a.id for a in _BY_ID.values() if not has_oracle(a.reference_template)]
    assert missing == [], f"archetypes lacking a reference oracle R: {missing}"


def test_mechanism_alias_resolution():
    assert normalize_mechanism("optional_embed") == "sparse_embed"
    assert normalize_mechanism("schema_versioning") == "versioning"
    assert normalize_mechanism("polymorphic") == "polymorphic"
    assert normalize_mechanism("") == "none"
    assert archetypes_for("optional_embed") == ARCHETYPES["sparse_embed"]


def test_get_archetype_unknown_raises():
    with pytest.raises(KeyError):
        get_archetype("not_a_real_archetype")


# --------------------------------------------------------------------------- #
# detectors on real BIRD signal
# --------------------------------------------------------------------------- #
@_needs_bird
@pytest.mark.integration
def test_detect_financial_polymorphic_and_sparse_embed():
    from tend.source import BirdSource

    with BirdSource(_BIRD) as src:
        mechs = detect_mechanisms(src, "financial")
    qb = [m for m in mechs if m.query_bearing]
    kinds = {m.mechanism for m in qb}
    assert "polymorphic" in kinds         # account.frequency / trans.type / ...
    assert "sparse_embed" in kinds        # loan optional embed (present/missing)
    # discriminators must be REAL column names (no synthesized __type / field_a)
    for m in qb:
        if m.mechanism == "polymorphic":
            assert not m.detail["discriminator_col"].startswith(("field_", "variant_", "__"))


@_needs_bird
@pytest.mark.integration
def test_no_synthesis_when_no_signal():
    # debit_card_specializing / formula_1 yield no query-bearing structural mechanism (census)
    from tend.source import BirdSource

    with BirdSource(_BIRD) as src:
        for db in ("debit_card_specializing", "formula_1"):
            mechs = detect_mechanisms(src, db)
            assert not any(m.query_bearing and m.structural for m in mechs), db
