"""Tests for the supply census + Coverage Controller (Session A: tend.source.census).

Reads real BIRD mini-dev; skipped if absent. Pins the census structural supply against the
standalone census_supply.py reference (153 L4 cells, 9/11 flex-eligible) and the controller's
composition targets (L4 >= 30%, L0 <= 5%) + determinism.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

_BIRD = Path("minidev/MINIDEV")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _BIRD.exists(), reason="BIRD mini-dev not present"),
]


def _census():
    from tend.source import BirdSource
    from tend.source.census import run_census

    with BirdSource(_BIRD) as src:
        return run_census(src)


@pytest.fixture(scope="module")
def full_census():
    return _census()


def test_census_matches_reference_supply(full_census):
    c = full_census
    assert len(c.databases) == 11
    # reproduces proposals/scripts/census_supply.py structural supply
    assert c.l4_supply_cells == 153
    assert c.ssf_supply_cells == 153
    assert sum(1 for d in c.databases.values() if d.flex_eligible) == 9
    assert not c.supply_relax                      # 9/11 = 0.82 > 0.30
    # the two dbs with no query-bearing structural supply
    assert not c.databases["debit_card_specializing"].flex_eligible
    assert not c.databases["formula_1"].flex_eligible
    assert c.databases["financial"].l4_supply_cells == 25
    assert c.databases["thrombosis_prediction"].l4_supply_cells == 35


def test_coverage_controller_hits_composition_targets(full_census):
    from tend.source.census import plan_coverage_slots

    c = full_census
    slots = plan_coverage_slots(c, n_records=200, seed=0)
    assert len(slots) == 200
    diff = Counter(s.target_difficulty for s in slots)
    assert diff["L4"] / 200 >= 0.30                # H5
    assert diff["L0"] / 200 <= 0.05                # H8
    ssf = sum(1 for s in slots if s.sql_infeasibility_class == "structural_schema_flex")
    assert ssf / 200 >= 0.20                        # H9


def test_coverage_controller_deterministic(full_census):
    from tend.source.census import plan_coverage_slots

    c = full_census
    a = plan_coverage_slots(c, n_records=120, seed=7)
    b = plan_coverage_slots(c, n_records=120, seed=7)
    assert [(s.db_id, s.mechanism, s.archetype) for s in a] == \
           [(s.db_id, s.mechanism, s.archetype) for s in b]
    # different seed -> generally different ordering
    d = plan_coverage_slots(c, n_records=120, seed=8)
    assert a != d or len(c.databases) == 0


def test_coverage_controller_prefers_sparse_embed_for_single_financial_l4():
    from tend.source import BirdSource
    from tend.source.census import plan_coverage_slots, run_census

    with BirdSource(_BIRD) as src:
        c = run_census(src, db_ids=["financial"])
    slots = plan_coverage_slots(c, n_records=1, seed=0)
    assert len(slots) == 1
    assert slots[0].mechanism == "sparse_embed"
    assert slots[0].archetype == "present_missing_projection"
    assert slots[0].target_difficulty == "L4"


def test_structural_controller_maps_full_financial_workload_to_l4_slots():
    from tend.source import BirdSource
    from tend.source.census import plan_source_full_structural_slots, run_census

    with BirdSource(_BIRD) as src:
        c = run_census(src, db_ids=["financial"])
    n = c.databases["financial"].query_count
    slots = plan_source_full_structural_slots(c, n_records=n, seed=0)
    assert len(slots) == 32
    assert all(s.db_id == "financial" for s in slots)
    assert all(s.target_difficulty == "L4" for s in slots)
    assert all(s.sql_infeasibility_class == "structural_schema_flex" for s in slots)
    # DM now materializes __variants for polymorphic discriminators (e.g. trans.type) as well
    # as sparse embeds, so the structural plan spans both mechanisms — this diversifies the
    # schema-less set beyond a single optional-embed structure.
    assert {s.mechanism for s in slots} == {"sparse_embed", "polymorphic"}
    assert {s.archetype for s in slots} == {
        "present_missing_projection",
        "has_vs_absent_compare",
        "per_subtype_agg",
        "subtype_cond_projection",
        "subtype_specific_field",
    }


def test_coverage_controller_penalizes_extreme_sparse_embed_smoke_cells():
    from tend.source import BirdSource
    from tend.source.census import plan_coverage_slots, run_census

    with BirdSource(_BIRD) as src:
        c = run_census(src, db_ids=["financial", "student_club", "thrombosis_prediction"])
    slots = plan_coverage_slots(c, n_records=1, seed=0)
    assert len(slots) == 1
    assert slots[0].mechanism == "sparse_embed"
    assert slots[0].db_id in {"financial", "student_club"}


def test_catalog_build():
    from tend.source import BirdSource
    from tend.source.catalog import build_catalog

    with BirdSource(_BIRD) as src:
        cat = build_catalog(src)
    assert cat["db_count"] == 11
    assert all(e["selected"] for e in cat["databases"])
    fin = next(e for e in cat["databases"] if e["db_id"] == "financial")
    assert fin["domain_id"] == "finance" and fin["flex_eligible"]
