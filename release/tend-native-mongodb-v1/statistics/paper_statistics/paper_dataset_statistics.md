# Paper-Level TEND Dataset Statistics

- Generated at: `2026-06-05T02:35:05.673496+00:00`
- Release directory: `/Users/jwlu18/Developer/TEND/release/tend-native-mongodb-v1`
- Primary statistics source: `data/TEND_lean.json`.
- Public record fields: `['record_id', 'db_id', 'NLQ', 'NLQ_colloquial', 'MQL']`.

## Main-Text Candidate Summary

| Metric | Value |
|---|---:|
| Databases | `11` |
| NL-MQL tasks | `1210` |
| Public record fields | `record_id, db_id, NLQ, NLQ_colloquial, MQL` |
| NL utterances | `2420` |
| Schema collections / queried collections | `32 / 30` |
| MongoDB documents | `269,177` |
| Aggregation pipelines | `1210` |
| Median / max stages | `7 / 12` |
| Median unique operators | `12` |
| Unique MQL signatures | `1210` |
| Global / DB-scoped skeleton families | `1035 / 1104` |
| Dynamic-key operator records | `1104 (91.2%)` |
| Array-operator records | `1172 (96.9%)` |
| Nested dotted-path records | `1162 (96.0%)` |
| Public contract | `OK` |
| Fresh exact execution | `1210/1210` |

## Public Contract

- Status: `OK`.
- Records: `1210`.
- DB count: `11`.
- Distinct MQL strings: `1210`.
- Distinct canonical NLQ strings: `1210`.

## Diversity And Concentration

- Exact MQL strings: `1210/1210`.
- Exact MQL signatures: `1210/1210`.
- Distinct NL texts: `2420/2420`; distinct NL pairs: `1210/1210`.
- Global skeleton signatures: `1035`; DB-scoped skeleton families: `1104`.
- Max global skeleton family: `7`; max DB-scoped skeleton family: `7`.
- Top-5 global skeleton coverage: `2.5%`; top-10 coverage: `4.4%`.
- Distinct stage sequences: `248`; max stage-sequence family: `126`.
- Top-5 stage-sequence coverage: `31.3%`; top-10 coverage: `43.8%`.

## Complexity Distributions

| Metric | Min | P25 | Median | P75 | P90 | Max | Mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| `stage_count` | 3 | 6 | 7 | 8 | 9 | 12 | 7.2066 |
| `unique_operator_count` | 4 | 10 | 12 | 13 | 14 | 19 | 11.4983 |
| `stage_operator_occurrence_count` | 4 | 12 | 14 | 17 | 20 | 40 | 14.6736 |
| `mql_chars` | 170 | 530 | 644 | 811 | 1051.5 | 2822 | 707.2719 |
| `canonical_nl_words` | 10 | 44 | 57 | 73 | 88.1 | 146 | 59.3884 |
| `colloquial_nl_words` | 8 | 35 | 47 | 59 | 73 | 127 | 48.5537 |
| `field_path_reference_count` | 0 | 3 | 5 | 8 | 11 | 26 | 5.7289 |
| `max_field_path_depth` | 0 | 3 | 3 | 4 | 5 | 6 | 3.2901 |
| `limit_value` | 8 | 12 | 25 | 50 | 100 | 100 | 39.5971 |

## Detailed Pipeline Stage Summary

- Total top-level stage occurrences: `8720`.
- Distinct stage types: `10`.
- Distinct full stage sequences: `248`; max sequence family: `126`.
- Top-5 stage-sequence coverage: `31.3%`; top-10 coverage: `43.8%`.
- Stage-count histogram: `{'3': 1, '4': 22, '5': 148, '6': 212, '7': 295, '8': 328, '9': 137, '10': 51, '11': 7, '12': 9}`.
- Structural buckets: `{'multi_unwind_grouped': 193, 'unwind_filter_project': 298, 'unwind_grouped': 397, 'enrich_filter_project': 63, 'linear_filter_project': 248, 'group_without_unwind': 11}`.

## Mongo-Native Semantic Signals

| Signal | Records | Share |
|---|---:|---:|
| `array_operator` | 1172 | 96.9% |
| `dynamic_key_operator` | 1104 | 91.2% |
| `expr_match` | 208 | 17.2% |
| `grouping_operator` | 634 | 52.4% |
| `has_conditional` | 536 | 44.3% |
| `has_filter_or_map` | 338 | 27.9% |
| `has_group` | 601 | 49.7% |
| `has_limit` | 1189 | 98.3% |
| `has_match` | 1035 | 85.5% |
| `has_project` | 1210 | 100.0% |
| `has_sort` | 1207 | 99.8% |
| `has_unwind` | 888 | 73.4% |
| `nested_dotted_path` | 1162 | 96.0% |

## Per-DB Summary

| DB | Records | Collections | Docs | Skeletons | Max family | Median stages | Median unique ops | Dynamic-key records | Array records | Grouping records |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `california_schools` | 110 | 3 | 19,172 | 94 | 5 | 8 | 12 | 110 | 110 | 59 |
| `card_games` | 110 | 2 | 57,373 | 103 | 4 | 7 | 12 | 106 | 105 | 64 |
| `codebase_community` | 110 | 3 | 84,269 | 101 | 3 | 8 | 11 | 110 | 110 | 65 |
| `debit_card_specializing` | 110 | 3 | 38,768 | 101 | 4 | 7 | 11 | 103 | 110 | 68 |
| `european_football_2` | 110 | 4 | 37,426 | 100 | 6 | 7 | 12.5 | 88 | 110 | 53 |
| `financial` | 110 | 4 | 9,092 | 106 | 3 | 7 | 12 | 110 | 110 | 64 |
| `formula_1` | 110 | 2 | 2,074 | 97 | 6 | 6 | 10 | 71 | 84 | 52 |
| `student_club` | 110 | 2 | 75 | 100 | 4 | 7 | 11 | 107 | 110 | 50 |
| `superhero` | 110 | 3 | 952 | 96 | 7 | 7 | 11 | 107 | 107 | 58 |
| `thrombosis_prediction` | 110 | 3 | 1,366 | 102 | 3 | 7 | 12 | 86 | 110 | 48 |
| `toxicology` | 110 | 1 | 18,610 | 104 | 4 | 7 | 12 | 106 | 106 | 53 |

## Generated Files

- `paper_dataset_statistics.json`: complete lean public dataset statistics.
- `paper_statistics_by_db.csv`: per-DB summary derived from public MQL/NLQ plus schema document counts.
- `operator_statistics.csv`: MongoDB operator occurrence and per-record presence.
- `stage_statistics.csv`: top-level aggregation stage occurrence and per-record presence.
- `skeleton_concentration.csv`: MQL skeleton and stage-sequence concentration.
- `feature_statistics.csv`: derived semantic-signal and structural-bucket counts.
- `pipeline_stage_detailed_statistics.*` and `pipeline_stage_*.csv`: detailed stage distributions generated from the same lean source.
