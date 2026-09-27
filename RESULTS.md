# TEND final results

This page records the final experimental results of the TEND project (the August 2026 experiment campaign), which numbers they supersede, and how each number was produced.

## Setup

- **Benchmark.** The native MongoDB release `tend-native-mongodb-v1`: 1,210 natural-language questions over 11 MongoDB databases, 110 per database. Every system is scored on all 1,210 questions. A missing, rejected, or failing answer counts as wrong and stays in the denominator.
- **Metric.** `EXC`, execution accuracy that ignores column names and tolerates at most two surplus columns per row (β = 2). Comparisons between two systems are exact two-sided McNemar tests on the same questions; "W / L" counts the questions only the first system answers correctly and the questions only the second one does.
- **Models.** DeepSeek-V4-Flash (`deepseek/deepseek-v4-flash`, the backbone of the submitted paper) and GPT-5.6-Luna (`openai/gpt-5.6-luna`), both called through OpenRouter at temperature 0.
- **Code.** Commit `260801ea` of this repository ("Publish the final code used for the August 2026 experiments") is the code the experiments ran on. It matches the code snapshot recorded by the final ablation campaign byte for byte, except for the line endings of one file; the earlier August runs recorded no source hash. Commit `d4ac7d2` keeps only the code the final experiments, the metric checks, dataset construction and the demo use, and adds packaging, the runtime files under `proposals/`, documentation and the command-line help text. In offline stub runs it gives the same prompts, answers and disclosure fields as `260801ea` for every system and ablation arm on this page, and a recording provider receives the same requests from both. Its ReAct step budget defaults to 50, the value the final runs set. The June arms of the submitted version below were removed after `260801ea`; run them from that commit. Later commits change no code under `src/`. They update the documentation and add the per-question answers in `results/` with their checker. SAG runs in its revised default configuration: dynamic-key (field-group) recognition, the identifier card, and value-witness handling.

## Main results

### DeepSeek-V4-Flash

| system | correct | EXC | EXF1 | SAG vs system |
|---|---:|---:|---:|---|
| **SAG** | **487** | **40.2%** | **43.8** | — |
| ReAct, informed (real collection names and first five rows)† | 373 | 30.8% | 34.6 | 205 W / 91 L, p = 2.9e-11 |
| Direct (data-rich prompt) | 352 | 29.1% | 29.7 | 214 W / 79 L, p = 1.5e-15 |
| Direct with sampled documents† | 346 | 28.6% | 29.4 | 214 W / 73 L, p = 3.1e-17 |
| DIN-SQL-inspired MQL adaptation | 339 | 28.0% | 30.8 | 215 W / 67 L, p = 2.9e-19 |
| SQL Pivot without the relational DDL† | 310 | 25.6% | 26.2 | 246 W / 69 L, p = 1.9e-24 |
| Direct with the schema only† | 3 | 0.2% | 0.4 | 484 W / 0 L, p = 4e-146 |
| Direct with the question only† | 2 | 0.2% | 0.2 | 485 W / 0 L, p = 2e-146 |

† June 2026 runs of the submitted version, made through its provider route and not re-run. They remain current, and the paper reports them unchanged (as Sampled-doc Direct, SQL Pivot, Schema Direct, and NLQ-only Direct). ReAct is `react_informed` in the current code. The other four arms, `direct`, `sql_pivot`, `schema_direct`, and `direct_nlq_only`, exist only up to commit `260801ea`.

The DIN-SQL-inspired adaptation (schema linking, classification and decomposition, class-conditioned generation, self-correction, six fixed examples from MongoDB's public sample databases) is not distinguishable from Direct: 88 W / 101 L, p = 0.38.

### GPT-5.6-Luna

| system | correct | EXC | EXF1 | SAG vs system |
|---|---:|---:|---:|---|
| **SAG** | **514** | **42.5%** | **46.3** | — |
| Direct with SAG's six output conventions | 445 | 36.8% | 40.7 | 152 W / 83 L, p = 8.0e-6 |
| Direct (data-rich prompt) | 421 | 34.8% | 37.4 | 176 W / 83 L, p = 7.6e-9 |
| ReAct, informed (real collection names and first five rows) | 390 | 32.2% | 35.0 | 207 W / 83 L, p = 2.2e-13 |
| SQL Pivot given the real relational DDL | 269 | 22.2% | 24.3 | 295 W / 50 L, p = 2.1e-43 |

### Outcome breakdown

Percent of the 1,210 questions, as in Tables III and IV of the paper. Fail is `no_submission`, Exec `exec_error`, Empty `empty`, Struct. `order_only`, `row_subset`, and `row_superset` together, and Value `value_mismatch`. The remainder is EXC. No system has an `invalid` or `row_count_exceeded` answer here.

| system | Fail | Exec | Empty | Struct. | Value |
|---|---:|---:|---:|---:|---:|
| **DeepSeek-V4-Flash** | | | | | |
| SAG | 1.7 | 0.0 | 1.0 | 2.2 | 54.9 |
| ReAct, informed | 9.0 | 0.2 | 0.3 | 2.7 | 56.9 |
| Direct (data-rich prompt) | 2.7 | 2.1 | 5.8 | 1.1 | 59.2 |
| Direct with sampled documents | 2.1 | 4.7 | 7.2 | 1.7 | 55.7 |
| DIN-SQL-inspired MQL adaptation | 5.1 | 3.9 | 6.0 | 1.8 | 55.2 |
| SQL Pivot without the relational DDL | 1.5 | 4.9 | 8.8 | 1.5 | 57.8 |
| Direct with the schema only | 6.3 | 15.3 | 69.3 | 0.1 | 8.8 |
| Direct with the question only | 8.2 | 1.1 | 89.4 | 0.0 | 1.2 |
| **GPT-5.6-Luna** | | | | | |
| SAG | 0.2 | 0.0 | 0.2 | 1.7 | 55.4 |
| Direct with SAG's six output conventions | 2.4 | 1.3 | 2.3 | 1.8 | 55.4 |
| Direct (data-rich prompt) | 4.0 | 1.7 | 1.5 | 1.2 | 56.9 |
| ReAct, informed | 9.0 | 0.7 | 3.6 | 1.3 | 53.2 |
| SQL Pivot given the real relational DDL | 2.6 | 3.9 | 4.7 | 0.9 | 65.7 |

### Correct answers per database

DeepSeek-V4-Flash:

| system | california | card_games | codebase | debit_card | eu_football | financial | formula_1 | student_club | superhero | thrombosis | toxicology | total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SAG | 34 | 59 | 34 | 32 | 54 | 56 | 18 | 47 | 48 | 48 | 57 | **487** |
| Direct | 24 | 45 | 23 | 22 | 30 | 56 | 19 | 45 | 17 | 34 | 37 | **352** |
| DIN-SQL-inspired | 28 | 36 | 27 | 18 | 34 | 54 | 22 | 40 | 18 | 25 | 37 | **339** |

GPT-5.6-Luna:

| system | california | card_games | codebase | debit_card | eu_football | financial | formula_1 | student_club | superhero | thrombosis | toxicology | total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SAG | 53 | 58 | 34 | 42 | 55 | 52 | 29 | 44 | 43 | 44 | 60 | **514** |
| Direct + conventions | 37 | 53 | 33 | 29 | 45 | 63 | 27 | 52 | 18 | 39 | 49 | **445** |
| Direct | 31 | 51 | 27 | 24 | 41 | 59 | 28 | 49 | 18 | 37 | 56 | **421** |
| ReAct, informed | 34 | 37 | 24 | 23 | 49 | 43 | 29 | 45 | 22 | 42 | 42 | **390** |
| SQL Pivot, real DDL | 18 | 37 | 14 | 12 | 31 | 42 | 23 | 39 | 11 | 8 | 34 | **269** |

## Component ablation (DeepSeek-V4-Flash)

Every row is compared with a reference run of full SAG that scores **479 (39.6%)**. That is a separate run of the same configuration as the 487 main result; pair the ablation rows with 39.6%, not with 40.2%.

Configurations are named as in Table VI of the paper, with the ablation arm in parentheses.

| configuration | correct | EXC | full SAG vs configuration |
|---|---:|---:|---|
| **Full SAG** (reference run, `sag_full`) | **479** | **39.6%** | — |
| One decode (`sag_core_generate_only`): one candidate, no gate, no repair, no vote | 457 | 37.8% | 93 W / 71 L, p = 0.10 |
| One candidate (`sag_v2`): gate and repair, no result-space vote | 456 | 37.7% | 83 W / 60 L, p = 0.065 |
| No value grounding (`sag_core_no_value_witness_strict`, earlier called "no value witnesses"): no value index, value witnesses, or value check, no empty-result prefix counting (`bisect_empty` in the code), and no stored-value examples on the card | 446 | 36.9% | 102 W / 69 L, p = 0.014 |
| No grounding (`sag_core_no_grounding`): the first three raw documents per collection instead of the induced card | 372 | 30.7% | 178 W / 71 L, p = 8.9e-12 |

Full SAG scores above every reduced configuration. The drops without grounding and without value grounding are significant, and the one-decode and one-candidate rows are within noise. The paper reports no p-values and presents the value-grounding, one-candidate, and one-decode drops (1.8 to 2.7 points) as trends, because they are close to the variation between repeated runs (see Stability).

Two representation variants isolate the path card itself. They were run alongside the 479 reference in the same runs, so they too compare with 39.6%:

| configuration | correct | EXC | full SAG vs configuration |
|---|---:|---:|---|
| Top-level fields only (`sag_v3_top_card`) | 142 | 11.7% | 365 W / 28 L, p = 5.8e-76 |
| No dynamic-key collapse (`sag_v3_no_collapse`) | 430 | 35.5% | 118 W / 69 L, p = 4.2e-4 |

Per database:

| configuration | california | card_games | codebase | debit_card | eu_football | financial | formula_1 | student_club | superhero | thrombosis | toxicology | total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Full SAG (reference) | 44 | 55 | 33 | 29 | 51 | 53 | 23 | 46 | 45 | 45 | 55 | **479** |
| One decode | 40 | 53 | 33 | 29 | 47 | 53 | 20 | 44 | 43 | 42 | 53 | **457** |
| One candidate | 40 | 57 | 26 | 31 | 49 | 48 | 21 | 44 | 46 | 41 | 53 | **456** |
| No value grounding | 44 | 53 | 35 | 27 | 53 | 50 | 20 | 42 | 45 | 45 | 32 | **446** |
| No grounding | 37 | 39 | 27 | 18 | 48 | 38 | 16 | 50 | 18 | 30 | 51 | **372** |
| No dynamic-key collapse | 28 | 48 | 30 | 40 | 49 | 50 | 15 | 58 | 45 | 43 | 24 | **430** |
| Top-level fields only | 25 | 13 | 7 | 11 | 24 | 6 | 8 | 10 | 14 | 21 | 3 | **142** |

## Ablation: narrower dynamic-key detector (GPT-5.6-Luna)

The dynamic-key comparison of the ablation study (Section IV-F of the paper): SAG with its full detector of maps whose keys are data values against the same configuration with the narrower detector used before the final revision (`TEND_SAG_KEYS_V2=0`), paired per database on all 1,210 questions. The full detector answers 512 and the narrower one 462, 89 W / 39 L, p = 1.2e-5.

- Eight databases (August): the full-detector side is the main GPT-5.6-Luna SAG result, and the narrower detector was run at the same time. 398 versus 360 correct on these 880 questions, 60 W / 22 L, p = 3.2e-5.
- formula_1, student_club, superhero (added 2026-09-26): both detectors run at the same time with the protocol of the August runs. 114 versus 102 correct, 29 W / 17 L, p = 0.10. Because the full-detector side of these three databases is a new run, the pooled total of this side (512) differs from the SAG total of the main results (514).

On the eight August databases, on the 203 questions whose reference query enumerates a dynamic-key map (`$objectToArray`) and whose narrower-detector answer does not, the result is 28 W / 3 L. Eight of those 28 wins are against narrower-detector answers that failed, seven because every model call failed (provider timeouts) and one with an execution error. Without them it is 20 W / 3 L (p = 4.9e-4). The pre-registered definition of this subset compares against the earlier frozen SAG instead of the narrower-detector run; under it the subset has 186 questions and the result is 19 W / 4 L (p = 0.0026). That earlier run is not part of `results/`.

## Stability (DeepSeek-V4-Flash, three runs of SAG)

| database | run 1 (main result) | run 2 | run 3 |
|---|---:|---:|---:|
| superhero | 48 | 46 | 47 |
| card_games | 59 | 58 | 59 |
| financial | 56 | 53 | 55 |

Between any two of these three runs, 5.5-10.9% of a database's 110 questions change between correct and wrong. The paper pools the three databases instead. On their 330 questions the three runs answer 163, 157, and 161 correctly and the reference run of the component ablation answers 153, and between any two of these four runs 6.7-9.1% of the questions change. On all 1,210 questions, the two full runs (487 and 479) differ on 9.8%. This variation is why every comparison on this page is paired by question. The per-question answers of runs 2 and 3 are not published; run 1 and the reference run are in `results/`.

## By structural label

Each question carries one structural label, assigned when the task was designed, and a resistance to SQL transfer computed from the operators of its reference pipeline. Both are in [`results/task_labels.csv`](results/task_labels.csv). EXC in percent. The DeepSeek-V4-Flash table is Table V of the paper, which omits the 12 tasks with weak resistance.

| system | dynamic-key map | nested event stream | polymorphic shape | missing versus present | attribute bag | medium resistance | strong resistance | weak resistance |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| tasks | 1,013 | 130 | 36 | 18 | 13 | 111 | 1,087 | 12 |
| **DeepSeek-V4-Flash** | | | | | | | | |
| SAG | 40.8 | 43.1 | 11.1 | 38.9 | 53.8 | 33.3 | 41.1 | 25.0 |
| ReAct, informed | 28.5 | 45.4 | 41.7 | 44.4 | 15.4 | 42.3 | 29.6 | 33.3 |
| Direct (data-rich prompt) | 29.5 | 28.5 | 25.0 | 38.9 | 0.0 | 23.4 | 29.6 | 33.3 |
| Direct with sampled documents | 29.8 | 19.2 | 25.0 | 55.6 | 0.0 | 21.6 | 29.2 | 41.7 |
| DIN-SQL-inspired MQL adaptation | 28.6 | 23.8 | 25.0 | 44.4 | 7.7 | 22.5 | 28.5 | 33.3 |
| SQL Pivot without the relational DDL | 26.0 | 22.3 | 33.3 | 33.3 | 0.0 | 21.6 | 25.7 | 58.3 |
| **GPT-5.6-Luna** | | | | | | | | |
| SAG | 44.2 | 33.8 | 16.7 | 50.0 | 53.8 | 35.1 | 43.4 | 25.0 |
| Direct with SAG's six output conventions | 38.1 | 29.2 | 30.6 | 55.6 | 0.0 | 28.8 | 37.6 | 33.3 |
| Direct (data-rich prompt) | 35.0 | 33.8 | 30.6 | 61.1 | 0.0 | 36.0 | 34.6 | 41.7 |
| ReAct, informed | 31.1 | 43.8 | 27.8 | 44.4 | 0.0 | 37.8 | 31.7 | 25.0 |
| SQL Pivot given the real relational DDL | 22.4 | 17.7 | 27.8 | 50.0 | 0.0 | 22.5 | 22.0 | 41.7 |

## Further analyses in the paper

The analyses the paper reports beyond the tables above, each recomputed from the per-question answers, the released `data/TEND.json`, and the release MongoDB databases. `scripts/check_results.py` recounts all of them except the step and call counts, which come from the run records, and the facts about stored documents and card sizes, which need the release MongoDB. The benchmark statistics and the analyses of reference pipelines need the restored release (`--dataset-dir`).

**Benchmark (Section II, Table I).**
- Operator usage of the released reference pipelines, with the operator sets of the release statistics script: dynamic-key operators in 1,094 tasks (90.4%), array operators in 1,172 (96.9%), and nested dotted paths in 1,174 (97.0%). `$objectToArray` appears in 1,093 and `$unwind` in 873. Pipelines have a median of 7 and at most 14 top-level stages and form 1,115 skeleton families, the largest with six members.
- Resistance to SQL transfer is computed with `classify_anti_sql_transfer` (`src/tend/construction/verify.py`) from the operators of each released reference pipeline: 1,087 strong, 111 medium, and 12 weak.

**Main comparison (Section IV-B, DeepSeek-V4-Flash).**
- Wrong rows (`value_mismatch`, `row_subset`, `row_superset`, `order_only`, `row_count_exceeded`) account for 57.0-60.2% of the tasks for every system that sees stored data.
- Mechanical failures (`no_submission`, `exec_error`, `empty`): SAG 2.6%, Direct (data-rich prompt) 10.7%, and ReAct 9.5%.
- Of SAG's 691 wrong-row answers, 594 (86%) have EXF1 = 0, so no predicted row matches a reference row, and 90 overlap the reference in part. For Direct, 88% of the wrong-row answers have EXF1 = 0.
- The eight systems of the main DeepSeek-V4-Flash table answer 657 questions together (54.3%), 85 of them answered only by SAG. None of the 13 system runs of both main tables answers 466 questions (38.5%).
- ReAct uses a median of 21 steps on the tasks it submits, and 78 of its 109 failures exhausted the 50-step budget. SAG makes about 3.3 model calls per question, since its three candidates average 1.1 rounds each (from the run records of the 487 result, which cover 1,098 questions).

**Second model (Section IV-C).** Direct with SAG's six output conventions answers 80 questions that plain Direct misses and misses 56 that plain Direct answers. All 109 failures of ReAct with GPT-5.6-Luna are responses that could not be parsed.

**Where the difficulty comes from (Section IV-E).**
- On the 283 tasks whose reference pipeline has at least two `$unwind` stages, SAG answers 42.0% and no baseline more than 25.8%. SAG leads ReAct, the strongest baseline, by 12.9 points on the 831 tasks with seven or more stages and by 1.8 points on the 379 shorter ones.
- All 36 polymorphic tasks query `formula_1.f1_actor_profiles`, which stores 840 drivers, 208 constructors, and 72 circuits in that order. Its first 400 documents, the sample SAG induces its card from, are all drivers. Of the 36 reference pipelines, 34 compare against the literal `"constructor"` or `"circuit"`.
- Six of the 130 nested-event tasks read the pit-stop arrays of `formula_1.race_weekends_v2`, which are empty in the first 824 documents of the collection. ReAct answers all six and SAG none. On the other 124 tasks, SAG answers 56 and ReAct 53.
- On the 111 tasks of medium resistance to SQL transfer, ReAct answers 42.3% and SAG 33.3%. On the 18 missing-versus-present tasks, Direct with sampled documents, ReAct, and the DIN-SQL-inspired adaptation all score above SAG.
- With GPT-5.6-Luna, SAG answers 44.2% of the dynamic-key tasks (best baseline 38.1%), 53.8% of the attribute-bag tasks (0.0%), 16.7% of the polymorphic tasks (30.6%), and 33.8% of the nested-event tasks (ReAct 43.8%).

**Ablation (Sections III-C and IV-F).**
- The top-level card returns no rows on 24.8% of the questions, against 0.2% for the reference run.
- One decode fails mechanically on 6.9% of the questions and returns wrong rows on 55.3%. One candidate, which adds the gate and repair, fails mechanically on 1.5% and returns wrong rows on 60.8%.
- Rendered without dynamic-key collapse from the first 400 documents of each collection, the card of 9 of the 11 databases exceeds the 400-entry cap, all except student_club and superhero. The card of `thrombosis_prediction.measurement_code_bags` has 17 entries with collapse and 187 without.

**Relational pivot (Section IV-H).** Direct with sampled documents answers 108 questions that SQL Pivot misses, and SQL Pivot 72 that Direct misses. SQL Pivot returns no rows on 8.8% of the questions, against 7.2%. With GPT-5.6-Luna, Direct answers 188 questions that SQL Pivot with the real DDL misses, and the pivot 36 that Direct misses.

## The submitted version, for reference

The originally submitted results came from an earlier SAG (June 2026) on DeepSeek-V4-Flash, called through a different provider route: SAG 433 (35.8%), Direct with a data-rich prompt 350, informed ReAct 373, Direct with sampled documents 346, SQL Pivot without the relational DDL 310, Direct with the schema only 3, Direct with the question only 2. Its cumulative ablation ladder scored `sag_card1` 379, `sag_gate` 421, `sag_v2` 406, `sag_full` 433. The tables above supersede these numbers for SAG and for every system that was re-run. The five systems that were not re-run (informed ReAct, Direct with sampled documents, SQL Pivot without the DDL, Direct with the schema only, and Direct with the question only) keep their June results, which appear in the main DeepSeek-V4-Flash table above and in the paper and are part of `results/`. The per-question answers of the superseded June SAG, Direct, and ablation ladder are not.

## Superseded numbers — do not cite

- **An August 15 seven-configuration ablation panel** (for example 42.1% without the gate, 40.6% without value witnesses, 39.9% with a plain empty-result message). Its value-witness arm still leaked stored values into the prompt, and its gate-free arm also changed prompt text, so neither isolates a component. Replaced by the component ablation above.
- **Earlier versions of the component ablation**, including 40.6% for a leaking value-witness arm and 40.3% for a gate-and-repair arm that is not part of the final table.
- **GPT-5.6-Luna SAG 450**, the solver before the revision. Its Direct and ReAct runs are the frozen controls used above.
- **GPT-5.6-Luna with only an `_id` change, 455**, rejected by its own pre-registered criterion.
- **DIN-SQL-inspired 341**, a first integration whose runner discarded the linking and classification output.
- Runs disabled by provider timeouts, pilots, and probes.

## How the numbers were produced

1. **Two DeepSeek SAG totals.** 487 (40.2%) is the main result. 479 (39.6%) is another run of the same configuration, used as the reference of the component ablation.
2. **Replayed answers.** Two databases were answered from saved transcripts after their runs were interrupted. SAG's final vote was replayed offline without the gate tie-break. In the DeepSeek SAG result this is california_schools: 92 answers replayed, 18 questions without an answer, scored 0. In the GPT-5.6-Luna SAG result it is student_club: all 110 replayed.
3. **Per-question reruns.** Questions lost to provider timeouts were re-run one at a time and merged, without looking at their scores: DeepSeek SAG on card_games and codebase_community, the ablation reference on formula_1 and superhero, and DIN-SQL on three databases. None of the comparisons is a single uninterrupted run, and the systems are not compute-matched.
4. **Frozen controls.** GPT-5.6-Luna Direct and ReAct are reused from an earlier campaign on the same questions and evaluator, and the DeepSeek-V4-Flash rows marked † are the June runs of the submitted version. Not every system was re-run.
5. **Stored scores are authoritative.** Re-evaluating the same answers against a reloaded MongoDB can move one or two answers whose correctness depends on the order of tied rows.
6. **Evaluator version.** The final evaluator stops reading a prediction after one row more than the reference and labels it `row_count_exceeded`. EXC is unchanged by this; EXF1 and the outcome-bucket fractions are not comparable with reports from older evaluator versions.

## Evidence

The per-question answers behind the results on this page are in [`results/`](results/): three folders, `SAG/`, `Baselines/`, and `Ablation_study/`, each with a `DeepSeek/` and a `Luna/` subfolder and one file per configuration of the paper, holding one row per question with the system's MQL or its typed failure and the stored EXC, EXF1 and outcome. `scripts/check_results.py` recounts the numbers on this page from them. The exceptions are named in its docstring: runs 2 and 3 of the stability check, the pre-registered subset of the dynamic-key detector comparison, the ReAct step and SAG call counts, and the facts about stored documents and card sizes, as well as the submitted version and the superseded numbers listed above, whose answers are not included. The question and the reference MQL are not repeated there and join `data/TEND.json` of the release on `(db_id, record_id)`.

The files in `results/` were derived from a sealed local bundle of the raw run outputs, `tend_final_results_v1` (13 MB), and from its addendum for the three databases added to the detector comparison. The bundle also holds the question and reference MQL of every row, the SHA-256 of every source file, the code snapshot recorded by the final ablation campaign, and a script that verifies the bundle and recounts every total. Its manifest is pinned by:

```text
3c764bcc317971bb9ec999d8015cb8f2c5a7e7e436820ff85dbeadd2d69b62d7  tend_final_results_v1/MANIFEST.json
```
