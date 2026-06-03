# TEND Final Artifact Index

Final run id: `native-variant-11db-110distinct-final3`

GitHub tracks the lightweight release assets needed for paper writing,
inspection, schema review, and statistics. The formal paper-facing release
package is:

`release/tend-native-mongodb-v1/`

The final run remains local generation evidence at:

`runs/native-variant-11db-110distinct-final3/`

The large MongoDB witness data is kept outside Git and should be distributed
through the Google Drive folder below.

Google Drive folder:
https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5

Direct Drive files:

- Full package: https://drive.google.com/file/d/1O9ctyY6mUuKBF6OMCqk_UXyGdNkM8MRE/view?usp=drivesdk
- Paper package: https://drive.google.com/file/d/1uEE_rVYBE1rNlKgCFOlHjI3omchdE_SA/view?usp=drivesdk

## Drive Packages

The local packages prepared for Google Drive upload are:

| Package | Size bytes | SHA256 | Contents |
| --- | ---: | --- | --- |
| `TEND_native_variant_11db_110distinct_final3_full_2026-06-03.tar.zst` | 224916368 | `3fabc19772bbe6e70c322944a3e7ce0e1a29c3c753ee04cb30d26580622f2594` | Source-build package containing the 11 MongoDB JSON witness exports. Extract raw JSON into the formal release layout. |
| `TEND_native_variant_11db_110distinct_final3_paper_2026-06-03.tar.zst` | 868185 | `05173b579fabdb7ff89828378e4293455cba063e776acef33cfa0e3753dfc80c` | Paper statistics, schemas, provenance, feature manifests, release JSON, and audit evidence, excluding raw `mongodb_data/`. |
| `SHA256SUMS.txt` | 269 | n/a | Package checksum sidecar. |

The full package preserves the source-build repository-relative path. From the
repository root, restore the raw witness data into the formal release with:

```bash
mkdir -p release/tend-native-mongodb-v1/mongodb_data
tar -I zstd -xf TEND_native_variant_11db_110distinct_final3_full_2026-06-03.tar.zst \
  -C release/tend-native-mongodb-v1/mongodb_data \
  --strip-components=4 \
  runs/native-variant-11db-110distinct-final3/dataset/mongodb_data
```

## GitHub-Tracked Assets

The repository keeps these formal release assets:

- `release/tend-native-mongodb-v1/data/TEND.json`
- `release/tend-native-mongodb-v1/data/test.json`
- `release/tend-native-mongodb-v1/data/TEND_lean.json`
- `release/tend-native-mongodb-v1/data/test_lean.json`
- `release/tend-native-mongodb-v1/data/TEND_lean.jsonl`
- `release/tend-native-mongodb-v1/data/bird_db_catalog.json`
- `release/tend-native-mongodb-v1/schema/mongodb_schema/`
- `release/tend-native-mongodb-v1/metadata/native_feature_manifest/`
- `release/tend-native-mongodb-v1/metadata/migration_recipe/`
- `release/tend-native-mongodb-v1/metadata/agent_design_rationale/`
- `release/tend-native-mongodb-v1/metadata/provenance/`
- `release/tend-native-mongodb-v1/audits/nl_mql/`
- `release/tend-native-mongodb-v1/audits/surgery/`
- `release/tend-native-mongodb-v1/statistics/paper_statistics/`
- `release/tend-native-mongodb-v1/external/`
- `release/tend-native-mongodb-v1/mongodb_data/README.md`

The repository does not use `runs/` as a release surface. `runs/` may remain on
the local machine as intermediate generation evidence, but it is ignored by Git.

## NLQ Convention

Each record has one canonical NLQ and one colloquial variant for the same MQL
target.

- Full release: `nl_queries.canonical` and `nl_queries.colloquial`
- Lean release: `NLQ` and `NLQ_colloquial`

Use the canonical field as the default evaluation query. Use the colloquial
field for robustness or paraphrase-style evaluation, not as a second independent
task.

## Key Statistics

The paper statistics are in
`release/tend-native-mongodb-v1/statistics/paper_statistics/`.

High-level release statistics:

- 1,210 NL-MQL records across 11 databases, 110 records per database.
- 2,420 NL texts after the canonical and colloquial NLQ fields are counted.
- 1,210 distinct MQL signatures.
- 107 global skeleton signatures and 207 database-scoped skeleton families.
- Median and max top-level aggregation stages: 7 and 12.
- Parsed aggregation pipelines: 1,210 of 1,210, with 0 parse errors.
- Fresh exact MongoDB execution verification: 1,210 executed, 0 failures, 0 empty outputs, 0 missing fields.

Most useful paper files:

- `paper_dataset_statistics.md`
- `paper_dataset_statistics.json`
- `paper_statistics_by_db.csv`
- `paper_tables.tex`
- `pipeline_stage_detailed_statistics.md`
- `pipeline_stage_detailed_statistics.json`
- `pipeline_stage_tables.tex`
- `pipeline_stage_complexity_by_record.csv`
- `fresh_exact_execution_by_db_verification.json`

## Validation Notes

The exact MongoDB execution verification is clean in
`release/tend-native-mongodb-v1/statistics/paper_statistics/fresh_exact_execution_by_db_verification.json`.
The release validator snapshot is also preserved, including issue category
statistics, because it checks a stricter publication contract than exact MongoDB
execution alone.
