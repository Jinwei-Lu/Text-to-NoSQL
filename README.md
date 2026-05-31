# TEND

TEND is a Text-to-NoSQL benchmark construction workspace for MongoDB. The current construction contract is BIRD mini-dev anchored, test-only, and proposal-driven: BIRD provides real relational data, column descriptions, and NL/SQL workload signals; TEND derives MongoDB records through DAR/RAR agents and validates gold MQL against reference results.

The checked-in proposal fixtures are **smoke fixtures**, not a production release. `release/` is currently the release target directory, but the canonical release artifact is not implied by the smoke fixtures under `proposals/fixtures/` or by `runs/codex-smoke-*`.

## Current Contracts

- Source workload: `minidev/MINIDEV` BIRD mini-dev data. Schema-facing outputs use `source_version`; smoke examples use `source_version: "smoke-fixture"`.
- QPS output: active records use `intent` plus `qps_trace`. The older `query_plan` schema is archival/non-active.
- Shape policy: `preserve`, `reshape`, or `reduce`.
- Canonical form set: thin RAR guard only. It carries disabled tokens, unavoidable idiom-invariant operators such as `$lookup`, and shape guards; it does not lock replaceable idioms such as `$addFields`, `$cond`, or `$type`.
- Disabled tokens: `$sample`, `$rand`, `$$NOW`, `$out`, `$merge`, `$function`.
- RTV: result-level equivalence only. Canonical round-trip must satisfy `NormExec(round_trip, D) == NormExec(gold, D)`.
- NNC: SQL/NoSQL bridge checks are pure result gates. `ast_check_pass` is diagnostic, not the bridge pass condition.
- CFS smoke fixtures and proposal docs describe smoke data as smoke only; production release records must come from the construction pipeline and release validation.

## Layout

```text
minidev/MINIDEV/             BIRD mini-dev source data
proposals/                   Current design docs, agent prompts, schemas, and smoke fixtures
proposals/agent_prompts/     Construction-agent prompt contracts
proposals/schemas/           JSON Schemas and valid/invalid fixtures
proposals/fixtures/          Smoke fixtures used for schema/prompt checks
src/tend/                    Runtime construction, validation, and solver code
tests/                       Runtime and schema regression tests
runs/                        Local run outputs
release/                     Release-output target directory
```

## Common Checks

Install the package in a Python 3.11+ environment, then run targeted checks from the repository root:

```bash
python -m pytest tests/test_validate.py
```

For proposal-only schema fixture checks, validate the JSON fixtures under `proposals/schemas/` against their paired schemas. The checked-in smoke fixtures are intentionally small and should not be described as production release data.

## Construction Entry Point

The runtime CLI is exposed as `tend` after installation and can also be run as a module:

```bash
python -m tend --help
```

Use relative paths such as `minidev/MINIDEV`, `runs/<run_id>`, and `release/TEND-dataset` in docs and examples. Avoid machine-local absolute paths in checked-in prompts, schemas, and proposal docs.
