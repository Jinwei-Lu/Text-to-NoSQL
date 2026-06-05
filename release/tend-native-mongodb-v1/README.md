# TEND Native MongoDB v1

Formal release id: `tend-native-mongodb-v1`

Source build/run id: `native-variant-11db-110distinct-final3`

Release date: `2026-06-05`

This is the lean public TEND Native MongoDB benchmark package. Use this
directory for GitHub publication, paper tables, reviewer inspection, and local
evaluation. Internal construction records, repair metadata, and audit
transcripts are intentionally not part of this release surface.

## Directory Layout

| Path | Contents |
| --- | --- |
| `data/TEND_lean.json` | Public NL-MQL records. Each record has exactly five fields: `record_id`, `db_id`, `NLQ`, `NLQ_colloquial`, and `MQL`. |
| `schema/mongodb_schema/` | One MongoDB schema JSON per database. |
| `statistics/paper_statistics/` | Paper-ready JSON, CSV, Markdown, and LaTeX statistics. |
| `mongodb_data/` | Raw MongoDB witness exports in a full local/Drive release; ignored by Git. |
| `external/` | Google Drive package links/checksums and raw MongoDB checksum manifest. |

## Data Files

The release data directory contains one public query file:

- `data/TEND_lean.json`: 1,210 lightweight evaluation records.

Lean records have this shape:

```json
{
  "record_id": 8346,
  "db_id": "student_club",
  "NLQ": "Find members attending multiple guest-speaker events that include Speaker Gifts budgets; return up to 11 members.",
  "NLQ_colloquial": "List up to 11 members tied to repeated guest-speaker gift budgets.",
  "MQL": "db.club_member_accounts_v2.aggregate([...])"
}
```

## NLQ Convention

Each record has one canonical natural-language query and one colloquial variant
for the same MQL target.

Use `NLQ` as the default evaluation utterance. `NLQ_colloquial` is for
robustness or paraphrase-style evaluation and should not be counted as a second
independent task.

## Scale

- Databases: `11`
- NL-MQL tasks: `1,210`
- Records per database: `110`
- Canonical NL utterances: `1,210`
- Colloquial NL utterances: `1,210`
- Distinct MQL strings/signatures: `1,210 / 1,210`
- Aggregation pipelines parsed: `1,210 / 1,210`
- Median / max top-level stages: `7 / 12`
- Global / DB-scoped MQL skeleton families: `1,035 / 1,104`
- Max DB-scoped skeleton family: `7`
- Raw MongoDB documents in witness data: `269,177`

The 11 database ids are:

`california_schools`, `card_games`, `codebase_community`,
`debit_card_specializing`, `european_football_2`, `financial`, `formula_1`,
`student_club`, `superhero`, `thrombosis_prediction`, and `toxicology`.

## MongoDB Witness Data

The raw files in `mongodb_data/*.json` are required for a full local release
and exact execution checks, but they are intentionally ignored by Git because
they are large. In this workspace, `mongodb_data/*.json` are hard-linked from
the final build artifacts to avoid duplicating several GiB on disk.

The Drive full package contains the raw witness exports:

https://drive.google.com/file/d/1O9ctyY6mUuKBF6OMCqk_UXyGdNkM8MRE/view?usp=drivesdk

Current lean public package upload status is recorded in
`external/google_drive_files.json`.

Drive folder:

https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5

Per-database raw JSON checksums and sizes are recorded in:

```text
external/raw_mongodb_data_checksums_and_sizes.txt
```

## Paper Statistics

Primary paper files:

- `statistics/paper_statistics/paper_dataset_statistics.md`
- `statistics/paper_statistics/paper_dataset_statistics.json`
- `statistics/paper_statistics/paper_statistics_by_db.csv`
- `statistics/paper_statistics/paper_tables.tex`
- `statistics/paper_statistics/pipeline_stage_detailed_statistics.md`
- `statistics/paper_statistics/pipeline_stage_detailed_statistics.json`
- `statistics/paper_statistics/pipeline_stage_tables.tex`
- `statistics/paper_statistics/pipeline_stage_complexity_by_record.csv`
- `statistics/paper_statistics/fresh_exact_execution_by_db_verification.json`

Important headline numbers:

- Fresh exact MongoDB execution: `1,210 / 1,210`
- Execution failures: `0`
- Empty outputs: `0`
- Public lean contract: `OK`
- Distinct full stage sequences: `248`
- Stage-count histogram: `3:1`, `4:22`, `5:148`, `6:212`,
  `7:295`, `8:328`, `9:137`, `10:51`, `11:7`, `12:9`
- Structural stage buckets: `unwind_grouped:397`,
  `unwind_filter_project:298`, `linear_filter_project:248`,
  `multi_unwind_grouped:193`, `enrich_filter_project:63`,
  `group_without_unwind:11`

Prefer the `statistics/paper_statistics/` stage and operator distributions for
main paper claims; these are regenerated directly from the public lean MQLs.

## Validation

This package validates as a lean public release:

```bash
.venv/bin/python -m tend.cli validate --dataset-dir release/tend-native-mongodb-v1
```

The validation target is `data/TEND_lean.json`, not the internal full
construction records. The validator checks the five-field public record shape,
database composition, MQL parsing, duplicate MQL/NLQ pairs, MQL skeleton-family
concentration, schema files, and local MongoDB witness files.
