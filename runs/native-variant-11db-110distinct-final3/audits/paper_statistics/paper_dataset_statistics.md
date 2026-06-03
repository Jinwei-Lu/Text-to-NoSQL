# Paper-Level TEND Dataset Statistics

- Generated at: `2026-06-03T13:40:06.577756+00:00`
- Run directory: `/Users/jwlu18/Developer/TEND/runs/native-variant-11db-110distinct-final3`
- Primary statistics source: `dataset/TEND.json`; lean release shape checked against `TEND_lean.json` and `TEND_lean.jsonl`.

## Main-Text Candidate Summary

| Metric | Value |
|---|---:|
| Databases | `11` |
| NL-MQL tasks | `1210` |
| NL utterances | `2420` |
| Schema collections / queried collections | `32 / 30` |
| MongoDB documents | `269,177` |
| Aggregation pipelines | `1210` |
| Median / max stages | `7 / 12` |
| Median unique operators | `12` |
| Unique MQL signatures | `1210` |
| Global / DB-scoped skeleton families | `107 / 207` |
| Native query patterns | `85` |
| Native feature ids | `43` |
| Dynamic-key operator records | `1095 (90.5%)` |
| Array-operator records | `1175 (97.1%)` |
| Nested dotted-path records | `1177 (97.3%)` |
| Fresh exact execution | `1210/1210` |
| Native verification metadata | `{True: 1209, False: 1}` |

## Diversity And Concentration

- Exact MQL signatures: `1210/1210`.
- Distinct NL texts: `2420/2420`; distinct NL pairs: `1210/1210`.
- Global skeleton signatures: `107`; db-scoped skeleton families: `207`.
- Max global skeleton family: `102`; max db-scoped skeleton family: `13`.
- Top-5 global skeleton coverage: `40.1%`; top-10 coverage: `64.5%`.
- Distinct stage sequences: `54`; max stage-sequence family: `204`.
- Top-5 stage-sequence coverage: `60.2%`; top-10 coverage: `77.1%`.

## Complexity Distributions

| Metric | Min | P25 | Median | P75 | P90 | Max | Mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| `stage_count` | 3 | 6 | 7 | 8 | 8 | 12 | 7.0074 |
| `unique_operator_count` | 5 | 9 | 12 | 13 | 14 | 19 | 11.3033 |
| `stage_operator_occurrence_count` | 5 | 12 | 14 | 15 | 17 | 40 | 14.1372 |
| `mql_chars` | 384 | 603.25 | 673 | 771 | 918 | 1467 | 717.2537 |
| `canonical_nl_words` | 14 | 29.25 | 32 | 33 | 34 | 40 | 29.5215 |
| `colloquial_nl_words` | 8 | 22 | 22 | 23 | 24 | 30 | 20.7876 |
| `field_path_reference_count` | 0 | 4 | 5 | 6 | 11 | 18 | 5.4702 |
| `max_field_path_depth` | 0 | 3 | 3 | 4 | 5 | 6 | 3.4479 |
| `limit_value` | 8 | 13 | 25 | 50 | 100 | 100 | 39.7519 |

## Detailed Pipeline Stage Summary

- Total top-level stage occurrences: `8479`.
- Distinct stage types: `7`.
- Distinct full stage sequences: `54`; max sequence family: `204`.
- Top-5 stage-sequence coverage: `60.2%`; top-10 coverage: `77.1%`.
- Stage-count histogram: `{'3': 1, '4': 6, '5': 29, '6': 390, '7': 408, '8': 291, '9': 79, '12': 6}`.
- Structural buckets: `{'multi_unwind_grouped': 134, 'unwind_filter_project': 308, 'unwind_grouped': 451, 'enrich_filter_project': 216, 'linear_filter_project': 91, 'group_without_unwind': 10}`.
- Full detail is in `pipeline_stage_detailed_statistics.md` and the `pipeline_stage_*.csv` files.

## Mongo-Native Semantic Signals

| Signal | Records | Share |
|---|---:|---:|
| `array_operator` | 1175 | 97.1% |
| `dynamic_key_operator` | 1095 | 90.5% |
| `expr_match` | 207 | 17.1% |
| `grouping_operator` | 616 | 50.9% |
| `has_conditional` | 559 | 46.2% |
| `has_filter_or_map` | 225 | 18.6% |
| `has_group` | 595 | 49.2% |
| `has_limit` | 1209 | 99.9% |
| `has_match` | 1093 | 90.3% |
| `has_project` | 1200 | 99.2% |
| `has_sort` | 1208 | 99.8% |
| `has_unwind` | 893 | 73.8% |
| `nested_dotted_path` | 1177 | 97.3% |

## Metadata Distributions

- `schema_flex`: `{'nested_event_stream': 130, 'dynamic_key': 1013, 'attribute_bag': 13, 'polymorphic': 36, 'missing_vs_present': 18}`
- `shape_policy`: `{'reduce': 144, 'reshape': 94, 'preserve': 972}`
- `native_feature_type`: `{'dynamic_key_object': 1097, 'polymorphic_collection': 31, 'nested_event_stream': 70, 'missing_vs_present': 12}`
- `anti_sql_transfer_level`: `{'strong': 1138, 'medium': 71, 'weak': 1}`
- `difficulty`: `{'L4': 1210}`
- `sql_infeasibility_class`: `{'structural_schema_flex': 1210}`
- `native_verification_ok`: `{True: 1209, False: 1}`

## Release Validator Caveat

- `tend validate` status: `INVALID`.
- Record-level validator issues: `1616`; schema issues: `0`; file issues: `0`.
- Category counts:

| Category | Issues | Affected records | Issue share | Record share |
|---|---:|---:|---:|---:|
| `ast_allowlist_check` | 690 | 690 | 42.7% | 57.0% |
| `unresolved_provenance_refs` | 498 | 498 | 30.8% | 41.2% |
| `claimed_native_constructs_absent` | 233 | 233 | 14.4% | 19.3% |
| `feature_path_not_accessed` | 160 | 160 | 9.9% | 13.2% |
| `dynamic_key_requires_objectToArray_path` | 19 | 19 | 1.2% | 1.6% |
| `nested_event_filter_requirement` | 15 | 8 | 0.9% | 0.7% |
| `polymorphic_discriminator_requirement` | 1 | 1 | 0.1% | 0.1% |

This validator gate is distinct from exact Mongo execution: the validator issues are primarily metadata/provenance/native-feature gate mismatches, while the exact execution report records runtime query execution.

## Per-Database Table

| DB | Pairs | Queried collections | Schema collections | Docs | Skeletons | Max family | Median stages | Median unique ops | Query patterns | Native features |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `california_schools` | 110 | 3 | 3 | 19,172 | 14 | 13 | 7 | 12 | 7 | 3 |
| `card_games` | 110 | 2 | 2 | 57,373 | 16 | 11 | 7 | 12 | 8 | 5 |
| `codebase_community` | 110 | 3 | 3 | 84,269 | 13 | 11 | 7 | 11 | 6 | 3 |
| `debit_card_specializing` | 110 | 3 | 3 | 38,768 | 13 | 11 | 7 | 11 | 7 | 4 |
| `european_football_2` | 110 | 4 | 4 | 37,426 | 18 | 8 | 7 | 12 | 7 | 4 |
| `financial` | 110 | 4 | 4 | 9,092 | 18 | 10 | 7 | 12 | 9 | 5 |
| `formula_1` | 110 | 2 | 2 | 2,074 | 23 | 7 | 6 | 9 | 12 | 5 |
| `student_club` | 110 | 2 | 2 | 75 | 16 | 11 | 7 | 11 | 9 | 3 |
| `superhero` | 110 | 3 | 4 | 952 | 24 | 7 | 7 | 11 | 7 | 4 |
| `thrombosis_prediction` | 110 | 3 | 3 | 1,366 | 26 | 7 | 6 | 12 | 7 | 4 |
| `toxicology` | 110 | 1 | 2 | 18,610 | 26 | 7 | 7 | 12 | 9 | 3 |

## Notes For Paper Wording

- Report exact query diversity and skeleton-level concentration together. The dataset has fully unique exact MQL strings, but skeleton families are intentionally reused across semantically different MongoDB domains.
- Distinguish global skeleton signatures from db-scoped skeleton families. The H11-style diversity cap is db-scoped; global family concentration is a separate descriptive statistic.
- Distinguish operator occurrence from operator presence. Occurrence counts nested expression operators repeatedly; presence counts each operator at most once per record.
- Distinguish exact Mongo execution from native-verification metadata. The execution report records runtime execution status; `native_verification.ok` records a metadata/native-feature gate and currently has one flagged record.

## Generated Files

- `paper_dataset_statistics.json`: complete machine-readable statistics.
- `paper_statistics_by_db.csv`: per-database table for papers/appendix.
- `operator_statistics.csv`: operator occurrence and per-record presence.
- `stage_statistics.csv`: stage occurrence and per-record presence.
- `skeleton_concentration.csv`: global skeleton and stage-sequence concentration.
- `feature_statistics.csv`: schema/feature/anti-SQL metadata distributions.
- `paper_tables.tex`: LaTeX tables using booktabs.
- `release_validator_snapshot.txt/.json`: full CLI validator snapshot.
- `release_validator_issue_statistics.json`: structured validator issue summary.
- `release_validator_issue_categories.csv`: validator issues by category.
- `release_validator_issue_by_db_category.csv`: validator issues by DB/category.
- `release_validator_issue_samples.csv`: first 250 validator issue samples.
- `fresh_exact_execution_by_db_verification.json`: fresh exact Mongo execution verification run by DB.
- `pipeline_stage_detailed_statistics.json/.md`: detailed top-level aggregation stage complexity statistics.
- `pipeline_stage_summary.csv`: per-stage occurrence, presence, repetition, and boundary-position counts.
- `pipeline_stage_by_db.csv`: per-DB stage-complexity summary.
- `pipeline_stage_occurrence_by_db.csv`: per-DB/per-stage occurrence and presence.
- `pipeline_stage_count_histogram.csv` and `pipeline_stage_count_by_db.csv`: pipeline-length distributions.
- `pipeline_stage_position_distribution.csv` and `pipeline_stage_normalized_position_distribution.csv`: absolute and normalized stage positions.
- `pipeline_stage_transition_distribution.csv`: adjacent internal stage bigrams.
- `pipeline_stage_sequence_distribution_detailed.csv`: full stage-sequence families.
- `pipeline_stage_structural_buckets.csv`, `pipeline_stage_depth_buckets.csv`, and `pipeline_stage_role_distribution.csv`: derived stage-complexity families.
- `pipeline_stage_tables.tex`: LaTeX tables for stage complexity.
