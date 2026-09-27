# TEND per-question results

The answer of every system on every question of the experiments reported in the paper and in [`RESULTS.md`](../RESULTS.md), with its stored scores. The tables and analyses of `RESULTS.md` can be recounted from these files:

```bash
python scripts/check_results.py
python scripts/check_results.py --dataset-dir release/tend-native-mongodb-v1
```

The second form also computes the benchmark statistics and the analyses that read the reference pipelines, so it needs the restored release (see the main README). The checker's docstring names the few numbers that need data outside this folder: runs 2 and 3 of the stability check, the pre-registered subset of the dynamic-key detector comparison, the numbers of the submitted version, the ReAct step and SAG call counts, and the facts about stored documents and card sizes.

## Layout

Three folders, one per part of the experiments in the paper, each with one subfolder per backbone model: `DeepSeek` (DeepSeek-V4-Flash) and `Luna` (GPT-5.6-Luna). Each file holds the answers of one configuration, named as in the paper.

```text
results/
├── README.md
├── task_labels.csv
├── MANIFEST.sha256
├── SAG/
│   ├── DeepSeek/
│   │   └── SAG.jsonl.gz
│   └── Luna/
│       └── SAG.jsonl.gz
├── Baselines/
│   ├── DeepSeek/
│   │   ├── NLQ-only_Direct.jsonl.gz
│   │   ├── Schema_Direct.jsonl.gz
│   │   ├── Sampled-doc_Direct.jsonl.gz
│   │   ├── Data-rich_Direct.jsonl.gz
│   │   ├── SQL_Pivot.jsonl.gz
│   │   ├── ReAct.jsonl.gz
│   │   └── DIN-SQL-inspired.jsonl.gz
│   └── Luna/
│       ├── Data-rich_Direct.jsonl.gz
│       ├── Direct+conventions.jsonl.gz
│       ├── SQL_Pivot+real_DDL.jsonl.gz
│       └── ReAct.jsonl.gz
└── Ablation_study/
    ├── DeepSeek/
    │   ├── Full_SAG.jsonl.gz
    │   ├── One_decode.jsonl.gz
    │   ├── One_candidate.jsonl.gz
    │   ├── No_value_grounding.jsonl.gz
    │   ├── No_grounding.jsonl.gz
    │   ├── Top-level_fields_only.jsonl.gz
    │   └── No_dynamic-key_collapse.jsonl.gz
    └── Luna/
        ├── Full_SAG.jsonl.gz
        └── Narrower_dynamic-key_detector.jsonl.gz
```

- `SAG/` and `Baselines/` are Tables III (DeepSeek) and IV (Luna) of the paper.
- NLQ-only Direct, Schema Direct, Sampled-doc Direct, SQL Pivot, and ReAct on DeepSeek are the June 2026 runs of the submitted version, which were not re-run. The paper reports them unchanged.
- `Ablation_study/DeepSeek/` is Table VI. Its `Full_SAG` is a second run of the full SAG (479 correct), the reference of the table. The main result is `SAG/DeepSeek/SAG.jsonl.gz` (487).
- `Ablation_study/Luna/` is the dynamic-key detector comparison of Section IV-F: `Full_SAG` with the full detector and `Narrower_dynamic-key_detector`. On eight databases (`campaign` 2026-08) the full-detector side is the main GPT-5.6-Luna answer and the narrower detector was run at the same time. formula_1, student_club, and superhero (`campaign` 2026-09) were run later with both detectors at the same time, so `Full_SAG` totals 512 rather than the 514 of `SAG/Luna/SAG.jsonl.gz`.

The command-line arm of every configuration is listed in the main README.

## Row format

Each `.jsonl.gz` file has one JSON object per question, sorted by `db_id` and `record_id`:

| field | meaning |
| --- | --- |
| `configuration` | the system as named in the paper, for example `SAG` or `Ablation study / One decode` |
| `arm` | the baseline or ablation arm that produced the answer (`data_rich_direct` for Direct + conventions, run with `TEND_BASELINE_OUTPUT_CONTRACT=1`, and `sag_full` for the narrower detector, run with `TEND_SAG_KEYS_V2=0`) |
| `backbone` | `DeepSeek-V4-Flash` or `GPT-5.6-Luna` |
| `campaign` | `2026-06` (the baselines kept from the submitted version), `2026-08` (the final experiments), or `2026-09` (the three databases added to the detector comparison) |
| `db_id`, `record_id` | the task, which joins `data/TEND.json` of the release |
| `prediction_status` | `ok`, `failed`, or `no_submission` |
| `predicted_mql` | the submitted pipeline, `null` when the system submitted none |
| `failure_code`, `failure_message`, `failure_mql` | the typed failure and the rejected pipeline, if any. The June 2026 export did not keep them, so they are `null` in those rows. |
| `salvaged_from_transcripts` | `true` for answers replayed offline from saved transcripts after an interrupted run (california_schools in `SAG/DeepSeek/SAG.jsonl.gz`, student_club in `SAG/Luna/SAG.jsonl.gz`) |
| `exc`, `exf1`, `outcome` | the stored scores: EXC (0 or 1), EXF1, and the outcome bucket |

The question and the reference MQL are not repeated here. Join on `(db_id, record_id)` with `data/TEND.json`.

Outcome buckets: `correct`, `no_submission` (Fail in the paper), `invalid`, `exec_error` (Exec), `empty` (Empty), `order_only`, `row_subset`, and `row_superset` (Struct.), `value_mismatch` (Value), and `row_count_exceeded` (a result with more rows than the reference, counted with the wrong rows). Every failed or missing answer scores 0 and stays in the denominator.

## Task labels

`task_labels.csv` has one row per task with `db_id`, `record_id`, `structure_category`, and `sql_transfer_resistance`. The structure category (dynamic-key map, nested event stream, polymorphic shape, missing versus present, or attribute bag) was assigned when the task was designed. The resistance to SQL transfer (strong, medium, or weak) is computed from the operators of the released reference pipeline with `classify_anti_sql_transfer` in `src/tend/construction/verify.py`.

## Provenance

The scores are copied from the stored per-record metrics of each run and are not re-computed. Re-evaluating the same answers against a reloaded MongoDB can move one or two answers whose correctness depends on the order of tied rows, so the stored scores are the official ones. These files were derived from a sealed local bundle of the raw run outputs, pinned by `3c764bcc317971bb9ec999d8015cb8f2c5a7e7e436820ff85dbeadd2d69b62d7  tend_final_results_v1/MANIFEST.json`, and from its addendum for the three databases added to the detector comparison.

## License

The files in this folder contain values from the TEND databases, which derive from BIRD mini-dev, and are released under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/), like the dataset.
