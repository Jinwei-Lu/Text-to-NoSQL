# TEND build — concurrent-session coordination

Two Claude Code sessions build this construction pipeline **concurrently** in one working tree
(user instruction: "你跟他一起并发构建"). Divide by layer; one rule:

> **Never overwrite a file you did not create.** First writer of a path owns it. Changes to a
> co-owned file (`errors.py`) must be surgical. Flag issues in someone else's file *here*
> rather than rewriting it.

Session B = author of `observability/`, `llm/`, `agents/`, `workflow/`, `construct/`, `execution/`,
`cli.py`, `dataset.py`, `stubs.py`. Session A = author of this file, `mechanisms/`, `source/census.py`,
`source/catalog.py`, and `tests/`.

## Ownership map (live)

| Layer / package | Owner | Status |
|---|---|---|
| `config.py`, `errors.py`, `source/bird.py` | shared (pre-existing) | stable |
| `observability/`, `llm/`, `agents/`, `workflow/`, `construct/migrate.py`, `cli.py`, `dataset.py`, `stubs.py` | **B** | in progress |
| `execution/` (ast_check, mongo, signature) — **canonical execution layer** | **B** | stable; see gaps below |
| `mechanisms/` (detectors, archetypes, reference oracles) | **A** | building |
| `source/census.py`, `source/catalog.py` | **A** | building |
| `tests/` | **A** | building |
| Coverage controller (replacing `cli._slots_for` skeleton) | TBD | A offers `mechanisms.census.plan_coverage_slots`; coordinate before wiring |

> Session A removed its earlier duplicate `core/` and `obs/` packages — `execution/` and
> `observability/` are canonical. No `core/` import exists anywhere.

## What `mechanisms/` exposes (B's QPS/SRA/MS depend on these)

```python
from tend.mechanisms import (
    detect_mechanisms,         # (BirdSource, db_id) -> list[MechanismInstance]  (03 §03-II-10)
    MechanismInstance,
    ARCHETYPES, archetype_for, # closed archetype catalog (04 §04-2-4): mechanism -> archetypes
    reference_oracle,          # (template_name) -> callable(snapshot, params) -> list[dict]  (R for MS gold-lock)
)
from tend.source.census import run_census, plan_coverage_slots   # supply census + slot scheduler
from tend.source.catalog import build_catalog                    # bird_db_catalog.json (02 §02-II-2)
```

Mechanism vocabulary (SSoT-aligned, census_supply.py set): `polymorphic`, `sparse_scalar`,
`sparse_embed`, `dynamic_key`, `versioning`. Alias map handles B's terms:
`optional_embed → sparse_embed`. Archetype ids match B's stubs (e.g. `present_missing_projection`).

## ⚠ Flags for Session B (execution/ correctness, not yet fixed by A — your call)

`execution/mongo.py` is spec-aligned except for two ≡_rec correctness gaps (01 §01-4):

1. **`_normalize_doc` strips `_id` unconditionally** (`if k != "_id"`). Per **01 §01-4-4**, the
   top-level `_id` must be **kept** when it is a semantic `$group` key (e.g.
   `$group:{_id:"$type"}`) — exactly the L4 polymorphic-dispatch / per-subtype-agg records that
   are the benchmark's core. Dropping it makes those gold results lose their group keys and mis-
   compare. Fix: strip `_id` only when the gold pipeline does not give it meaning (a `$group`
   with non-null `_id`, or an explicit `$project _id:1`). A `should_strip_id(gold_mql)` helper.
2. **No int/float unification.** `_doc_key` json-serializes, so gold `1` vs predicted `1.0`
   compare unequal. Per **01 §01-4-1**, integral values should collapse to a single form before
   compare (and use the relative+absolute double-tolerance for non-integral). Recommend a
   `normalize_numeric` in `_normalize_doc` (`1.0 -> 1`; 12-sig-digit float otherwise).

(Reference implementations for both existed in A's removed `core/` and are mirrored in the
`tests/` A is adding; the tests pin these requirements so a fix is verifiable.)

If you prefer A to apply these surgically, say so here and A will.

## Session A — delivered (done, tested) + handoff to B

All zero-LLM, built on `source/bird.py` + `execution/`; **35 tests pass** (`tests/test_mechanisms.py`,
`test_oracles.py`, `test_census.py`, `test_validate.py` + B's `test_pipeline.py`), stub mode.

| Module | What it gives B |
|---|---|
| `mechanisms/detectors.py` | `detect_mechanisms(src, db_id)` → 5-mechanism recovery (financial: 12 query-bearing). SRA Stage B / `__variants`. |
| `mechanisms/archetypes.py` | `ARCHETYPES` (15), `archetypes_for`, `get_archetype`, `normalize_mechanism` (incl. `optional_embed→sparse_embed`). QPS intent enumeration. |
| `mechanisms/oracles.py` | `reference_oracle(template)` → naive R for **all 15** archetypes. MS gold-lock (`NormExec(gold) ≡_rec Norm(R)`). |
| `source/census.py` | `run_census(src)` (=153 L4 cells, matches census_supply.py) + `plan_coverage_slots(census, n_records, seed)` → the real Coverage Controller. |
| `source/catalog.py` | `build_catalog(src)` → `bird_db_catalog.json` (02 §02-II-2). |
| `publish/validate.py` | `validate_record` (C1-C9), `validate_composition` (H1/H4-H9), `validate_release(out_dir, executor=…)`. Publish gate after `dataset.write_*`. |

**Wiring offer 1 — replace `cli._slots_for` skeleton with the census-driven controller:**

```python
from tend.source import BirdSource
from tend.source.census import run_census, plan_coverage_slots
from .workflow.flows import CoverageSlot

def _slots_for(artifacts, n_records):
    with BirdSource(settings.paths.bird_root) as src:          # or reuse the run's source
        reqs = plan_coverage_slots(run_census(src, db_ids=list(artifacts)),
                                   n_records=n_records, seed=settings.seed)
    return [CoverageSlot(db_id=r.db_id, mechanism=r.mechanism, archetype=r.archetype,
                         record_id=1000 + i) for i, r in enumerate(reqs)]
```

This makes coverage hit L4≥30% / L0≤5% / structural≥20% instead of the hardcoded single cell.
QPS receives `mechanism`/`archetype`; `get_archetype(slot.archetype)` gives MS the
`reference_template` for `reference_oracle(...)`, and `shape_policy`/`target_difficulty`.

**Wiring offer 2 — gate the release after writing:**

```python
from tend.publish import validate_release
report = validate_release(out_dir, schemas_dir=settings.paths.schemas, executor=mongo)
log.info("release_validation", ok=report.ok, summary=report.summary())
```

