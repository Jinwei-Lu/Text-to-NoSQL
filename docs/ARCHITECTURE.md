# TEND Construction Pipeline - Architecture

TEND currently has one dataset construction path: MongoDB-native DataWorld
construction under `src/tend/construction/`. The previous legacy relation-to-document
migration flow has been removed from runtime, CLI, public imports, and active tests.

## Layout (`src/tend/`)

| Module | Role |
|---|---|
| `config.py` | `.env` loading, paths, OpenAI-compatible provider configuration, toggles |
| `errors.py` | Typed exception taxonomy and `Anomaly` classification |
| `source/bird.py` | BIRD mini-dev loader: schema, workload, column enums, SQLite probes |
| `observability/logging.py` | File-first JSONL logging, anomalies, transcripts, diagnostics |
| `observability/progress.py` | Live progress tree plus `progress.jsonl` snapshots |
| `llm/client.py` | Async OpenAI-compatible client, schema repair loop, transcripts, stub mode |
| `agents/base.py` | `Agent` lifecycle wrapper, `LLMAgent`, and registry |
| `agents/native_migration.py`, `agents/native_nl.py` | Optional native construction helper agents for reviewed recipe design and NL wording |
| `construction/phase_a.py` | Native Phase A orchestration and `NativeDbArtifacts` |
| `construction/phase_b.py` | Manifest-driven coverage planning and deterministic gold-MQL compilers |
| `construction/verify.py` | Native record verification and anti-SQL-transfer classification |
| `construction/recipe.py` | Typed native recipe, feature manifest, provenance, and validation contracts |
| `construction/executor.py` | Deterministic recipe executor for recipe-backed designs |
| `construction/audit.py` | Structural audits for native DataWorld materializers |
| `construction/designs/` | One database-specific MongoDB-native design module per supported BIRD db |
| `construction/artifacts.py` | Dataset artifact writers for schemas, data, manifests, provenance, and records |
| `workflow/engine.py` | Generic `Workflow`: `agent`, `parallel`, and `pipeline` primitives |
| `execution/` | MQL parsing, banned-operator scan, signatures, Mongo execution, world signatures |
| `publish/` | Release validation against record, schema, artifact, and composition contracts |
| `solver/`, `baselines/`, `ablations/` | Evaluation-time solvers and experiment runners |
| `cli.py` | Runtime assembly and command dispatch |

## Construction CLI

```bash
# native smoke, offline
python -m tend construct --phase all --dbs financial --records 2 --stub --quiet --run-id native-smoke
python -m tend validate --dataset-dir runs/native-smoke/dataset --smoke

# native Phase A only
python -m tend construct --phase A --dbs financial

# all registered BIRD mini-dev databases
python -m tend construct --phase all --dbs all --records 20

# full benchmark-style target: all 11 dbs, fixed records per db
python -m tend construct --phase all --dbs all --records-per-db 100 --stub --quiet --run-id native-full-11db
python -m tend validate --dataset-dir runs/native-full-11db/dataset
```

`tend construct` is native-only. There is no `--construction-mode`, `--full-db`, or
`--structural-fraction` flag. `--records all` resolves to the selected BIRD workload
count. `--phase B` requires Phase A artifacts in the same process and fails closed
when they are absent; the CLI intentionally does not implement disk resume.

Everything for a run lands under `runs/<run_id>/`; by default construction writes
dataset assets under `runs/<run_id>/dataset/`:

- `mongodb_schema/`
- `mongodb_data/`
- `agent_design_rationale/`
- `migration_recipe/`
- `native_feature_manifest/`
- `provenance/`
- `bird_db_catalog.json`
- `test.json`
- `TEND.json`

## Native Phase A

Native Phase A is database-design-code-first. Each module under
`src/tend/construction/designs/` encodes the actual semantics of one BIRD database:
tables, fields, foreign keys, source workloads, value distributions, and domain
concepts. Shared helpers are allowed, but the design module decides which real source
fields become MongoDB-native structures such as:

- polymorphic collections
- dynamic key objects
- derived tag arrays
- nested event streams
- attribute bags
- versioned fields
- missing-vs-present structures

Registered modules are listed in `construction/designs/registry.py`. Selecting an
unregistered `db_id` raises a construction error rather than falling back to a generic
migration. `provenance/<db_id>.json` records `conversion_code_ref` values such as
`tend.construction.designs.financial`, linking artifacts back to their exact code and
source-column lineage.

## Native Phase B

Native Phase B reads `NativeFeatureManifest` objects produced by Phase A and plans
coverage slots directly from native features. Gold MQL is compiled deterministically
from feature type and query pattern, then structurally verified before record output.
The main feature families are:

- dynamic key comparison
- subtype field dispatch
- tag combination logic
- nested event filtering
- missing-vs-present expressions

Records include native metadata such as `native_feature_id`, `native_query_pattern`,
`mongo_native_constructs`, `anti_sql_transfer_level`, `provenance_refs`, and
`migration_recipe_ref`.

## Runtime Prompts

The active prompt directory `proposals/agent_prompts/` is a runtime asset directory,
not part of the proposal narrative. It contains only prompts still loaded by runtime
code:

- `native_migration_designer.md`
- `native_nl_generator.md`
- `smart_intent_formalizer.md`
- `smart_nosql_planner.md`

## Logging And Anomaly Capture

Triage starts at `runs/<run_id>/anomalies.jsonl`. Every line is a structured anomaly
with kind, message, bound context, and, for LLM faults, a transcript reference under
`runs/<run_id>/llm/<agent>/<call_id>.md` plus a diagnostics JSON sidecar. `events.jsonl`
is the complete event stream; `progress.jsonl` stores progress snapshots even when
`--quiet` disables the live UI.

## Status

The active construction stack supports all 11 BIRD mini-dev databases through
database-specific native design modules and manifest-driven Phase B. The repository's
formal dataset release, statistics, provenance, and audit evidence are tracked under
`release/tend-native-mongodb-v1/`. `runs/` is local generation evidence only; large
MongoDB witness data is referenced through the artifact notes rather than stored
directly in Git.
