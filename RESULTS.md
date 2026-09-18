# TEND final results

This page records the final experimental results of the TEND project (the August 2026
experiment campaign), which numbers they supersede, and how each number was produced.

## Setup

- **Benchmark.** The native MongoDB release `tend-native-mongodb-v1`: 1,210 natural-language
  questions over 11 MongoDB databases, 110 per database. Every system is scored on all 1,210
  questions. A missing, rejected, or failing answer counts as wrong and stays in the
  denominator.
- **Metric.** `EXC`, execution accuracy that ignores column names and tolerates at most two
  surplus columns per row (β = 2). Comparisons between two systems are exact two-sided
  McNemar tests on the same questions; "W / L" counts the questions only the first system
  answers correctly and the questions only the second one does.
- **Models.** DeepSeek-V4-Flash (`deepseek/deepseek-v4-flash`, the backbone of the submitted
  paper) and GPT-5.6-Luna (`openai/gpt-5.6-luna`), both called through OpenRouter at
  temperature 0.
- **Code.** Commit `260801ea` of this repository ("Publish the final code used for the August
  2026 experiments") is the code the experiments ran on. It matches the code snapshot recorded
  by the final ablation campaign byte for byte, except for the line endings of one file; the
  earlier August runs recorded no source hash. The release tag `v1.0.0` keeps only the code
  the final experiments, the metric checks, dataset construction and the demo use, and adds
  packaging, the runtime files under `proposals/`, documentation and the command-line help
  text. In offline stub runs it gives the same prompts, answers and disclosure fields as
  `260801ea` for every system and ablation arm on this page, and a recording provider receives
  the same requests from both. Its ReAct step budget defaults to 50, the value the final runs
  set. The June arms of the submitted version below were removed after `260801ea`; run them
  from that commit. SAG runs in its revised default configuration: dynamic-key (field-group)
  recognition, the identifier card, and value-witness handling.

## Main results

### DeepSeek-V4-Flash

| system | correct | EXC | SAG vs system |
|---|---:|---:|---|
| **SAG** | **487** | **40.2%** | — |
| Direct (data-rich prompt) | 352 | 29.1% | 214 W / 79 L, p = 1.5e-15 |
| DIN-SQL-inspired MQL adaptation | 339 | 28.0% | 215 W / 67 L, p = 2.9e-19 |

The DIN-SQL-inspired adaptation (schema linking, classification and decomposition,
class-conditioned generation, self-correction, six fixed examples from MongoDB's public sample
databases) is not distinguishable from Direct: 88 W / 101 L, p = 0.38.

### GPT-5.6-Luna

| system | correct | EXC | SAG vs system |
|---|---:|---:|---|
| **SAG** | **514** | **42.5%** | — |
| Direct with SAG's six output conventions | 445 | 36.8% | 152 W / 83 L, p = 8.0e-6 |
| Direct (data-rich prompt) | 421 | 34.8% | 176 W / 83 L, p = 7.6e-9 |
| ReAct, informed (real collection names and first five rows) | 390 | 32.2% | 207 W / 83 L, p = 2.2e-13 |
| SQL Pivot given the real relational DDL | 269 | 22.2% | 295 W / 50 L, p = 2.1e-43 |

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

Every row is compared with a reference run of full SAG that scores **479 (39.6%)**. That is a
separate run of the same configuration as the 487 main result; pair the ablation rows with
39.6%, not with 40.2%.

| configuration | correct | EXC | full SAG vs configuration |
|---|---:|---:|---|
| **full SAG (reference run)** | **479** | **39.6%** | — |
| one decode: one candidate, no gate, no repair, no vote | 457 | 37.8% | 93 W / 71 L, p = 0.10 |
| one candidate: gate and repair, no result-space vote | 456 | 37.7% | 83 W / 60 L, p = 0.065 |
| no value witnesses (strict: no value index, no empty-result bisection) | 446 | 36.9% | 102 W / 69 L, p = 0.014 |
| no grounding: the first three raw documents per collection instead of the induced card | 372 | 30.7% | 178 W / 71 L, p = 8.9e-12 |

Full SAG scores above every reduced configuration. The drops from removing grounding and
value witnesses are significant; the single-decode and single-candidate rows are within noise.

Two representation variants isolate the path card itself. They were run alongside the 479
reference in the same runs, so they too compare with 39.6%:

| configuration | correct | EXC |
|---|---:|---:|
| card with top-level fields only | 142 | 11.7% |
| card without dynamic-key collapse | 430 | 35.5% |

Per database:

| configuration | california | card_games | codebase | debit_card | eu_football | financial | formula_1 | student_club | superhero | thrombosis | toxicology | total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| full SAG (reference) | 44 | 55 | 33 | 29 | 51 | 53 | 23 | 46 | 45 | 45 | 55 | **479** |
| one decode | 40 | 53 | 33 | 29 | 47 | 53 | 20 | 44 | 43 | 42 | 53 | **457** |
| one candidate | 40 | 57 | 26 | 31 | 49 | 48 | 21 | 44 | 46 | 41 | 53 | **456** |
| no value witnesses | 44 | 53 | 35 | 27 | 53 | 50 | 20 | 42 | 45 | 45 | 32 | **446** |
| no grounding | 37 | 39 | 27 | 18 | 48 | 38 | 16 | 50 | 18 | 30 | 51 | **372** |
| no dynamic-key collapse | 28 | 48 | 30 | 40 | 49 | 50 | 15 | 58 | 45 | 43 | 24 | **430** |
| top-level fields only | 25 | 13 | 7 | 11 | 24 | 6 | 8 | 10 | 14 | 21 | 3 | **142** |

## Dynamic-key recognition on and off (GPT-5.6-Luna, 8 databases)

The same SAG configuration with the revised dynamic-key recognition switched on and off, on
the 880 questions of eight databases: 398 versus 360 correct, 60 W / 22 L, p = 3.2e-5.

On the 203 questions whose reference query enumerates a dynamic-key map (`$objectToArray`)
and whose flag-off answer does not, the result is 28 W / 3 L. Eight of those 28 wins are
flag-off answers lost to provider timeouts; without them it is 20 W / 3 L (p = 4.9e-4). The
pre-registered definition of this subset compares against the earlier frozen SAG instead of the
flag-off run; under it the subset has 186 questions and the result is 19 W / 4 L (p = 0.0026).

## Stability (DeepSeek-V4-Flash, three runs of SAG)

| database | run 1 (main result) | run 2 | run 3 |
|---|---:|---:|---:|
| superhero | 48 | 46 | 47 |
| card_games | 59 | 58 | 59 |
| financial | 56 | 53 | 55 |

Between runs, 5.5-10.9% of the questions change between correct and wrong, which is why every
comparison on this page is paired by question.

## By structural feature (DeepSeek-V4-Flash)

Each question carries one structural label in the release metadata.

| feature | questions | SAG | Direct |
|---|---:|---:|---:|
| dynamic keys | 1,013 | 413 (40.8%) | 299 (29.5%) |
| nested event streams | 130 | 56 (43.1%) | 37 (28.5%) |
| polymorphic collections | 36 | 4 (11.1%) | 9 (25.0%) |
| missing versus present fields | 18 | 7 (38.9%) | 7 (38.9%) |
| attribute bags | 13 | 7 (53.8%) | 0 (0.0%) |

## The submitted version, for reference

The originally submitted results came from an earlier SAG (June 2026) on DeepSeek-V4-Flash,
called through a different provider route: SAG 433 (35.8%), Direct with a data-rich prompt 350,
informed ReAct 373, Direct with sampled documents 346, SQL Pivot without the relational DDL 310,
Direct with the schema only 3, Direct with the question only 2. Its cumulative ablation ladder
scored `sag_card1` 379, `sag_gate` 421, `sag_v2` 406, `sag_full` 433. The tables above supersede
these numbers for SAG and for every system that was re-run.

## Superseded numbers — do not cite

- **An August 15 seven-configuration ablation panel** (for example 42.1% without the gate,
  40.6% without value witnesses, 39.9% with a plain empty-result message). Its value-witness
  arm still leaked stored values into the prompt, and its gate-free arm also changed prompt text,
  so neither isolates a component. Replaced by the component ablation above.
- **Earlier versions of the component ablation**, including 40.6% for a leaking value-witness
  arm and 40.3% for a gate-and-repair arm that is not part of the final table.
- **GPT-5.6-Luna SAG 450**, the solver before the revision. Its Direct and ReAct runs are the
  frozen controls used above.
- **GPT-5.6-Luna with only an `_id` change, 455**, rejected by its own pre-registered criterion.
- **DIN-SQL-inspired 341**, a first integration whose runner discarded the linking and
  classification output.
- Runs disabled by provider timeouts, pilots, and probes.

## How the numbers were produced

1. **Two DeepSeek SAG totals.** 487 (40.2%) is the main result. 479 (39.6%) is another run of
   the same configuration, used as the reference of the component ablation.
2. **Replayed answers.** Two databases were answered from saved transcripts after their runs
   were interrupted. SAG's final vote was replayed offline without the gate tie-break. In the
   DeepSeek SAG result this is california_schools: 92 answers replayed, 18 questions without an
   answer, scored 0. In the GPT-5.6-Luna SAG result it is student_club: all 110 replayed.
3. **Per-question reruns.** Questions lost to provider timeouts were re-run one at a time and
   merged, without looking at their scores: DeepSeek SAG on card_games and codebase_community,
   the ablation reference on formula_1 and superhero, and DIN-SQL on three databases. None of
   the comparisons is a single uninterrupted run, and the systems are not compute-matched.
4. **Frozen controls.** GPT-5.6-Luna Direct and ReAct are reused from an earlier campaign on the
   same questions and evaluator; not every system was re-run.
5. **Stored scores are authoritative.** Re-evaluating the same answers against a reloaded
   MongoDB can move one or two answers whose correctness depends on the order of tied rows.
6. **Evaluator version.** The final evaluator stops reading a prediction after one row more than
   the reference and labels it `row_count_exceeded`. EXC is unchanged by this; EXF1 and the
   outcome-bucket fractions are not comparable with reports from older evaluator versions.

## Evidence

The per-question raw answers behind every number on this page, including the submitted
version, are kept in a sealed bundle outside this repository, `tend_final_results_v1` (13 MB).
It holds one row per system and question (the question, the reference MQL, the system's MQL
or its typed failure, and the stored EXC, EXF1 and outcome), the SHA-256 of every source file,
the code snapshot recorded by the final ablation campaign, and a script that verifies the
bundle and recounts every total. Its manifest is pinned by:

```text
3c764bcc317971bb9ec999d8015cb8f2c5a7e7e436820ff85dbeadd2d69b62d7  tend_final_results_v1/MANIFEST.json
```
