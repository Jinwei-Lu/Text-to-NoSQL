# TEND Final Artifact Index

Final run id: `native-variant-11db-110distinct-final3`

GitHub tracks the lightweight release assets needed for paper writing,
inspection, schema review, and statistics. The large MongoDB witness data is
kept outside Git and should be distributed through the Google Drive folder
below.

Google Drive folder:
https://drive.google.com/drive/folders/1s7LgW-zub1gIx9A1OpuWdx7lyNVwXhi5

## Drive Packages

The local packages prepared for Google Drive upload are:

| Package | Size bytes | SHA256 | Contents |
| --- | ---: | --- | --- |
| `TEND_native_variant_11db_110distinct_final3_full_2026-06-03.tar.zst` | 224916368 | `3fabc19772bbe6e70c322944a3e7ce0e1a29c3c753ee04cb30d26580622f2594` | Full final run, including the 11 MongoDB JSON witness exports. |
| `TEND_native_variant_11db_110distinct_final3_paper_2026-06-03.tar.zst` | 868185 | `05173b579fabdb7ff89828378e4293455cba063e776acef33cfa0e3753dfc80c` | Paper statistics, schemas, provenance, feature manifests, release JSON, and audit evidence, excluding raw `mongodb_data/`. |
| `SHA256SUMS.txt` | 269 | n/a | Package checksum sidecar. |

The package tarballs preserve repository-relative paths. From the repository
root, restore with:

```bash
tar -I zstd -xf TEND_native_variant_11db_110distinct_final3_full_2026-06-03.tar.zst
```

## GitHub-Tracked Assets

The repository keeps these final-run assets:

- `runs/native-variant-11db-110distinct-final3/dataset/TEND.json`
- `runs/native-variant-11db-110distinct-final3/dataset/test.json`
- `runs/native-variant-11db-110distinct-final3/dataset/TEND_lean.json`
- `runs/native-variant-11db-110distinct-final3/dataset/test_lean.json`
- `runs/native-variant-11db-110distinct-final3/dataset/TEND_lean.jsonl`
- `runs/native-variant-11db-110distinct-final3/dataset/bird_db_catalog.json`
- `runs/native-variant-11db-110distinct-final3/dataset/mongodb_schema/`
- `runs/native-variant-11db-110distinct-final3/dataset/native_feature_manifest/`
- `runs/native-variant-11db-110distinct-final3/dataset/migration_recipe/`
- `runs/native-variant-11db-110distinct-final3/dataset/agent_design_rationale/`
- `runs/native-variant-11db-110distinct-final3/dataset/provenance/`
- `runs/native-variant-11db-110distinct-final3/audits/paper_statistics/`
- `runs/native-variant-11db-110distinct-final3/audits/surgery/post_surgery_exact_execution.json`
- `runs/native-variant-11db-110distinct-final3/audits/surgery/surgical_nl_mql_patch_report.json`
- `runs/native-variant-11db-110distinct-final3/audits/surgery/surgical_nl_mql_patch_report.md`

## Key Statistics

The paper statistics are in
`runs/native-variant-11db-110distinct-final3/audits/paper_statistics/`.

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
`audits/paper_statistics/fresh_exact_execution_by_db_verification.json`.
The release validator snapshot is also preserved, including issue category
statistics, because it checks a stricter publication contract than exact MongoDB
execution alone.
