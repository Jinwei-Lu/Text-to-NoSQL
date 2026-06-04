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
| `observability/_runtime.py`, `observability/_formatters.py` | File-first JSONL logging, anomalies, transcripts, diagnostics |
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
| `solver/` | Provider-native SMART-EG runtime, NLQ+DB input derivation, MongoDB introspection tools, evidence gates, final sanity execution, and typed failures |
| `baselines/` | Constrained LLM baseline runtime, public record/schema sanitizers, baseline disclosure, and disjointness checks |
| `ablations/` | SMART-EG mechanism toggles plus low/medium/high budget profile sweeps |
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

## Solver And Experiment Runtime

SMART-EG is implemented under `src/tend/solver/eg/` as a provider-native tool-call
loop. Release-record mode is a shim over the same NLQ+DB path: it extracts the
selected NLQ, preloads witness data into MongoDB when needed, and then passes only
`NLQ`, `db_id`, and `record_id` into the agent. The runtime does not load
`proposals/schemas/solver_allow_list.json` as SMART-EG configuration, and it does
not load the inactive SMART proposal prompt files.

The successful exit path is `submit_final_mql`. The execution stage can use
`run_readonly_probe`, `check_ast_filter`, and bounded `run_final_sanity_execution`
before final submission. Prefix-pipeline tool names remain visible as a policy and
ablation exposure, but current implementations return `TOOL_UNIMPLEMENTED`; when
the `smart_eg_no_prefix_execution` ablation disables that exposure, they return
`tool_not_exposed`. Prefix execution should therefore be described only as an
unsupported or archival Proposal 06 design, not as a production success mechanism.

`tend baseline` runs the maintained baselines under `src/tend/baselines/`. It
sanitizes records and schemas before prompt construction, records
`baseline_public_schema_v1`, and reports disclosure fields such as
`uses_gold_mql=false`, `uses_execution_feedback=false`,
`schema_sanitizer_applied=true`, `record_sanitizer_applied=true`, stripped-field
lists, and disjointness details from `solver_allow_list.json`.

`tend ablation` runs the SMART-EG ablation registry. The registry includes full
SMART-EG, evidence-gate, counterexample, value-grounding, relationship-probe,
prefix-exposure, revisit, and low/medium/high budget-profile variants. It emits
both predictions and typed ablation failures, then automatic evaluation scores both
artifact types when a release dataset is available.

`tend evaluate` writes proposal-05 metrics to `per_record_metrics.jsonl`,
`per_record_metrics.csv`, `report.json`, and `report.md`. Missing predictions and
typed `solver_failure`, `baseline_failure`, or `ablation_failure` rows are preserved
as zero-score per-record rows, so failures produce partial reports instead of
silently dropping records.

Automatic evaluation is skipped only when there is no release evaluation target,
for example `solve --nlq`, `baseline --nlq`, or `ablation --nlq`. The CLI prints
that as `NLQ+DB mode has no release evaluation dataset`, distinct from `--no-eval`
and from `no predictions`.

## Runtime Prompts

`proposals/agent_prompts/` is mixed. The active `LLMAgent` registry currently loads
only the native construction helper prompts:

- `native_migration_designer.md`
- `native_nl_generator.md`

The SMART prompt templates in that directory are retained as proposal/test assets
until a runtime registers them again:

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
