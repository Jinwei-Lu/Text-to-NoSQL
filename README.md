# TEND: Text-to-NoSQL Benchmark and QueryCraft Demonstration

TEND is an execution-verified benchmark and runtime for Text-to-NoSQL:
translating natural-language questions into executable MongoDB aggregation
pipelines over MongoDB-native document databases. The benchmark is designed to
evaluate reasoning over nested paths, arrays, optional and sparse fields,
polymorphic document shapes, dynamic keys, and dependencies across aggregation
stages.

This repository provides the public code artifact for the TEND benchmark, the
SAG reference solver, and QueryCraft, an interactive demonstration system for
natural-language MongoDB querying.

## Publications

| Work | Status | Link |
| --- | --- | --- |
| **Bridging the Gap: Enabling Natural Language Queries for NoSQL Databases through Text-to-NoSQL Translation** | Full paper | Reference withheld for double-blind review |
| **QueryCraft: A Natural Language-Driven NoSQL Database Querying System Powered by Large Language Models** | Accepted to the VLDB 2026 Demo Track | Source code in [`demonstration/`](demonstration/) |

The demonstration system built on this work has been accepted to the VLDB 2026
Demo Track. Author details and citations are withheld while the full paper is
under double-blind review.

## Repository Contents

| Path | Purpose |
| --- | --- |
| [`src/tend/`](src/tend/) | Public Python package for dataset handling, validation, solving, baselines, ablations, evaluation, and observability. |
| [`demonstration/`](demonstration/) | QueryCraft Flask demo with database selection, schema browsing, generated-MQL inspection, optional execution, solver metadata, and query history. |
| [`pyproject.toml`](pyproject.toml) | Package metadata, optional demo dependency group, and `tend` CLI entry point. |
| [`requirements.txt`](requirements.txt) | Runtime dependency file for standard pip-based installation. |
| [`.env.example`](.env.example) | Optional local configuration template. |

Large release artifacts, MongoDB witness data, generated experiment outputs,
and paper source directories are not stored in GitHub. They are restored or
generated locally as described below.

## Dataset Release

The TEND dataset is hosted outside GitHub because the release contains large
MongoDB witness data. Download the current native MongoDB release from:

[Google Drive: TEND native variant final artifacts](https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5?usp=drive_link)

Restore the release under the repository root with this layout:

```text
release/tend-native-mongodb-v1/
  data/TEND.json
  schema/mongodb_schema/
  mongodb_data/
```

The CLI and QueryCraft demo use `release/tend-native-mongodb-v1/` by default.
Set `TEND_DEMO_DATASET_DIR` or pass `--dataset-dir` to use a different
release-compatible location.

`release/`, `runs/`, raw MongoDB payloads, local logs, and generated outputs are
ignored by Git. They should remain local artifacts rather than repository
contents.

## Benchmark Snapshot

The current public release is `tend-native-mongodb-v1`.

| Metric | Value |
| --- | ---: |
| Databases | 11 |
| NL-MQL tasks | 1,210 |
| Records per database | 110 |
| Canonical NL utterances | 1,210 |
| Colloquial NL utterances | 1,210 |
| Public record fields | `record_id`, `db_id`, `NLQ`, `NLQ_colloquial`, `MQL` |
| Schema collections / queried collections | 32 / 30 |
| MongoDB witness documents | 269,177 |
| Distinct MQL strings / signatures | 1,210 / 1,210 |
| Global / DB-scoped skeleton families | 1,098 / 1,134 |
| Median / max top-level stages | 7 / 13 |
| Dynamic-key operator records | 1,096 (90.6%) |
| Array-operator records | 1,170 (96.7%) |
| Nested dotted-path records | 1,164 (96.2%) |
| Fresh exact MongoDB execution | 1,210 / 1,210 |

The release contains the following database ids:

```text
california_schools
card_games
codebase_community
debit_card_specializing
european_football_2
financial
formula_1
student_club
superhero
thrombosis_prediction
toxicology
```

## Record Format

After dataset restore, `release/tend-native-mongodb-v1/data/TEND.json` is the
benchmark task file. Each record contains:

```json
{
  "record_id": 8346,
  "db_id": "student_club",
  "NLQ": "Find members attending multiple guest-speaker events that include Speaker Gifts budgets; return up to 11 members.",
  "NLQ_colloquial": "List up to 11 members tied to repeated guest-speaker gift budgets.",
  "MQL": "db.club_member_accounts_v2.aggregate([...])"
}
```

Use `NLQ` as the default evaluation utterance. `NLQ_colloquial` is a
paraphrase/robustness variant for the same MQL target, not a second independent
task.

## Installation

Requirements:

- Python 3.11 or newer.
- MongoDB for live solver, baseline, ablation, QueryCraft execution, and
  evaluation runs over witness data.
- An OpenAI-compatible chat-completions provider for live LLM runs.
- The restored TEND release package for full benchmark execution.
- BIRD mini-dev data only if reconstructing the benchmark from source; the
  released benchmark can be inspected and evaluated without BIRD.

Create an environment and install the package:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

Install the optional QueryCraft dependency group when running the demo:

```bash
python -m pip install -e '.[demo]'
```

Optional local configuration:

```bash
cp .env.example .env
```

Common environment variables:

```dotenv
OPENAI_API_KEY=...
OPENAI_BASE_URL=...
TEND_MODEL=deepseek-v4-flash
TEND_MONGO_URI=mongodb://localhost:27017
TEND_BIRD_ROOT=minidev/MINIDEV
TEND_LLM_MAX_CONCURRENCY=0
TEND_QUIET=0
```

Commands that run in stub mode use deterministic local responses and do not
call a live LLM provider.

## QueryCraft Demo

QueryCraft is an interactive browser-based system for natural-language MongoDB
querying. It presents the components needed to inspect Text-to-NoSQL behavior:

- database selection and example NLQs;
- hierarchical MongoDB schema browsing, including nested fields and field
  types;
- generated MongoDB aggregation pipelines;
- optional execution feedback over MongoDB witness data;
- solver metadata for debugging successful and failed generations;
- local history for comparing previous attempts.

The demo source is tracked in [`demonstration/`](demonstration/). The demo does
not include copied dataset payloads; it reads the restored release directory
described above.

Start QueryCraft locally:

```bash
TEND_DEMO_PORT=5050 ./.venv/bin/python -c "from demonstration.app import app; app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)"
```

Open:

```text
http://127.0.0.1:5050
```

Useful demo settings:

```dotenv
TEND_DEMO_DATASET_DIR=release/tend-native-mongodb-v1
TEND_DEMO_SOLVER_MODE=stub
TEND_DEMO_SOLVE_TIMEOUT_S=90
TEND_DEMO_MAX_RETRIES=...
```

`TEND_DEMO_SOLVER_MODE=stub` is the default and is appropriate for smoke tests
and UI checks. Set `TEND_DEMO_SOLVER_MODE=live` to use the configured
OpenAI-compatible provider. Live mode requires `OPENAI_API_KEY`,
`OPENAI_BASE_URL`, and `TEND_MODEL`. Query execution also requires MongoDB via
`TEND_MONGO_URI`.

## Command Line Usage

After installation, use either `tend ...` or `python -m tend ...`.

```bash
.venv/bin/python -m tend --help
.venv/bin/python -m tend construct --help
.venv/bin/python -m tend validate --help
.venv/bin/python -m tend publish --help
.venv/bin/python -m tend solve --help
.venv/bin/python -m tend baseline --help
.venv/bin/python -m tend ablation --help
.venv/bin/python -m tend evaluate --help
```

Useful commands after restoring the release:

```bash
.venv/bin/python -m tend validate \
  --dataset-dir release/tend-native-mongodb-v1 \
  --metadata-only

.venv/bin/python -m tend solve \
  --dataset-dir release/tend-native-mongodb-v1 \
  --db-id financial \
  --limit 110 \
  --run-id sag-financial

.venv/bin/python -m tend baseline \
  --dataset-dir release/tend-native-mongodb-v1 \
  --baselines direct_nlq_only,schema_direct,direct,data_rich_direct,sql_pivot \
  --db-id financial \
  --limit 110 \
  --run-id baseline-financial

.venv/bin/python -m tend ablation \
  --dataset-dir release/tend-native-mongodb-v1 \
  --ablations all \
  --db-id financial \
  --limit 110 \
  --workers 48 \
  --run-id ablation-financial
```

Evaluate saved predictions with:

```bash
.venv/bin/python -m tend evaluate \
  --dataset-dir release/tend-native-mongodb-v1 \
  --predictions runs/<run_id>/solver_predictions.jsonl \
  --kind solver \
  --workers 8
```

Outputs are written under `runs/<run_id>/evaluation/<kind>/` by default.
`runs/` is local runtime evidence and is intentionally not part of the GitHub
artifact.

## Reference Solver

The maintained reference solver is SAG, short for Schema-as-Data Grounding,
implemented under:

```text
src/tend/solver/sag/
```

SAG solves the task from `NLQ + read-only MongoDB world`. In release-record
mode, the CLI selects the record and database, but the solver prompt does not
receive gold MQL, difficulty, shape policy, canonical-form guards, private
audit data, or training artifacts.

Mechanism summary:

1. Induce a per-database `GroundingIndex` from bounded witness samples.
2. Render a closed lattice path card, including dynamic-key map collapse.
3. Anchor NLQ literals to observed stored values and paths.
4. Apply A_path and A_value alignment gates plus execution-grounded repair.
5. Use result-space consistency clustering for the full `sag_full` arm.

## Evaluation Metrics

The headline metric is `EXC`, a bounded column-tolerant execution accuracy with
`beta=2`. Strict `EX`, unbounded `EXC_spider`, `EM`, `QSM`, `QFC`, `EFM`, and
`EVM` are preserved as diagnostic columns. Missing predictions and typed
`solver_failure`, `baseline_failure`, or `ablation_failure` rows remain in the
denominator as zero-score rows.

Do not treat `--stub` runs as paper-score runs. Stub mode is for offline
connectivity, interface checks, and contract testing only.

## Citation

Citation details are withheld while this submission is under double-blind
review, and will be restored in the camera-ready version.
