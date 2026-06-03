# TEND Glossary

> Single source of truth for cross-volume terminology. Each term lists its owning volume and primary anchor.

## Core Task Terms

## AST_check
**Definition**: Static boolean check that a candidate MQL string satisfies all four subsets of `canonical_form_set`. **RAR**: since cfs is thin (output-guard + invariant), AST_check primarily enforces disabled-operator + shape-policy legality, not a native-idiom fingerprint.  
**Owner**: [01 §01-3](../01_task_definition.md#01-3)  
**See also**: EX, canonical_form_set, witness discrimination

<a id="canonical_form_set"></a>
## canonical_form_set
**Definition**: Four-tuple AST predicate for gold-as-class membership. **RAR thin**: collapsed to idiom-invariant operators + output-space guard — `must_not_contain` ⊇ 6 disabled ops; `must_contain`/`must_contain_at_root` hold only unavoidable structural ops (e.g. `$lookup`/`$setWindowFields`) + shape guards, and MAY be empty; never locks replaceable idioms (`$addFields`↔`$project`, `$cond`↔`$switch`↔`$ifNull`). Structural discrimination ("did it handle the heterogeneity?") is carried by the witness (L2/P3), not cfs.  
**Owner**: [01 §01-3-1](../01_task_definition.md#01-3-1), derived thin in [04 §04-3-2](../04_agent_framework.md#04-3-2) by MS  
**See also**: gold-as-class, AST_check, witness discrimination, reference oracle (R)

## canonical representative (MQL)
**Definition**: The named representative query stored in record field `MQL`; one member of `gold-class(r)`.  
**Owner**: [01 §01-3](../01_task_definition.md#01-3)

## db_id
**Definition**: Unique database identifier indexing schema `S` and frozen snapshot `D(db_id)`. Sourced from BIRD mini-dev DB ids.  
**Owner**: [01 §01-1](../01_task_definition.md#01-1), [02 §02-2](../02_dataset_design.md#02-2)

## EX (Execution Match)
**Definition**: Primary correctness metric: `AST_check = pass` AND `NormExec(q_p, D) ≡_rec NormExec(q_g, D)`.  
**Owner**: [01 §01-2](../01_task_definition.md#01-2), [05 §05-2](../05_evaluation_methodology.md#05-2)

## functional_sql_solvable
**Definition**: Diagnostic flag: SQL-bridge MQL achieves NormExec ≡_rec with gold on witness D (functional correctness only, ignores AST shape).  
**Owner**: [05 §05-2](../05_evaluation_methodology.md#05-2), assigned by NNC

## structural_sql_solvable
**Definition**: Diagnostic flag: SQL-bridge MQL achieves both NormExec ≡_rec and AST_check pass (functional + structural match).  
**Owner**: [05 §05-2](../05_evaluation_methodology.md#05-2), assigned by NNC

## gold-as-class
**Definition**: Correctness anchor treating gold as an equivalence class, not a single MQL literal.  
**Owner**: [01 §01-3](../01_task_definition.md#01-3)

## Norm
**Definition**: Four-layer result normalization (scalar / composite / null-vs-missing / _id + shape-preserving).  
**Owner**: [01 §01-4](../01_task_definition.md#01-4)

## NormExec
**Definition**: Composite operator `Norm(Exec(Parse(q), D))`; all execution equality uses NormExec, never raw Exec.  
**Owner**: [01 §01-1](../01_task_definition.md#01-1)

## ≡_rec
**Definition**: Recursive structural equality on normalized BSON results; the execution-layer equality relation.  
**Owner**: [01 §01-5](../01_task_definition.md#01-5)

## P1–P4
**Definition**: Instance-level root principles — P1 execution correctness, P2 semantic uniqueness, P3 discriminability, P4 world non-triviality.  
**Owner**: [01 §01-6](../01_task_definition.md#01-6)

<a id="reference-oracle"></a>
## reference oracle (R)
**Definition**: RAR — per-archetype naive, auditable reference implementation (independent Python, cross-paradigm) that **defines the answer** to an intent on D. MS's gold-lock criterion is `NormExec(gold,D) ≡_rec R(D)` ∧ dual-path convergence, replacing pure-reverse "gold self-certifies" and catching systematic gold bugs. R ≠ gold (R defines the answer; gold is the native idiom). Only intents with a simple auditable R enter the archetype catalog.  
**Owner**: [04 §04-2-4](../04_agent_framework.md#04-2-4); P1 in [01 §01-6](../01_task_definition.md#01-6)  
**See also**: archetype catalog, intent, P1–P4

<a id="archetype-catalog"></a>
## archetype catalog
**Definition**: RAR keystone — a closed catalog indexed by DAR mechanism; each entry carries {question shape, reference-oracle R template, resulting difficulty}. `intent / gold criterion / NLQ / coverage` all flow from it; "enumerate questions from heterogeneity" = deterministic `mechanism-instance × archetype × domain-noun` cross-product (LLM only does surface phrasing). Guarantees coverage countability + query-bearing by construction.  
**Owner**: [04 §04-2-4](../04_agent_framework.md#04-2-4)  
**See also**: intent, reference oracle (R), question_archetype

<a id="intent"></a>
## intent
**Definition**: RAR — the upstream construction atom replacing operator-centric `query_plan`. Fields: seed_mechanism, seed_signal, archetype, domain_framing, analytical_op, reference_oracle, semantic_properties; **no** primary_pattern/operator_graph as input. The seed is an information need (cause); operators/difficulty/cfs are derived (effect).  
**Owner**: [04 §04-2-2](../04_agent_framework.md#04-2-2)  
**See also**: archetype catalog, reference oracle (R), QPS

## Schema (S)
**Definition**: Complete schema description for `db_id`: collections, field tree, types, and design rationale.  
**Owner**: [01 §01-1](../01_task_definition.md#01-1), [03 §03-3](../03_dataworld_construction.md#03-3)

## world_signature
**Definition**: SHA-256 of canonical BSON serialization of `D(db_id)`; pins evaluation reproducibility.  
**Owner**: [02 §02-2](../02_dataset_design.md#02-2)

## sql_infeasibility_class
**Definition**: Record metadata enum: `feasible`, `semantic`, `performative`, `structural_pipeline`, `structural_schema_flex`; drives NNC graduated SQL-shortcut gate.  
**Owner**: [02 §02-2](../02_dataset_design.md#02-2), [04 §04-5](../04_agent_framework.md#04-5)

## DAR Construction Terms — Phase A

<a id="dar"></a>
## Document-Aggregate Recovery (DAR)
**Definition**: Phase A 构造法。以真实工作负载 + FK 外键图 + 列语义为信号,对源关系库执行反范式化,**恢复**其内在的文档聚合(aggregate),并将异构(heterogeneity)作为该恢复过程的**涌现属性**而非外加注入。取代旧「Spider 锚定 + H1–H4 主动注入」范式。  
**Owner**: 03  
**See also**: aggregate (recovery), query-bearing heterogeneity, BIRD mini-dev

<a id="query-bearing-heterogeneity"></a>
## query-bearing heterogeneity
**Definition**: 满足如下判据的异构机制 —— 其存在改变至少一条 record 的 echo-gold 执行结果。即:移除/规整该异构会令某条 record 的 gold 结果发生变化。**非 query-bearing** 的异构(不影响任何 record 结果)即视为装饰,应删除。  
**Owner**: 03  
**See also**: Gate-QB, DAR

<a id="gate-qb"></a>
## Gate-QB / Gate-SD
**Definition**: DAR 两道门禁。**Gate-QB**(query-bearing 门禁):异构必须 query-bearing,否则删除。**Gate-SD**(schema≡data 门禁):声明的 schema 必须与实际数据形状逐字段一致,schema 与 data 不得漂移。  
**Owner**: 03  
**See also**: query-bearing heterogeneity, Gate-SD

<a id="aggregate"></a>
## aggregate (recovery)
**Definition**: 文档建模的设计单元(对应一个 MongoDB collection 的文档聚合边界),由 BIRD 源库的 join 图聚类得出 —— 强连通/高频联结的关系被聚为一个文档聚合。DAR 「恢复」的即是这些聚合。  
**Owner**: 03  
**See also**: DAR, BIRD mini-dev

<a id="bird-mini-dev"></a>
## BIRD mini-dev
**Definition**: DAR 构造数据源,含 11 个真实业务库,自带 (question, evidence, SQL, difficulty) 工作负载。取代 Spider 1.0 作为 Phase A 构造源;基准为 test-only(无 train/test 切分)。  
**Owner**: 03  
**See also**: DAR, db_id, aggregate (recovery)

## Agent Framework Terms — Phase A (DataWorld)

## WP (Workload Profiler)
**Definition**: Agent extracting access patterns from BIRD `(question, evidence, SQL)` workload (real join graph + co-access frequency) to drive MongoDB aggregate design; outputs `scenario_summary` for Phase B NL paraphrase.  
**Owner**: [03 §03-4](../03_dataworld_construction.md#03-4)

## scenario_summary
**Definition**: WP output field: domain semantics and typical business question patterns (no SQL); consumed by NLP for reverse paraphrase only.  
**Owner**: [03 §03-4](../03_dataworld_construction.md#03-4)

## SRA (Schema Re-architect)
**Definition**: Agent recovering document aggregates from the relational source into a workload-driven MongoDB-native layout via Stage A (11 pattern menu) + Stage B (DAR five-mechanism heterogeneity recovery).  
**Owner**: [03 §03-3](../03_dataworld_construction.md#03-3), [03 §03-6](../03_dataworld_construction.md#03-6)

## SC (Schema Critic)
**Definition**: Agent adversarially reviewing SRA output for anti-patterns, workload coverage, and query-bearing (Gate-QB) supply pre-audit.  
**Owner**: [03 §03-4](../03_dataworld_construction.md#03-4)

## DM (Data Migrator)
**Definition**: Agent de-normalizing BIRD relational rows into the SRA-designed MongoDB aggregates (DAR materialization; deterministic stratified sampling preserving rare subtypes).  
**Owner**: [03 §03-4](../03_dataworld_construction.md#03-4)

## flex_eligible
**Definition**: Boolean on `bird_db_catalog` entries: DB recovers ≥1 **query-bearing** DAR heterogeneity mechanism in Phase A (DAR 后亦称 `query_bearing`).  
**Owner**: [02 §02-II-2](../02_dataset_design.md#02-ii-2), [03 §03-4](../03_dataworld_construction.md#03-4)

## min_flex_db_ratio
**Definition**: SC pre-audit 阈值(DAR 后为 `min_query_bearing_ratio`);test-only 下 11 库全部装载、无入选门槛,该比例仅作 query-bearing 供给观测,不再驱动配额放宽。  
**Owner**: [03 §03-4](../03_dataworld_construction.md#03-4), [02 §02-4](../02_dataset_design.md#02-4)

## Agent Framework Terms — Phase B (Query Construction)

## QPS (Query Plan Sampler → Intent Enumerator)
**Definition**: RAR — enumerates `intent` from Phase A Gate-QB heterogeneity × archetype catalog × scenario_summary; **no longer samples** operator `primary_pattern`/`operator_graph`. Coverage quota is on cause-axes (seed_mechanism × archetype × domain); difficulty is derived.  
**Owner**: [04 §04-2](../04_agent_framework.md#04-2)

## MS (MQL Synthesizer)
**Definition**: Agent synthesizing `mql_primary` + `mql_alt` via ≥2 independent paths. **RAR gold-lock**: `NormExec(gold,D) ≡_rec R(D)` (independent reference oracle) ∧ dual-path ≡_rec; mechanically derives **thin** `canonical_form_set`.  
**Owner**: [04 §04-3](../04_agent_framework.md#04-3)

## MUT (Mutation Generator)
**Definition**: Agent producing 5–8 plausible wrong MQL variants per record for P3 discriminativeness validation.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## PV (Property Verifier)
**Definition**: Agent verifying semantic properties, mutations EX-fail, and AST_check on witness D.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## NLP (NL Paraphraser)
**Definition**: Agent paraphrasing canonical + colloquial NLQ. **RAR**: paraphrases from the **intent** (information need), NOT the locked MQL pipeline — keeps NLQ a natural business question instead of a stage transcription.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## RTV (Round-Trip Verifier)
**Definition**: Independent NL→MQL agent verifying intent recoverability. **RAR result-level**: canonical must `NormExec ≡_rec gold` (not the cfs fingerprint) — decoupling difficulty from intent-clarity lets a ~gpt-4o-mini RTV close L4 records without the NLQ leaking the pipeline.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## NNC (NoSQL Nativeness Critic)
**Definition**: Agent assigning L0–L4 difficulty, `sql_infeasibility_class`, graduated SQL-shortcut gate, and ambiguity attack.  
**Owner**: [04 §04-5](../04_agent_framework.md#04-5)

## RA (Realism Auditor)
**Definition**: Agent validating record realism and P4 non-triviality; targeted augment when needed.  
**Owner**: [04 §04-7](../04_agent_framework.md#04-7)

## property_verification
**Definition**: PV audit artifact: boolean property table (cardinality, null/missing, shape, tie handling, edge coverage).  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## round_trip
**Definition**: RTV audit artifact: independent NL→MQL round-trip results and pass/fail verdict.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## Coverage & Split Terms

## cross-domain holdout (已废止)
**Definition**: 原指按 Spider 域级别不相交的 train/test 切分。**DAR 迁移后废止**:基准为 **test-only**,直接采用 BIRD mini-dev 的 11 个真实业务库,无 train/test 切分、无 holdout 概念;不存在跨域泄漏问题。  
**Owner**: [02 §02-3](../02_dataset_design.md#02-3)

## L0–L4
**Definition**: NoSQL nativeness difficulty tiers; L4 = highly NoSQL-native; test hard constraints L4 ≥ 30%, L0 ≤ 5%, schema_flex ≠ none ≥ 25% (with supply-relax).  
**Owner**: [04 §04-5](../04_agent_framework.md#04-5), [02 §02-4](../02_dataset_design.md#02-4)

## coverage axes (RAR cause-axes)
**Definition**: RAR — min+max dual quota on **cause-axes**: `seed_mechanism` (= schema_flex) × `question_archetype` × `domain`. `difficulty_tier` / `join_depth` / `aggregation_depth` / `schema_pattern` are demoted to **derived observations** (monitored, not targeted). Supersedes the old operator/shape six-axis.  
**Owner**: [02 §02-4](../02_dataset_design.md#02-4)

## schema_flex
**Definition**: Record/db metadata for SRA Stage B heterogenization type: `none`, `polymorphic`, `attribute_bag`, `schema_versioning`, or `dynamic_key`.  
**Owner**: [02 §02-2](../02_dataset_design.md#02-2), [03 §03-6](../03_dataworld_construction.md#03-6)

## heterogeneous_ratio
**Definition**: 异构 record 占比。**DAR 迁移后已降级为 audit 描述统计**,仅作事后观测,**不再是构造目标 / 配额约束**;异构由 DAR 作为涌现属性恢复并经 Gate-QB 过滤,其比例不再被主动调控。  
**Owner**: 03  
**See also**: query-bearing heterogeneity, Gate-QB

<a id="__variants"></a>
## __variants
**Definition**: Optional collection-level array in `mongodb_schema/<db_id>.json` declaring document shape variants with `discriminator`, `fields`, `coverage`, and `source_signal`. **RAR hybrid disclosure**: declares the discriminator + variant field sets + coverage (presence rate), but does **not** promise a deterministic discriminator→fields function or per-doc shape — the solver handles per-doc reality at runtime (the schema-less "sweet spot" that makes [06](../06_solution_design.md)'s probe-based solver meaningful). Gate-SD holds at the set level.  
**Owner**: [03 §03-6](../03_dataworld_construction.md#03-6)  
**See also**: Gate-QB / Gate-SD, hybrid disclosure

## heterogenization
**Definition**: Optional block in `agent_design_rationale/<db_id>.yaml` recording which DAR mechanisms (①–⑤) were recovered and the per-mechanism BIRD signal evidence.  
**Owner**: [03 §03-6](../03_dataworld_construction.md#03-6)

## DAR 五机制 (heterogeneity recovery · 取代 H1–H4)
**Definition**: Deterministic Stage B mechanisms recovered from real BIRD signals — ① polymorphic_subtype(低基数判别列 + value_description 枚举)、② sparse(列 NULL 率)、③ type/structure(构造期合成)、④ nesting(FK + join 频率)、⑤ schema_version(时间列)。多机制可在一个聚合上叠加;每个机制实例均须经 **Gate-QB**(query-bearing)。**取代旧 H0 强制合成 + H1–H4 优先级仲裁**。  
**Owner**: [03 §03-6](../03_dataworld_construction.md#03-6)

## structural_schema_flex
**Definition**: `sql_infeasibility_class` for queries requiring schema-shape operators (`$switch`, `$objectToArray`, cross-variant `$type`) that SQL cannot express.  
**Owner**: [04 §04-5](../04_agent_framework.md#04-5)

## structural_pipeline
**Definition**: `sql_infeasibility_class` for pipeline-structure lossy queries (e.g. `$facet + $setWindowFields`).  
**Owner**: [04 §04-5](../04_agent_framework.md#04-5)

## polymorphic_dispatch
**Definition**: QPS primary_pattern requiring `$switch` or `$type` dispatch across `__variants` document shapes.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## dynamic_key_aggregation
**Definition**: QPS primary_pattern requiring `$objectToArray` / `$arrayToObject` on dynamic-key documents (H4).  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## attribute_bag_unfold
**Definition**: QPS primary_pattern requiring `$arrayToObject` or `$reduce` on sparse attribute bags (H2).  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## schema_version_fallback
**Definition**: QPS primary_pattern requiring multi-layer `$ifNull` chains across schema version fields (H3).  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)
