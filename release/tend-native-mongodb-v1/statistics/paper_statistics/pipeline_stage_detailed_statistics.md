# Detailed Pipeline Stage Statistics

- Generated at: `2026-06-03T13:39:42.165961+00:00`
- Records parsed as aggregation pipelines: `1210/1210`
- Total top-level stage occurrences: `8479`
- Distinct stage types: `7`
- Distinct full stage sequences: `54`
- Max stage-sequence family: `204`

## Stage Count Distribution

| Stage count | Records | Share | Cumulative share |
|---:|---:|---:|---:|
| 3 | 1 | 0.1% | 0.1% |
| 4 | 6 | 0.5% | 0.6% |
| 5 | 29 | 2.4% | 3.0% |
| 6 | 390 | 32.2% | 35.2% |
| 7 | 408 | 33.7% | 68.9% |
| 8 | 291 | 24.0% | 93.0% |
| 9 | 79 | 6.5% | 99.5% |
| 12 | 6 | 0.5% | 100.0% |

## Stage Occurrence And Presence

| Stage | Role | Occurrences | Occurrence share | Record presence | Presence share | Repeated in records | Max repetitions |
|---|---|---:|---:|---:|---:|---:|---:|
| `$project` | `projection` | 2021 | 23.8% | 1200 | 99.2% | 808 | 3 |
| `$match` | `filter` | 1222 | 14.4% | 1093 | 90.3% | 129 | 2 |
| `$limit` | `result_bound` | 1209 | 14.3% | 1209 | 99.9% | 0 | 1 |
| `$sort` | `ordering` | 1208 | 14.2% | 1208 | 99.8% | 0 | 1 |
| `$addFields` | `enrichment` | 1139 | 13.4% | 1053 | 87.0% | 80 | 3 |
| `$unwind` | `array_traversal` | 1085 | 12.8% | 893 | 73.8% | 180 | 3 |
| `$group` | `aggregation` | 595 | 7.0% | 595 | 49.2% | 0 | 1 |

## Structural Buckets

| Bucket | Records | Share |
|---|---:|---:|
| `unwind_grouped` | 451 | 37.3% |
| `unwind_filter_project` | 308 | 25.5% |
| `enrich_filter_project` | 216 | 17.9% |
| `multi_unwind_grouped` | 134 | 11.1% |
| `linear_filter_project` | 91 | 7.5% |
| `group_without_unwind` | 10 | 0.8% |

## Depth Buckets

| Bucket | Records | Share |
|---|---:|---:|
| `medium_6_to_7` | 798 | 66.0% |
| `long_8_to_9` | 370 | 30.6% |
| `short_3_to_5` | 36 | 3.0% |
| `very_long_10_plus` | 6 | 0.5% |

## Top Stage Sequences

| Stage sequence | Records | Share | Stage count | Bucket |
|---|---:|---:|---:|---|
| `$addFields>$project>$unwind>$match>$project>$sort>$limit` | 204 | 16.9% | 7 | `unwind_filter_project` |
| `$addFields>$project>$match>$project>$sort>$limit` | 204 | 16.9% | 6 | `enrich_filter_project` |
| `$addFields>$project>$unwind>$match>$group>$project>$sort>$limit` | 131 | 10.8% | 8 | `unwind_grouped` |
| `$addFields>$project>$unwind>$match>$group>$sort>$limit` | 104 | 8.6% | 7 | `unwind_grouped` |
| `$addFields>$project>$unwind>$match>$group>$match>$sort>$limit` | 85 | 7.0% | 8 | `unwind_grouped` |
| `$addFields>$project>$unwind>$group>$sort>$limit` | 84 | 6.9% | 6 | `unwind_grouped` |
| `$addFields>$addFields>$match>$project>$sort>$limit` | 60 | 5.0% | 6 | `linear_filter_project` |
| `$unwind>$addFields>$project>$match>$project>$sort>$limit` | 24 | 2.0% | 7 | `unwind_filter_project` |
| `$addFields>$match>$project>$sort>$limit` | 21 | 1.7% | 5 | `linear_filter_project` |
| `$unwind>$addFields>$project>$unwind>$match>$group>$project>$sort>$limit` | 16 | 1.3% | 9 | `multi_unwind_grouped` |
| `$project>$unwind>$addFields>$match>$project>$sort>$limit` | 15 | 1.2% | 7 | `unwind_filter_project` |
| `$unwind>$project>$unwind>$match>$group>$project>$sort>$limit` | 14 | 1.2% | 8 | `multi_unwind_grouped` |
| `$unwind>$addFields>$project>$unwind>$match>$project>$sort>$limit` | 14 | 1.2% | 8 | `unwind_filter_project` |
| `$project>$unwind>$match>$group>$project>$sort>$limit` | 13 | 1.1% | 7 | `unwind_grouped` |
| `$project>$unwind>$unwind>$group>$match>$project>$sort>$limit` | 13 | 1.1% | 8 | `multi_unwind_grouped` |
| `$project>$unwind>$group>$project>$sort>$limit` | 12 | 1.0% | 6 | `unwind_grouped` |
| `$project>$unwind>$unwind>$match>$group>$project>$sort>$limit` | 12 | 1.0% | 8 | `multi_unwind_grouped` |
| `$project>$unwind>$unwind>$group>$addFields>$match>$project>$sort>$limit` | 11 | 0.9% | 9 | `multi_unwind_grouped` |
| `$unwind>$addFields>$project>$unwind>$match>$group>$sort>$limit` | 10 | 0.8% | 8 | `multi_unwind_grouped` |
| `$unwind>$project>$group>$project>$sort>$limit` | 7 | 0.6% | 6 | `unwind_grouped` |

## Top Internal Stage Transitions

| From | To | Count | Share of internal transitions |
|---|---|---:|---:|
| `$sort` | `$limit` | 1207 | 16.6% |
| `$addFields` | `$project` | 892 | 12.3% |
| `$project` | `$sort` | 888 | 12.2% |
| `$project` | `$unwind` | 843 | 11.6% |
| `$unwind` | `$match` | 662 | 9.1% |
| `$match` | `$project` | 657 | 9.0% |
| `$match` | `$group` | 441 | 6.1% |
| `$project` | `$match` | 263 | 3.6% |
| `$group` | `$project` | 238 | 3.3% |
| `$group` | `$sort` | 214 | 2.9% |
| `$addFields` | `$match` | 163 | 2.2% |
| `$unwind` | `$group` | 143 | 2.0% |
| `$group` | `$match` | 126 | 1.7% |
| `$unwind` | `$addFields` | 113 | 1.6% |
| `$match` | `$sort` | 106 | 1.5% |
| `$unwind` | `$unwind` | 85 | 1.2% |
| `$unwind` | `$project` | 82 | 1.1% |
| `$addFields` | `$addFields` | 80 | 1.1% |
| `$project` | `$addFields` | 19 | 0.3% |
| `$group` | `$addFields` | 17 | 0.2% |

## Per-DB Stage Complexity

| DB | Records | Median stages | P90 stages | Max stages | Distinct seq. | Max seq. family | Top seq. share | Unwind records | Group records | Unwind+group records |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `california_schools` | 110 | 7 | 8 | 9 | 10 | 26 | 23.6% | 84 | 57 | 57 |
| `card_games` | 110 | 7 | 8 | 8 | 11 | 21 | 19.1% | 79 | 62 | 60 |
| `codebase_community` | 110 | 7 | 9 | 12 | 9 | 26 | 23.6% | 88 | 62 | 62 |
| `debit_card_specializing` | 110 | 7 | 8 | 9 | 10 | 23 | 20.9% | 88 | 65 | 65 |
| `european_football_2` | 110 | 7 | 8 | 9 | 10 | 25 | 22.7% | 69 | 51 | 51 |
| `financial` | 110 | 7 | 8 | 8 | 10 | 21 | 19.1% | 84 | 60 | 60 |
| `formula_1` | 110 | 6 | 8 | 8 | 16 | 19 | 17.3% | 63 | 45 | 41 |
| `student_club` | 110 | 7 | 8 | 9 | 11 | 24 | 21.8% | 86 | 46 | 46 |
| `superhero` | 110 | 7 | 9 | 9 | 19 | 15 | 13.6% | 94 | 56 | 54 |
| `thrombosis_prediction` | 110 | 6 | 8 | 8 | 10 | 30 | 27.3% | 66 | 45 | 45 |
| `toxicology` | 110 | 7 | 9 | 9 | 16 | 14 | 12.7% | 92 | 46 | 44 |

## Generated CSV Files

- `pipeline_stage_summary.csv`: per-stage occurrence, presence, role, repetition, and boundary-position counts.
- `pipeline_stage_count_histogram.csv`: global stage-count histogram.
- `pipeline_stage_count_by_db.csv`: stage-count histogram within each DB.
- `pipeline_stage_by_db.csv`: per-DB stage-complexity summary.
- `pipeline_stage_occurrence_by_db.csv`: per-DB/per-stage occurrence and presence.
- `pipeline_stage_position_distribution.csv`: absolute 1-based stage-position distribution.
- `pipeline_stage_normalized_position_distribution.csv`: first/early/middle/late/last position distribution.
- `pipeline_stage_first_last_distribution.csv`: first, second, penultimate, and last-stage distributions.
- `pipeline_stage_transition_distribution.csv`: adjacent internal stage bigrams.
- `pipeline_stage_boundary_transition_distribution.csv`: start and end boundary stage distributions.
- `pipeline_stage_sequence_distribution_detailed.csv`: full sequence families with structural buckets.
- `pipeline_stage_structural_buckets.csv`: coarse structural stage families.
- `pipeline_stage_depth_buckets.csv`: short/medium/long/very-long stage-count buckets.
- `pipeline_stage_repeated_stage_combinations.csv`: repeated stage combinations inside a pipeline.
- `pipeline_stage_role_distribution.csv`: stage role occurrence and record presence.
- `pipeline_stage_complexity_by_record.csv`: record-level stage complexity labels and counts.
- `pipeline_stage_collection_bucket_distribution.csv`: collection-level structural bucket counts.
- `pipeline_stage_tables.tex`: LaTeX tables for stage-count, stage-type, and structural-bucket statistics.
