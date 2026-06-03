# TEND Native MongoDB v1

Formal release id: `tend-native-mongodb-v1`

Source build/run id: `native-variant-11db-110distinct-final3`

Release date: `2026-06-04`

This is the release-facing TEND Native MongoDB benchmark package. Use this
directory for GitHub publication, paper tables, reviewer inspection, and local
evaluation. The `runs/` directory is generation evidence only and is not the
canonical release location.

## Directory Layout

| Path | Contents |
| --- | --- |
| `data/` | Full and lean NL-MQL records plus database catalog and MongoDB data manifest. |
| `schema/mongodb_schema/` | One MongoDB schema JSON per database. |
| `metadata/native_feature_manifest/` | Per-database Mongo-native feature manifests. |
| `metadata/migration_recipe/` | Per-database relational-to-native MongoDB migration recipes. |
| `metadata/agent_design_rationale/` | Per-database structure and workload design rationale. |
| `metadata/provenance/` | Per-database provenance records. |
| `statistics/paper_statistics/` | Paper-ready JSON, CSV, Markdown, and LaTeX statistics. |
| `audits/nl_mql/` | Final post-surgery NL-MQL pair extraction and diversity/complexity audit. |
| `audits/surgery/` | Surgical patch report and exact post-surgery execution evidence. |
| `mongodb_data/` | Raw MongoDB witness exports in a full local/Drive release; ignored by Git. |
| `external/` | Google Drive package links/checksums and raw MongoDB checksum manifest. |

## Data Files

- `data/TEND.json`: full release records with metadata, provenance, native
  feature labels, and verification fields.
- `data/TEND_lean.json`: lightweight evaluation records.
- `data/TEND_lean.jsonl`: JSONL form of the lightweight records.
- `data/test.json` and `data/test_lean.json`: release test aliases matching the
  same 1,210 records.

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

- Full format: `nl_queries.canonical` and `nl_queries.colloquial`
- Lean format: `NLQ` and `NLQ_colloquial`

Use `NLQ` / `nl_queries.canonical` as the default evaluation utterance.
`NLQ_colloquial` is for robustness or paraphrase-style evaluation and should
not be counted as a second independent task.

## Scale

- Databases: `11`
- NL-MQL tasks: `1,210`
- Records per database: `110`
- Canonical NL utterances: `1,210`
- Colloquial NL utterances: `1,210`
- Distinct MQL strings/signatures: `1,210 / 1,210`
- Aggregation pipelines parsed: `1,210 / 1,210`
- Median / max top-level stages: `7 / 12`
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

Drive folder:

https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5

Expected full package checksum:

```text
3fabc19772bbe6e70c322944a3e7ce0e1a29c3c753ee04cb30d26580622f2594
```

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
- Distinct full stage sequences: `54`
- Stage-count histogram: `3:1`, `4:6`, `5:29`, `6:390`,
  `7:408`, `8:291`, `9:79`, `12:6`
- Structural stage buckets: `unwind_grouped:451`,
  `unwind_filter_project:308`, `enrich_filter_project:216`,
  `multi_unwind_grouped:134`, `linear_filter_project:91`,
  `group_without_unwind:10`

The post-surgery NL-MQL audit also reports `complexity_score`. This is a
heuristic audit score, not the primary paper metric:

```text
complexity_score =
  stage_count
  + 0.35 * unique_operator_count
  + 1.25 * count(root stages in {$unwind, $group})
  + 2.00 * count(unique heavy operators present)
  + 0.80 * count(unique nested/array operators present)
```

where heavy operators are `$lookup`, `$facet`, `$graphLookup`,
`$setWindowFields`, and `$unionWith`; nested/array operators are
`$objectToArray`, `$filter`, `$map`, `$reduce`, `$setUnion`, and
`$setIntersection`. Prefer the `statistics/paper_statistics/` stage and
operator distributions for main paper claims.

## Validation Caveat

Exact MongoDB execution is clean. The stricter release validator snapshot is
kept in `statistics/paper_statistics/release_validator_snapshot.*`; it records
metadata/provenance/native-feature gate issues separate from runtime execution.
Use the exact execution report for query executability and the validator
snapshot for remaining metadata-contract caveats.
