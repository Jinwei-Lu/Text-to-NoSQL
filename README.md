# TEND

TEND is a submission repository for a Text-to-NoSQL benchmark and runtime
centered on executable MongoDB aggregation pipelines.

The public GitHub repository is intentionally lean. Apart from minimal root
setup files such as this README, `pyproject.toml`, `requirements.txt`, and
`.env.example`, the uploaded code surface is `src/tend/` plus the accepted
VLDB demo implementation under `demonstration/`. Do not upload `tests/`,
`scripts/`, `docs/`, `proposals/`, `release/`, `drive_upload/`, `runs/`,
generated result folders, raw dataset payloads, or paper source directories to
GitHub.

## Dataset Download

The TEND dataset is not stored in GitHub. Download it from Google Drive:

[Google Drive: TEND native variant final artifacts](https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5?usp=drive_link)

The Drive release should contain exactly:

```text
TEND.json
mongodb_data/
```

It should not contain schemas, paper statistics, value dictionaries, README
files, checksum sidecars, archives, release helper folders, or any other
generated payload.

After downloading, restore the files locally with this layout:

```text
release/tend-native-mongodb-v1/
  data/TEND.json
  mongodb_data/
```

`release/` and `drive_upload/` are local-only directories. They are ignored by
Git and should not be uploaded to GitHub.

## QueryCraft Demonstration

This repository also includes `QueryCraft`, the browser-based TEND
demonstration system accepted to the VLDB demo track. QueryCraft lets users ask
natural language questions over MongoDB databases, inspect generated aggregation
pipelines, optionally execute them against MongoDB, and review prior attempts in
a local history panel. The interface is designed for Text-to-NoSQL behavior
that Text-to-SQL demos usually hide: nested schema browsing, aggregation-stage
debugging, execution feedback, and solver metadata inspection.

The public demo source lives in:

```text
demonstration/
```

The demo is code-only. It reads the restored TEND release dataset from
`release/tend-native-mongodb-v1/` by default and must not carry copied data
payloads inside `demonstration/`. In particular, local folders such as
`demonstration/mongodb_data/`, `demonstration/mongodb_schema/`, and
`demonstration/schemas/` are intentionally ignored. The VLDB demo-paper source
under `paper_demo/` is also local-only and should not be uploaded to GitHub.

## Repository Layout

| Path | Availability | Purpose |
| --- | --- | --- |
| `src/tend/` | GitHub | Public Python package: construction, validation, solver, baselines, ablations, evaluation, and observability. |
| `README.md` | GitHub | Public setup, dataset-download, and command guide. |
| `pyproject.toml` | GitHub | Package metadata and `tend` CLI entry point. |
| `requirements.txt` | GitHub | The single pip dependency file for the public runtime package. |
| `.env.example` | GitHub | Optional local configuration template. |
| `demonstration/` | GitHub | QueryCraft Flask demo: schema browser, natural-language query UI, generated MQL view, execution feedback, and history panel. |
| `release/tend-native-mongodb-v1/data/TEND.json` | Google Drive restore | Public NL-MQL task file after dataset restore. |
| `release/tend-native-mongodb-v1/mongodb_data/` | Google Drive restore | Raw MongoDB witness JSON files after dataset restore. |

## Benchmark Snapshot

The current public release is `tend-native-mongodb-v1`. Download it from the
Drive folder above, then restore it under `release/tend-native-mongodb-v1/`
before running validation, solver evaluation, baselines, or ablations.

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

The 11 database ids are:

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

## Public Record Format

After dataset restore, `release/tend-native-mongodb-v1/data/TEND.json` is the
reviewer-facing task file. Each record has exactly five fields:

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
paraphrase/robustness track for the same MQL target, not a second independent
task.

## Installation

Requirements:

- Python 3.11 or newer.
- MongoDB for live solver, baseline, ablation, and evaluation runs against
  witness data.
- An OpenAI-compatible chat-completions provider for live LLM runs.
- The downloaded TEND dataset package from Google Drive for full benchmark
  execution.
- BIRD mini-dev data only if you want to run the construction pipeline from
  source; the frozen release can be inspected without BIRD.

Install with standard `venv` and `pip`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

For the QueryCraft demo, install the optional Flask dependency:

```bash
python -m pip install -e '.[demo]'
```

Optional local runtime configuration:

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

Stub-mode commands use deterministic fixed responses and do not call a live LLM.

## Running QueryCraft

Restore the dataset as described above, then start the Flask demo:

```bash
TEND_DEMO_PORT=5050 ./.venv/bin/python -c "from demonstration.app import app; app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)"
```

Open:

```text
http://127.0.0.1:5050
```

By default, QueryCraft uses `TEND_DEMO_SOLVER_MODE=stub`, which exercises the
SAG solver path with a deterministic local LLM stub. Set
`TEND_DEMO_SOLVER_MODE=live` to use the configured OpenAI-compatible provider;
live mode requires the usual `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and
`TEND_MODEL` settings. Selecting execution in the UI also requires MongoDB via
`TEND_MONGO_URI`.

## CLI

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

Useful commands after restoring the Drive dataset:

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

Outputs are written under `runs/<run_id>/evaluation/<kind>/` by default. `runs/`
is local runtime evidence and is not part of the GitHub upload surface.

## Reference Solver

The maintained reference solver is SAG, short for Schema-as-Data Grounding,
implemented under:

```text
src/tend/solver/sag/
```

SAG solves the task from `NLQ + read-only MongoDB world`. In release-record mode,
the CLI selects the record and database, but the solver prompt does not receive
gold MQL, difficulty, shape policy, canonical-form guards, private audit data, or
training artifacts.

Mechanism summary:

1. Induce a per-database `GroundingIndex` from bounded witness samples.
2. Render a closed lattice path card, including dynamic-key map collapse.
3. Anchor NLQ literals to observed stored values and paths.
4. Apply A_path and A_value alignment gates plus execution-grounded repair.
5. Use result-space consistency clustering for the full `sag_full`/v3 arm.

## Evaluation Metrics

The current headline metric is `EXC`, a bounded column-tolerant execution
accuracy with `beta=2`. Strict `EX`, unbounded `EXC_spider`, `EM`, `QSM`, `QFC`,
`EFM`, and `EVM` are preserved as diagnostic columns. Missing predictions and
typed `solver_failure`, `baseline_failure`, or `ablation_failure` rows remain in
the denominator as zero-score rows.

Do not treat `--stub` runs as paper-score runs. Stub mode is for offline
connectivity and contract testing only.

## Citation

```bibtex
@misc{lu2026bridginggapenablingnatural,
      title={Bridging the Gap: Enabling Natural Language Queries for NoSQL Databases through Text-to-NoSQL Translation}, 
      author={Jinwei Lu and Jiawei Lu and Chen Zhang and Zhiqian Qin and Haodi Zhang and Yuanfeng Song and Raymond Chi-Wing Wong},
      year={2026},
      eprint={2502.11201},
      archivePrefix={arXiv},
      primaryClass={cs.DB},
      url={https://arxiv.org/abs/2502.11201}, 
}
```
