# TEND

TEND is a Text-to-NoSQL benchmark construction and evaluation workspace for
MongoDB. It builds MongoDB-style document worlds from BIRD mini-dev relational
databases, generates natural-language-to-MQL benchmark records through a
multi-agent construction pipeline, validates candidate releases against the
project contracts, and includes a SMART reference solver for released records.

The repository is intentionally both a research artifact and an executable
pipeline:

- `src/tend/` contains the active construction, validation, execution, logging,
  and solver code.
- `proposals/` contains the methodology documents, prompt contracts, JSON
  Schemas, smoke fixtures, and release criteria that the runtime implements.
- `baselines/` contains legacy and reproduction-oriented baseline scripts for
  zero-shot, ICL, RAG, self-debugging, and SQL-to-NoSQL conversion experiments.

The checked-in fixtures under `proposals/fixtures/` and `tests/fixtures/` are
smoke fixtures. They are useful for contract and plumbing checks, but they are
not a production benchmark release. A production release is expected to be built
by the construction pipeline and pass `tend publish` validation.

## What TEND Builds

TEND targets MongoDB queries that are hard to obtain by mechanically translating
SQL. The construction pipeline is anchored in real BIRD mini-dev schemas,
foreign keys, column descriptions, SQLite data, and natural-language/SQL
workloads. It then produces:

- per-database MongoDB schemas under `mongodb_schema/`;
- witness MongoDB data under `mongodb_data/`;
- agent design rationale under `agent_design_rationale/`;
- benchmark records in `test.json` and `TEND.json`;
- a `bird_db_catalog.json` source catalog;
- structured run logs, anomaly streams, and LLM transcripts under `runs/`.

Each record is intended to include a canonical and colloquial NLQ, locked gold
MQL, difficulty, SQL-infeasibility class, shape policy, schema-flex metadata,
world signature, and a thin `canonical_form_set` guard.

## Architecture

The active runtime is a Python package named `tend`.

| Area | Role |
| --- | --- |
| `tend.config` | Loads `.env`, resolves paths, configures OpenAI-compatible LLM settings, MongoDB URI, stub mode, concurrency, and run ids. |
| `tend.source` | Loads BIRD mini-dev schemas, workloads, descriptions, SQLite probes, census data, and source catalogs. |
| `tend.mechanisms` | Detects query-bearing heterogeneity mechanisms and maps them to archetypes and reference oracles. |
| `tend.construct` | Deterministically migrates relational BIRD tables into document-aggregate MongoDB witness data. |
| `tend.agents` | Defines the agent lifecycle, LLM agent base class, Phase A agents, Phase B agents, and deterministic verifier agents. |
| `tend.workflow` | Provides the dynamic workflow engine: concurrency-limited `agent`, `parallel`, `pipeline`, Phase A, and Phase B flows. |
| `tend.execution` | Parses MQL, scans disabled operators, derives canonical form sets, loads/runs MongoDB witnesses, normalizes results, and computes world signatures. |
| `tend.publish` | Validates release records, schema fixtures, required files, and test-set composition constraints. |
| `tend.solver` | Implements the SMART reference solver with solver-visible boundary guards, staged contracts, per-stage execution checks, and typed failures. |
| `tend.observability` | Writes file-first JSONL logs, anomalies, markdown LLM transcripts, diagnostics JSON, and optional rich progress UI. |

For a deeper module-level view, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Construction Pipeline

The construction workflow has two phases.

### Phase A: DataWorld Construction

Phase A runs per BIRD database:

1. `WP` profiles the real workload and summarizes access patterns.
2. `SRA` records document-design rationale from the workload and schema.
3. `DM` deterministically derives the document-aggregate layout from real FK
   cardinalities, materializes witness documents, computes `world_signature`,
   and loads MongoDB when available.
4. `SC` reviews the materialized schema/data and query-bearing evidence. A
   reject can trigger a bounded SRA revision loop.

DM is deterministic and authoritative for materialized schema/data. LLM output
can provide rationale and review context, but it does not override the actual
DM witness.

### Phase B: NL-MQL Record Construction

Phase B runs one pipeline per coverage slot:

1. `QPS` enumerates a concrete intent for a detected mechanism/archetype cell.
2. `MS` synthesizes candidate gold MQL and gold-locks it with deterministic
   execution checks plus a reference oracle.
3. `MUT` generates plausible wrong mutations.
4. `PV` verifies that enough mutations are discriminating.
5. `NLP` writes canonical and colloquial natural-language queries.
6. `RTV` independently translates the canonical NLQ back to MQL and checks
   result equivalence.
7. `NNC` assigns difficulty and SQL-infeasibility class.
8. `RA` checks non-triviality against the witness.

The workflow uses bounded retries for known feedback loops, including SC->SRA,
MS gold-lock retry, PV->MUT, RTV->NLP, and RA/NNC follow-up paths. In stub mode,
LLM calls use canned outputs and execution-dependent checks are designed to be
offline-friendly so the whole control flow can be exercised without API calls.

## SMART Solver

The `tend solve` command runs the SMART schema-less reference solver against a
release-style dataset. It intentionally separates solver-visible information
from construction gold:

1. Shape comprehension probes the public schema per collection and reduces the
   result into a shape model.
2. Intent formalization converts the NLQ into a logical specification.
3. NoSQL planning produces a physical MongoDB plan with variant-handling notes.
4. Query realization renders MQL, checks disabled operators, and executes
   prefixes per stage when a local MongoDB executor is available.

The solver boundary removes forbidden gold/audit fields such as `MQL`,
`canonical_form_set`, and `*_ref` values before prompts are built. Solver
failures are returned as typed `solver_failure` JSONL records rather than dummy
queries.

## Repository Layout

```text
src/tend/                    Active Python package
tests/                       Runtime, validation, solver, and contract tests
docs/                        Architecture notes for the active runtime
proposals/                   Methodology docs, agent prompts, schemas, smoke fixtures
proposals/agent_prompts/     Prompt contracts used by LLM-backed agents
proposals/schemas/           JSON Schemas, valid/invalid fixtures, solver allow-list
proposals/fixtures/          Proposal smoke fixtures, not production release data
baselines/                   Legacy/reproduction baseline scripts
runs/                        Local run outputs and logs
release/TEND-dataset/        Default production release target
minidev/MINIDEV/             Expected BIRD mini-dev source root, if present locally
```

## Requirements

- Python 3.11 or newer.
- BIRD mini-dev data at `minidev/MINIDEV`, or a custom path via
  `TEND_BIRD_ROOT`, for `construct`.
- An OpenAI-compatible chat-completions provider for live `construct` and
  `solve` runs.
- MongoDB for live Phase B execution gates and SMART per-stage execution. Phase
  A can still materialize data without a reachable MongoDB, and stub mode can
  exercise the pipeline offline.
- `uv` is recommended because this repo includes `uv.lock`; standard `pip`
  works as well.

## Installation

Using `uv`:

```bash
uv sync
uv pip install -e ".[test]"
```

Using `venv` and `pip`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[test]"
```

Create local runtime configuration:

```bash
cp .env.example .env
```

Then edit `.env` as needed:

```dotenv
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
TEND_MONGO_URI=mongodb://localhost:27017
TEND_BIRD_ROOT=minidev/MINIDEV
TEND_MODEL=deepseek-v4-flash
TEND_LLM_MAX_CONCURRENCY=16
TEND_QUIET=0
```

For offline plumbing runs, use `--stub` on the command line or set
`TEND_LLM_STUB=1`.

## CLI

After installation, use either `tend ...` or `python -m tend ...`.

```bash
python -m tend --help
python -m tend construct --help
python -m tend validate --help
python -m tend publish --help
python -m tend solve --help
```

### Construct

Run a deterministic offline smoke construction over the `financial` database:

```bash
python -m tend construct --phase all --dbs financial --records 1 --stub --quiet
```

Run live Phase A only:

```bash
python -m tend construct --phase A --dbs financial
```

Attempt a larger live construction over all configured BIRD mini-dev databases:

```bash
python -m tend construct --phase all --dbs all --records 20
```

Useful flags:

| Flag | Meaning |
| --- | --- |
| `--phase A|B|all` | Select construction phase. Phase B needs Phase A artifacts from the same run. |
| `--dbs financial` | Comma-separated database ids, or `all`. |
| `--records 1` | Number of Phase B records to attempt. |
| `--stub` | Use deterministic canned LLM responses. |
| `--quiet` | Disable the live rich progress UI and keep structured console/log output. |
| `--run-id my-run` | Pin the run id and output directory. |

By default, construction writes dataset artifacts to
`runs/<run_id>/dataset/`. Override with `TEND_DATASET_OUT`.

### Validate

Validate a candidate dataset directory:

```bash
python -m tend validate --dataset-dir runs/<run_id>/dataset
```

Smoke validation relaxes the all-11-database composition requirement, which is
useful for tiny fixtures:

```bash
python -m tend validate --dataset-dir tests/fixtures/smoke_release --smoke
```

### Publish

`publish` validates in full mode and only copies the dataset when validation
passes:

```bash
python -m tend publish \
  --dataset-dir runs/<run_id>/dataset \
  --out release/TEND-dataset
```

Full validation enforces record contracts, JSON Schema checks, required
per-database files, matching `test.json`/`TEND.json`, world signatures, and
composition thresholds such as all 11 BIRD databases, L4 share, L0 cap,
schema-flex share, and structural-schema-flex share.

### Solve

Run the SMART solver against a release-style dataset:

```bash
python -m tend solve \
  --dataset-dir tests/fixtures/smoke_release \
  --db-id financial \
  --record-id 1001 \
  --stub \
  --quiet
```

Outputs are written under the run directory:

- `solver_predictions.jsonl` for successful predictions;
- `solver_failures.jsonl` for typed terminal failures;
- standard `events.jsonl`, `anomalies.jsonl`, and LLM transcripts.

## Outputs And Logs

Every run receives a run id such as `run-20260601-013355-a1b2` unless
`--run-id` is provided.

```text
runs/<run_id>/
  events.jsonl
  anomalies.jsonl
  llm/<agent>/<call_id>.md
  llm/<agent>/<call_id>.diagnostics.json
  dataset/
    mongodb_schema/<db_id>.json
    mongodb_data/<db_id>.json
    agent_design_rationale/<db_id>.yaml
    bird_db_catalog.json
    test.json
    TEND.json
```

Start failure triage with `runs/<run_id>/anomalies.jsonl`. LLM-related anomalies
include a `transcript_ref` and diagnostics reference pointing to the exact prompt,
response attempts, parsed output, validation failures, and usage metadata. The
full event stream is in `events.jsonl`.

## Release Contract

The active release validator checks the contracts encoded in
`proposals/schemas/` and `src/tend/publish/validate.py`.

Important invariants:

- Gold MQL must be a `db.<collection>.aggregate([...])` pipeline.
- Disabled tokens are rejected anywhere: `$sample`, `$rand`, `$$NOW`, `$out`,
  `$merge`, and `$function`.
- `canonical_form_set` is intentionally thin: it carries disabled tokens,
  unavoidable structural operators, and shape guards, not replaceable idioms
  such as `$addFields`, `$cond`, or `$type`.
- `shape_policy` must be one of `preserve`, `reshape`, or `reduce`.
- `structural_schema_flex` records must be L4 and must carry a non-`none`
  `schema_flex` mode.
- `world_signature` must match the canonicalized witness data for the record's
  `db_id`.
- Production publish mode expects complete release composition, not smoke-only
  fixtures.

## Running Tests

With dependencies installed:

```bash
python -m pytest
```

Targeted checks:

```bash
python -m pytest tests/test_validate.py
python -m pytest tests/test_solver_workflow.py
python -m pytest tests/test_pipeline.py
```

Some tests and live paths depend on local BIRD mini-dev data and/or MongoDB.
Stub-mode tests avoid live LLM calls.

## Baselines

The `baselines/` directory contains reproduction-oriented scripts rather than
the active construction pipeline:

- `zero-shot/` prompts a model directly from schema and NLQ.
- `ICL/` adds in-context examples.
- `RAG/` retrieves related examples before generation.
- `self_debug/` performs iterative self-debugging around generated MQL.
- `SQL_to_NoSQL/` uses SQL, SQL schema, MongoDB schema, and a bundled SQL-to-
  Mongo converter grammar for SQL-assisted baselines.

These scripts are useful for comparison experiments, but the active CLI,
contracts, observability, release validation, and SMART solver live under
`src/tend/`.

## Development Notes

- Prefer relative repository paths in docs, prompts, schemas, and examples.
- Keep smoke fixtures labeled as smoke; do not describe them as production
  release data.
- Use `--stub --quiet` for fast workflow plumbing checks.
- Use `tend validate --smoke` for tiny fixtures and full `tend publish` for
  release candidates.
- When debugging a run, inspect `anomalies.jsonl` first, then the referenced LLM
  transcript markdown and diagnostics JSON.
- CodeGraph is available for structural code navigation in this repository.
