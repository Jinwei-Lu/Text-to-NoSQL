# Detailed Pipeline Stage Statistics

- Generated at: `2026-06-05T02:34:30.937008+00:00`
- Records parsed as aggregation pipelines: `1210/1210`
- Total top-level stage occurrences: `8720`
- Distinct stage types: `10`
- Distinct full stage sequences: `248`
- Max stage-sequence family: `126`

## Stage Count Distribution

| Stage count | Records | Share | Cumulative share |
|---:|---:|---:|---:|
| 3 | 1 | 0.1% | 0.1% |
| 4 | 22 | 1.8% | 1.9% |
| 5 | 148 | 12.2% | 14.1% |
| 6 | 212 | 17.5% | 31.7% |
| 7 | 295 | 24.4% | 56.0% |
| 8 | 328 | 27.1% | 83.1% |
| 9 | 137 | 11.3% | 94.5% |
| 10 | 51 | 4.2% | 98.7% |
| 11 | 7 | 0.6% | 99.3% |
| 12 | 9 | 0.7% | 100.0% |

## Stage Occurrence And Presence

| Stage | Role | Occurrences | Occurrence share | Record presence | Presence share | Repeated in records | Max repetitions |
|---|---|---:|---:|---:|---:|---:|---:|
| `$project` | `projection` | 2146 | 24.6% | 1210 | 100.0% | 867 | 4 |
| `$addFields` | `enrichment` | 1223 | 14.0% | 974 | 80.5% | 206 | 5 |
| `$sort` | `ordering` | 1210 | 13.9% | 1207 | 99.8% | 3 | 2 |
| `$limit` | `result_bound` | 1189 | 13.6% | 1189 | 98.3% | 0 | 1 |
| `$match` | `filter` | 1172 | 13.4% | 1035 | 85.5% | 133 | 3 |
| `$unwind` | `array_traversal` | 1169 | 13.4% | 888 | 73.4% | 253 | 4 |
| `$group` | `aggregation` | 602 | 6.9% | 601 | 49.7% | 1 | 2 |
| `$replaceRoot` | `other` | 5 | 0.1% | 5 | 0.4% | 0 | 1 |
| `$set` | `other` | 3 | 0.0% | 3 | 0.2% | 0 | 1 |
| `$unset` | `other` | 1 | 0.0% | 1 | 0.1% | 0 | 1 |

## Structural Buckets

| Bucket | Records | Share |
|---|---:|---:|
| `unwind_grouped` | 397 | 32.8% |
| `unwind_filter_project` | 298 | 24.6% |
| `linear_filter_project` | 248 | 20.5% |
| `multi_unwind_grouped` | 193 | 16.0% |
| `enrich_filter_project` | 63 | 5.2% |
| `group_without_unwind` | 11 | 0.9% |

## Depth Buckets

| Bucket | Records | Share |
|---|---:|---:|
| `medium_6_to_7` | 507 | 41.9% |
| `long_8_to_9` | 465 | 38.4% |
| `short_3_to_5` | 171 | 14.1% |
| `very_long_10_plus` | 67 | 5.5% |

## Top Stage Sequences

| Stage sequence | Records | Share | Stage count | Bucket |
|---|---:|---:|---:|---|
| `$addFields>$project>$unwind>$match>$group>$project>$sort>$limit` | 126 | 10.4% | 8 | `unwind_grouped` |
| `$addFields>$project>$unwind>$match>$project>$sort>$limit` | 91 | 7.5% | 7 | `unwind_filter_project` |
| `$addFields>$match>$project>$sort>$limit` | 68 | 5.6% | 5 | `linear_filter_project` |
| `$addFields>$addFields>$match>$project>$sort>$limit` | 55 | 4.5% | 6 | `linear_filter_project` |
| `$addFields>$project>$unwind>$match>$group>$sort>$limit>$project` | 39 | 3.2% | 8 | `unwind_grouped` |
| `$addFields>$project>$unwind>$group>$sort>$limit>$project` | 36 | 3.0% | 7 | `unwind_grouped` |
| `$project>$unwind>$match>$project>$sort>$limit` | 35 | 2.9% | 6 | `unwind_filter_project` |
| `$addFields>$project>$unwind>$unwind>$match>$group>$match>$sort>$limit>$project` | 32 | 2.6% | 10 | `multi_unwind_grouped` |
| `$addFields>$project>$match>$sort>$limit` | 27 | 2.2% | 5 | `linear_filter_project` |
| `$addFields>$project>$unwind>$group>$project>$sort>$limit` | 21 | 1.7% | 7 | `unwind_grouped` |
| `$addFields>$project>$unwind>$match>$group>$match>$sort>$limit>$project` | 21 | 1.7% | 9 | `unwind_grouped` |
| `$addFields>$project>$unwind>$addFields>$group>$sort>$limit>$project` | 19 | 1.6% | 8 | `unwind_grouped` |
| `$addFields>$project>$unwind>$match>$project>$sort>$limit>$project` | 18 | 1.5% | 8 | `unwind_filter_project` |
| `$addFields>$unwind>$match>$project>$sort>$limit` | 18 | 1.5% | 6 | `unwind_filter_project` |
| `$addFields>$match>$sort>$limit>$project` | 17 | 1.4% | 5 | `linear_filter_project` |
| `$addFields>$project>$match>$project>$sort>$limit` | 17 | 1.4% | 6 | `enrich_filter_project` |
| `$project>$unwind>$match>$group>$project>$sort>$limit` | 16 | 1.3% | 7 | `unwind_grouped` |
| `$unwind>$project>$unwind>$match>$group>$project>$sort>$limit` | 15 | 1.2% | 8 | `multi_unwind_grouped` |
| `$addFields>$unwind>$project>$group>$project>$sort>$limit` | 14 | 1.2% | 7 | `unwind_grouped` |
| `$project>$unwind>$group>$project>$sort>$limit` | 14 | 1.2% | 6 | `unwind_grouped` |

## Top Internal Stage Transitions

| From | To | Count | Share of internal transitions |
|---|---|---:|---:|
| `$sort` | `$limit` | 1157 | 15.4% |
| `$project` | `$sort` | 841 | 11.2% |
| `$project` | `$unwind` | 773 | 10.3% |
| `$unwind` | `$match` | 631 | 8.4% |
| `$addFields` | `$project` | 623 | 8.3% |
| `$match` | `$project` | 505 | 6.7% |
| `$match` | `$group` | 384 | 5.1% |
| `$limit` | `$project` | 322 | 4.3% |
| `$group` | `$project` | 319 | 4.2% |
| `$addFields` | `$match` | 290 | 3.9% |
| `$match` | `$sort` | 203 | 2.7% |
| `$unwind` | `$group` | 151 | 2.0% |
| `$group` | `$sort` | 151 | 2.0% |
| `$addFields` | `$addFields` | 150 | 2.0% |
| `$unwind` | `$addFields` | 145 | 1.9% |
| `$unwind` | `$unwind` | 145 | 1.9% |
| `$project` | `$match` | 116 | 1.5% |
| `$group` | `$match` | 108 | 1.4% |
| `$addFields` | `$unwind` | 101 | 1.3% |
| `$unwind` | `$project` | 97 | 1.3% |

## Per-DB Stage Complexity

| DB | Records | Median stages | P90 stages | Max stages | Distinct seq. | Max seq. family | Top seq. share | Unwind records | Group records | Unwind+group records |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `california_schools` | 110 | 8 | 9 | 11 | 36 | 24 | 21.8% | 84 | 58 | 58 |
| `card_games` | 110 | 7 | 8.1 | 10 | 39 | 21 | 19.1% | 76 | 62 | 58 |
| `codebase_community` | 110 | 8 | 10 | 12 | 45 | 15 | 13.6% | 88 | 62 | 62 |
| `debit_card_specializing` | 110 | 7 | 9 | 11 | 41 | 13 | 11.8% | 88 | 65 | 65 |
| `european_football_2` | 110 | 7 | 9 | 10 | 43 | 10 | 9.1% | 67 | 52 | 52 |
| `financial` | 110 | 7 | 9 | 11 | 44 | 16 | 14.5% | 84 | 59 | 59 |
| `formula_1` | 110 | 6 | 8.1 | 11 | 50 | 12 | 10.9% | 64 | 46 | 42 |
| `student_club` | 110 | 7 | 9 | 10 | 40 | 14 | 12.7% | 85 | 45 | 45 |
| `superhero` | 110 | 7 | 10 | 12 | 65 | 7 | 6.4% | 95 | 57 | 56 |
| `thrombosis_prediction` | 110 | 7 | 9 | 10 | 40 | 19 | 17.3% | 66 | 45 | 45 |
| `toxicology` | 110 | 7 | 9 | 9 | 45 | 11 | 10.0% | 91 | 50 | 48 |

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
