# Codex-native Migration and NL-MQL Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Codex-designed MongoDB-native DataWorld mode and a native-feature-driven NL-MQL construction pipeline, while keeping the current deterministic migration as the legacy path.

**Architecture:** Introduce an explicit native mode behind `--construction-mode native`; keep existing behavior as `legacy` and as the default for compatibility. Native mode must not be a generic relational-to-Mongo rule pass. Each BIRD database has a database-specific conversion module that encodes its actual table and field semantics, may call shared native helpers, emits a structured recipe, verifies it deterministically, executes it through a recipe executor, emits native feature/provenance artifacts, and builds Phase B records from the native feature manifest instead of schema-derived slots.

**Tech Stack:** Python dataclasses, YAML/JSON artifacts, existing `LLMAgent` lifecycle, existing `Workflow`, existing `MongoExecutor`, `pytest`, `.venv/bin/python`.

---

## Implementation Order

- [ ] Preserve existing dirty changes in `src/tend/cli.py` and `tests/test_pipeline.py`.
- [ ] Add native recipe and manifest types in `src/tend/construct/native_recipe.py` with focused tests.
- [ ] Add deterministic recipe executor in `src/tend/construct/native_executor.py` with SQLite fixture tests.
- [ ] Add `src/tend/construct/native_designs/` with one explicit conversion module per BIRD database plus a registry. Native mode must fail closed when a database has no registered design.
- [ ] Record `conversion_code_ref` in recipe/provenance artifacts so every native DataWorld can be traced to the module, function, design version, source fields, and derived rules that produced it.
- [ ] Add `native_migration_designer` agent and prompt as a design-assistance path, not as an unreviewed runtime code generator.
- [ ] Add native Phase A orchestration and native artifact writers.
- [ ] Add native Phase B slots, planners, gold compilers, NL generation, verifier, anti-SQL-transfer gate, and record builder.
- [ ] Wire `tend construct --construction-mode native` while preserving legacy default.
- [ ] Extend release validation and record schema for native metadata.
- [ ] Update docs and run compile, targeted tests, legacy regressions, and stubbed native smoke validation.

## Verification Commands

```bash
.venv/bin/python -m compileall src/tend tests
.venv/bin/python -m pytest tests/test_native_recipe.py tests/test_native_executor.py tests/test_native_phase_a.py tests/test_native_phase_b.py tests/test_native_record_builder.py tests/test_native_verify.py tests/test_native_validate.py tests/test_native_construct_cli.py -v
.venv/bin/python -m pytest tests/test_migration.py tests/test_pipeline.py tests/test_validate.py tests/test_cli.py -v
.venv/bin/python -m tend construct --construction-mode native --phase all --dbs financial --records 2 --stub --quiet --run-id native-smoke
.venv/bin/python -m tend validate --dataset-dir runs/native-smoke/dataset --smoke
.venv/bin/python -m tend construct --construction-mode native --phase all --records 1100 --stub --quiet --run-id native-full-11db
.venv/bin/python -m tend validate --dataset-dir runs/native-full-11db/dataset
```

## Defaults

- `--construction-mode legacy` remains the default.
- Native mode is database-design-code-first: Codex can write and update reviewed conversion code, but runtime execution loads checked-in per-database design modules rather than inventing a generic mapping.
- Shared native helpers are allowed, but database-specific modules decide which real tables and fields become polymorphic collections, dynamic keys, attribute bags, derived tags, versioned fields, or nested event streams.
- Unsupported recipe expressions fail closed with `MigrationError`.
- V1 native Phase B supports deterministic compilers for dynamic-key comparison, polymorphic field dispatch, derived tag combination, and nested event filtering.
- The full target dataset requires all 11 BIRD databases and at least 100 NL-MQL records per database.
