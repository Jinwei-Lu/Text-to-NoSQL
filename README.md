# TEND: Text-to-NoSQL Benchmark and QueryCraft Demonstration

TEND is an execution-verified benchmark and runtime for Text-to-NoSQL: translating natural-language questions into executable MongoDB aggregation pipelines over MongoDB-native document databases. The benchmark is designed to evaluate reasoning over nested paths, arrays, optional and sparse fields, polymorphic document shapes, dynamic keys, and dependencies across aggregation stages.

This repository provides the public code artifact for the TEND benchmark, the SAG reference solver, and QueryCraft, an interactive demonstration system for natural-language MongoDB querying.

## Publications

| Work | Status | Link |
| --- | --- | --- |
| **Bridging the Gap: Enabling Natural Language Queries for NoSQL Databases through Text-to-NoSQL Translation** | Accepted to ICDE 2027; proceedings citation forthcoming | [arXiv:2502.11201](https://arxiv.org/abs/2502.11201) |
| **QueryCraft: A Natural Language-Driven NoSQL Database Querying System Powered by Large Language Models** | Accepted to the VLDB 2026 Demo Track; proceedings citation forthcoming | Source code in [`demonstration/`](demonstration/) |

Please cite the full paper for the benchmark, solver, and dataset. The QueryCraft demo paper has been accepted but has not yet appeared in the proceedings; this README will be updated with the official demo citation after publication.

## Repository Contents

| Path | Purpose |
| --- | --- |
| [`src/tend/`](src/tend/) | Public Python package for dataset handling, validation, solving, baselines, ablations, evaluation, and observability. |
| [`demonstration/`](demonstration/) | QueryCraft Flask demo with database selection, browsing of the structure induced from stored documents, generated-MQL inspection, optional read-only execution, and solver metadata. |
| [`proposals/`](proposals/) | Runtime files the package reads: the baseline allow list and the record and catalog JSON schemas used by `tend validate`. |
| [`scripts/`](scripts/) | Metric-validation runners, `validate_metric.py` (M1, M2) and `run_counterfactual.py` (M3), and `check_results.py`, which recounts the tables and analyses of `RESULTS.md` from `results/`. |
| [`RESULTS.md`](RESULTS.md) | Final experimental results and how each number was produced. |
| [`results/`](results/) | The answer of every system on every question of the reported experiments, with its stored scores, and the label file `task_labels.csv` with each task's structure category and resistance to SQL transfer. |
| [`pyproject.toml`](pyproject.toml) | Package metadata, optional `demo` and `test` dependency groups, and `tend` CLI entry point. |
| [`requirements.txt`](requirements.txt) | Runtime dependency file for standard pip-based installation. |
| [`.env.example`](.env.example) | Optional local configuration template. |

Large release artifacts, MongoDB witness data, raw run outputs, and paper source directories are not stored in GitHub. They are restored or generated locally as described below.

## Dataset Release

The TEND dataset is hosted outside GitHub because the release contains large MongoDB witness data. Download the current native MongoDB release from:

[Google Drive: TEND native variant final artifacts](https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5?usp=drive_link)

The Drive folder holds `TEND.json` (the task file) and `mongodb_data.zip` (the MongoDB witness documents). TEND is schema-less by design: no schema is distributed, and a system has to induce each database's structure from the stored documents. From the repository root, with both files downloaded:

```bash
mkdir -p release/tend-native-mongodb-v1/data
mv /path/to/TEND.json release/tend-native-mongodb-v1/data/
unzip /path/to/mongodb_data.zip -x '__MACOSX/*' -d release/tend-native-mongodb-v1/
```

The result is:

```text
release/tend-native-mongodb-v1/
  data/TEND.json               # Google Drive
  mongodb_data/<db_id>.json    # Google Drive (mongodb_data.zip)
```

The CLI and QueryCraft demo use `release/tend-native-mongodb-v1/` by default. Set `TEND_DEMO_DATASET_DIR` or pass `--dataset-dir` to use a different release-compatible location.

`release/` is ignored by Git, as are `runs/`, local logs, and generated outputs. They should remain local artifacts rather than repository contents.

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
| Collections / queried collections | 32 / 30 |
| MongoDB witness documents | 269,177 |
| Distinct MQL strings | 1,210 |
| Skeleton families / largest family | 1,115 / 6 |
| Median / max top-level stages | 7 / 14 |
| Dynamic-key operator records | 1,094 (90.4%) |
| Array-operator records | 1,172 (96.9%) |
| Nested dotted-path records | 1,174 (97.0%) |
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

After dataset restore, `release/tend-native-mongodb-v1/data/TEND.json` is the benchmark task file. Each record contains:

```json
{
  "record_id": 1248463,
  "db_id": "european_football_2",
  "NLQ": "How many league seasons have recorded at least one home win?",
  "NLQ_colloquial": "I need the number of seasons that have at least one home win in the dataset.",
  "MQL": "db.league_season_buckets.aggregate([...])"
}
```

Use `NLQ` as the default evaluation utterance. `NLQ_colloquial` is a paraphrase/robustness variant for the same MQL target, not a second independent task.

The structure category and the resistance to SQL transfer of every task, which the paper uses to break down the results, are in [`results/task_labels.csv`](results/task_labels.csv).

## Installation

Requirements:

- Python 3.11 or newer.
- MongoDB for live solver, baseline, ablation, QueryCraft execution, and evaluation runs over witness data.
- An OpenAI-compatible chat-completions provider for live LLM runs.
- The restored TEND release package for full benchmark execution.
- BIRD mini-dev data only if reconstructing the benchmark from source; the released benchmark can be inspected and evaluated without BIRD.

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

Commands that run in stub mode use deterministic local responses and do not call a live LLM provider.

## QueryCraft Demo

QueryCraft is an interactive browser-based system for natural-language MongoDB querying. It presents the components needed to inspect Text-to-NoSQL behavior:

- database selection and example NLQs;
- hierarchical browsing of each database's structure, induced from sampled documents, including nested fields, field types, and dynamic-key maps;
- generated MongoDB aggregation pipelines;
- optional read-only execution of the generated or edited pipeline;
- solver metadata for debugging successful and failed generations.

The demo source is tracked in [`demonstration/`](demonstration/). The demo does not include copied dataset payloads; it reads the restored release directory described above.

Start QueryCraft locally:

```bash
TEND_DEMO_PORT=5050 TEND_USE_EXISTING_MONGO_DBS=1 ./.venv/bin/python -m demonstration.app
```

`TEND_USE_EXISTING_MONGO_DBS=1` makes the demo read the eleven release databases from a MongoDB that already holds them (one database per `db_id`) instead of parsing the multi-GB witness files; see [`demonstration/README.md`](demonstration/README.md).

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
TEND_DEMO_HOST=127.0.0.1
TEND_DEMO_PORT=5000
TEND_DEMO_LLM_MAX_CONCURRENCY=3
TEND_DEMO_DEBUG=0
TEND_USE_EXISTING_MONGO_DBS=1
```

`TEND_DEMO_SOLVER_MODE=stub` is the default and is appropriate for smoke tests and UI checks. Set `TEND_DEMO_SOLVER_MODE=live` to use the configured OpenAI-compatible provider. Live mode requires `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `TEND_MODEL`. Query execution also requires MongoDB via `TEND_MONGO_URI`.

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
.venv/bin/python -m tend solve \
  --dataset-dir release/tend-native-mongodb-v1 \
  --db-id financial \
  --limit 110 \
  --run-id sag-financial

.venv/bin/python -m tend baseline \
  --dataset-dir release/tend-native-mongodb-v1 \
  --baselines all \
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

Baseline arms, the baselines in [`RESULTS.md`](RESULTS.md): `data_rich_direct` (Direct with a data-rich prompt), `dinsql_mql` (the DIN-SQL-inspired MQL adaptation), `sql_pivot_schema` (SQL Pivot given the real relational DDL), and `react_informed` (ReAct with the real collection names and raw first-five-row observations, at most 50 steps as in the final runs; `TEND_BASELINE_REACT_MAX_STEPS` overrides it); `--baselines all` runs all four. "Direct with SAG's six output conventions" is `data_rich_direct` run with `TEND_BASELINE_OUTPUT_CONTRACT=1`, which appends the same six output rules SAG's prompt states.

On the restored release, `tend validate --dataset-dir release/tend-native-mongodb-v1` checks the five-field records (1,210 tasks, 110 per database, parseable MQL, no duplicate MQL or NLQ) and that every database's witness data is present. On the output of a local `tend construct` run, `tend validate` checks the full record and composition contract, and `tend publish --out <dir>` copies the dataset to `<dir>` only if it passes.

`tend construct --dbs all` rebuilds the eleven MongoDB databases from BIRD mini-dev byte for byte as released (`mongodb_data/`) and generates candidate records deterministically from each database's native features. The released questions came from an earlier, LLM-assisted version of the pipeline followed by review, so a construct run does not reproduce `data/TEND.json`.

Ablation arms, the seven configurations of the ablation in [`RESULTS.md`](RESULTS.md): `sag_full` (the full solver, the reference row), `sag_core_generate_only`, `sag_v2`, `sag_core_no_value_witness_strict`, `sag_core_no_grounding`, `sag_v3_top_card`, and `sag_v3_no_collapse`; `--ablations all` runs all seven.

`--run-id` is a tag: each run is written to `runs/run-<timestamp>-<tag>-<hex>/`, and `solve`, `baseline`, and `ablation` evaluate their predictions automatically unless `--no-eval` is given. To evaluate saved predictions again:

```bash
.venv/bin/python -m tend evaluate \
  --dataset-dir release/tend-native-mongodb-v1 \
  --predictions runs/<run_dir>/solve/solver_predictions.jsonl \
  --kind solver \
  --workers 8
```

Outputs are written under `runs/<run_dir>/evaluation/<kind>/` by default. `runs/` is local runtime evidence and is intentionally not part of the GitHub artifact.

## Reference Solver

The maintained reference solver is SAG, short for Schema-as-Data Grounding, implemented under:

```text
src/tend/solver/sag/
```

SAG solves the task from `NLQ + read-only MongoDB world`. In release-record mode, the CLI selects the record and database, but the solver prompt does not receive gold MQL, difficulty, shape policy, canonical-form guards, private audit data, or training artifacts.

Mechanism summary:

1. Induce a per-database `GroundingIndex` from bounded witness samples.
2. Render a closed lattice path card per collection. Dynamic-key maps are recognized from their keys (dates, codes, parallel sibling members) and collapsed to `<*>`, and an `_id` line explains that the document key is a readable identifier.
3. Anchor NLQ literals to observed stored values and paths (value witnesses).
4. Check each candidate with the gate (`A_path`, `A_value`, and the limit check) and repair it from execution feedback. An empty result is traced to the first stage that empties it (prefix counting in the paper, `bisect_empty` in `repair.py`).
5. Pick among three candidates by result-space consistency (`sag_full`).

`TEND_SAG_KEYS_V2=0` switches back to the dynamic-key recognition used before the final revision; it exists for the on/off comparison in [`RESULTS.md`](RESULTS.md).

## Evaluation Metrics

The main metric is `EXC`, execution accuracy that ignores column names and tolerates at most two surplus columns per row (`beta=2`). `EXF1` is its graded companion: a row-multiset F1 without surplus tolerance. Every record also gets one outcome bucket (`correct`, `no_submission`, `invalid`, `exec_error`, `empty`, `order_only`, `row_subset`, `row_superset`, `value_mismatch`, `row_count_exceeded`), and ablation reports add an exact McNemar test against `sag_full`. Missing predictions and typed `solver_failure`, `baseline_failure`, or `ablation_failure` rows remain in the denominator as zero-score rows. If MongoDB becomes unavailable during evaluation, the run stops instead of scoring the affected rows zero.

Two scripts check the metric itself; they need MongoDB but no LLM. `scripts/validate_metric.py m1` confirms that every reference query scores EXC = 1 against itself, and `scripts/validate_metric.py m2` sweeps the surplus bound over null and shortcut probes, which should select β = 2. `scripts/run_counterfactual.py` measures how often a system's passing answers flip on a witness extended with distractor documents that leave the reference result unchanged.

Do not treat `--stub` runs as paper-score runs. Stub mode is for offline connectivity, interface checks, and contract testing only.

## Results

Final results on all 1,210 questions, as reported in Tables III and IV of the paper (details, per-database tables, the component ablation, and how each number was produced are in [`RESULTS.md`](RESULTS.md)):

| system | DeepSeek-V4-Flash | GPT-5.6-Luna |
| --- | ---: | ---: |
| NLQ-only Direct | 2 (0.2%)† | — |
| Schema Direct | 3 (0.2%)† | — |
| Sampled-doc Direct | 346 (28.6%)† | — |
| Data-rich Direct | 352 (29.1%) | 421 (34.8%) |
| Direct + conventions (SAG's six output rules) | — | 445 (36.8%) |
| SQL Pivot | 310 (25.6%)† | — |
| SQL Pivot + real DDL | — | 269 (22.2%) |
| ReAct (real collection names, first five rows) | 373 (30.8%)† | 390 (32.2%) |
| DIN-SQL-inspired | 339 (28.0%) | — |
| **SAG** | **487 (40.2%)** | **514 (42.5%)** |

† Runs of the submitted version (June 2026) that were not re-run for the final results. The paper reports them unchanged. NLQ-only Direct, Schema Direct, Sampled-doc Direct, and SQL Pivot without the DDL exist in the code only up to commit `260801ea` (see below).

The answer of every system on every question, including the ablation, stability, and dynamic-key experiments, is in [`results/`](results/), and `python scripts/check_results.py` recounts the tables and analyses of `RESULTS.md` from these files. Its docstring names the few numbers that need data outside `results/`.

## Paper-to-Code Map

How each system and variant of the paper's experiments (Section IV) is run with this package:

| paper name | how to run |
| --- | --- |
| SAG | `tend ablation --ablations sag_full` (the harness of the final runs) |
| Data-rich Direct | `tend baseline --baselines data_rich_direct` |
| Direct + conventions | `data_rich_direct` with `TEND_BASELINE_OUTPUT_CONTRACT=1` |
| SQL Pivot + real DDL | `tend baseline --baselines sql_pivot_schema` |
| ReAct | `tend baseline --baselines react_informed` (50-step budget) |
| DIN-SQL-inspired | `tend baseline --baselines dinsql_mql` |
| NLQ-only Direct, Schema Direct, Sampled-doc Direct, SQL Pivot | baselines `direct_nlq_only`, `schema_direct`, `direct`, and `sql_pivot` at commit `260801ea` |
| One decode | `tend ablation --ablations sag_core_generate_only` |
| One candidate | `tend ablation --ablations sag_v2` |
| No value grounding | `tend ablation --ablations sag_core_no_value_witness_strict` |
| No grounding | `tend ablation --ablations sag_core_no_grounding` |
| Top-level fields only | `tend ablation --ablations sag_v3_top_card` |
| No dynamic-key collapse | `tend ablation --ablations sag_v3_no_collapse` |
| Narrower dynamic-key detector | `sag_full` with `TEND_SAG_KEYS_V2=0` |

The components named in the paper map to these modules under `src/tend/solver/sag/`: the grounding index and path card to `induction.py`, value witnesses to `witness.py`, the gate checks to `gates.py`, execution repair and prefix counting to `repair.py`, and the result-consistency vote to `runtime.py`.

## License

- **Code**, everything in this repository except the dataset and `results/`: MIT License, see [`LICENSE`](LICENSE).
- **Results**, the per-question answers and task labels in [`results/`](results/): [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), like the dataset, because they contain values from its databases.
- **Dataset**, the TEND release distributed through Google Drive: [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). TEND's databases and values are derived from [BIRD mini-dev](https://github.com/bird-bench/mini_dev), which is released under CC BY-SA 4.0, and its ShareAlike terms carry over to TEND.
- **Third-party examples.** The six fixed examples used by the DIN-SQL-inspired baseline, [`src/tend/baselines/assets/dinsql_mql_exemplars.json`](src/tend/baselines/assets/dinsql_mql_exemplars.json), come from MongoDB's [natural-language-to-mongosh](https://huggingface.co/datasets/mongodb-eai/natural-language-to-mongosh) dataset and are redistributed under the Apache License 2.0; the license text is next to them in `dinsql_mql_exemplars.LICENSE.txt`.

## Citation

Please cite the full paper:

```bibtex
@inproceedings{lu2027bridging,
      title={Bridging the Gap: Enabling Natural Language Queries for NoSQL Databases through Text-to-NoSQL Translation},
      author={Jinwei Lu and Jiawei Lu and Chen Jason Zhang and Zhiqian Qin and Yuanfeng Song and Haodi Zhang and Raymond Chi-Wing Wong},
      booktitle={Proceedings of the IEEE International Conference on Data Engineering (ICDE)},
      year={2027},
      note={To appear},
}
```

The preprint is available on arXiv:

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

The QueryCraft demo citation will be added after the VLDB proceedings entry is available.
