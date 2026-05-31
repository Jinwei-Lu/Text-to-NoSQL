# TEND Glossary

> Single source of truth for cross-volume terminology. Each term lists its owning volume and primary anchor.

## Core Task Terms

## AST_check
**Definition**: Static boolean check that a candidate MQL string satisfies all four subsets of `canonical_form_set` (must_contain, must_not_contain, must_contain_at_root, must_not_contain_at_root).  
**Owner**: [01 §01-3](../01_task_definition.md#01-3)  
**See also**: EX, canonical_form_set

<a id="canonical_form_set"></a>
## canonical_form_set
**Definition**: Four-tuple equivalence-class specification defining gold-as-class membership for a record.  
**Owner**: [01 §01-3](../01_task_definition.md#01-3), derived in [04 §04-4](../04_agent_framework.md#04-4) by MS  
**See also**: gold-as-class, AST_check

## canonical representative (MQL)
**Definition**: The named representative query stored in record field `MQL`; one member of `gold-class(r)`.  
**Owner**: [01 §01-3](../01_task_definition.md#01-3)

## db_id
**Definition**: Unique database identifier indexing schema `S` and frozen snapshot `D(db_id)`. Sourced from Spider 1.0 DB ids.  
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

## QIM (Query Intent Match)
**Definition**: Structural half of EX; AST-level alignment without requiring execution match.  
**Owner**: [05 §05-2](../05_evaluation_methodology.md#05-2)

## Schema (S)
**Definition**: Complete schema description for `db_id`: collections, field tree, types, and design rationale.  
**Owner**: [01 §01-1](../01_task_definition.md#01-1), [03 §03-3](../03_spider_anchored_dataworld.md#03-3)

## world_signature
**Definition**: SHA-256 of canonical BSON serialization of `D(db_id)`; pins evaluation reproducibility.  
**Owner**: [02 §02-2](../02_dataset_design.md#02-2)

## sql_infeasibility_class
**Definition**: Record metadata enum: `feasible`, `semantic`, `performative`, `structural_pipeline`, `structural_schema_flex`; drives NNC graduated SQL-shortcut gate.  
**Owner**: [02 §02-2](../02_dataset_design.md#02-2), [04 §04-5](../04_agent_framework.md#04-5)

## Agent Framework Terms — Phase A (DataWorld)

## WP (Workload Profiler)
**Definition**: Agent extracting access patterns from Spider (NL, SQL) pairs to drive MongoDB schema design; outputs `scenario_summary` for Phase B NL paraphrase.  
**Owner**: [03 §03-4](../03_spider_anchored_dataworld.md#03-4)

## scenario_summary
**Definition**: WP output field: domain semantics and typical business question patterns (no SQL); consumed by NLP for reverse paraphrase only.  
**Owner**: [03 §03-4](../03_spider_anchored_dataworld.md#03-4)

## SRA (Schema Re-architect)
**Definition**: Agent redesigning relational Spider schema into workload-driven MongoDB-native layout via Stage A (11 pattern menu) + Stage B (H1–H4 schema heterogenization).  
**Owner**: [03 §03-3](../03_spider_anchored_dataworld.md#03-3), [03 §03-6](../03_spider_anchored_dataworld.md#03-6)

## SC (Schema Critic)
**Definition**: Agent adversarially reviewing SRA output for anti-patterns, workload coverage, and flex-DB supply pre-audit.  
**Owner**: [03 §03-4](../03_spider_anchored_dataworld.md#03-4)

## DM (Data Migrator)
**Definition**: Agent migrating Spider relational data into the SRA-designed MongoDB collections.  
**Owner**: [03 §03-4](../03_spider_anchored_dataworld.md#03-4)

## flex_eligible
**Definition**: Boolean on spider_db_catalog entries: DB has schema-flex heterogenization in Phase A (natural H1–H4 or build-policy H0 when `TEND_FORCE_DOCUMENT_FLEX=1`).  
**Owner**: [02 §02-II-2](../02_dataset_design.md#02-ii-2), [03 §03-4](../03_spider_anchored_dataworld.md#03-4)

## min_flex_db_ratio
**Definition**: SC pre-audit threshold (default 0.30); when selected flex_eligible DB ratio falls below, Coverage Controller relaxes H7/H9 quotas.  
**Owner**: [03 §03-4](../03_spider_anchored_dataworld.md#03-4), [02 §02-4](../02_dataset_design.md#02-4)

## Agent Framework Terms — Phase B (Query Construction)

## QPS (Query Plan Sampler)
**Definition**: Agent sampling `query_plan` from schema, witness summary, quota state, and scenario_summary; controls coverage and complexity.  
**Owner**: [04 §04-2](../04_agent_framework.md#04-2)

## MS (MQL Synthesizer)
**Definition**: Agent synthesizing `mql_primary` + `mql_alt` via ≥2 independent paths; mechanically derives `canonical_form_set`.  
**Owner**: [04 §04-3](../04_agent_framework.md#04-3)

## MUT (Mutation Generator)
**Definition**: Agent producing 5–8 plausible wrong MQL variants per record for P3 discriminativeness validation.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## PV (Property Verifier)
**Definition**: Agent verifying semantic properties, mutations EX-fail, and AST_check on witness D.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## NLP (NL Paraphraser)
**Definition**: Agent reverse-paraphrasing canonical + colloquial NLQ from locked MQL and query_plan.  
**Owner**: [04 §04-4](../04_agent_framework.md#04-4)

## RTV (Round-Trip Verifier)
**Definition**: Independent NL→MQL agent verifying NL information-lossless closure; canonical must ∈ gold-class.  
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

## cross-domain holdout
**Definition**: Train/test split disjoint at Spider domain level; no domain overlap between splits.  
**Owner**: [02 §02-3](../02_dataset_design.md#02-3)

## L0–L4
**Definition**: NoSQL nativeness difficulty tiers; L4 = highly NoSQL-native; test hard constraints L4 ≥ 30%, L0 ≤ 5%, schema_flex ≠ none ≥ 25% (with supply-relax).  
**Owner**: [04 §04-5](../04_agent_framework.md#04-5), [02 §02-4](../02_dataset_design.md#02-4)

## six-axis coverage
**Definition**: Coverage axes with min+max dual quotas: domain, join_depth, aggregation_depth, schema_pattern, schema_flex, difficulty_tier.  
**Owner**: [02 §02-4](../02_dataset_design.md#02-4)

## schema_flex
**Definition**: Record/db metadata for SRA Stage B heterogenization type: `none`, `polymorphic`, `attribute_bag`, `schema_versioning`, or `dynamic_key`.  
**Owner**: [02 §02-2](../02_dataset_design.md#02-2), [03 §03-6](../03_spider_anchored_dataworld.md#03-6)

<a id="__variants"></a>
## __variants
**Definition**: Optional collection-level array in `mongodb_schema/<db_id>.json` declaring document shape variants with `discriminator`, `fields`, `coverage`, and `source_signal`.  
**Owner**: [03 §03-6](../03_spider_anchored_dataworld.md#03-6)

## heterogenization
**Definition**: Optional block in `agent_design_rationale/<db_id>.yaml` recording which H0/H1–H4 triggers fired and evidence (Spider signals or build policy).  
**Owner**: [03 §03-6](../03_spider_anchored_dataworld.md#03-6)

## H0 (build-policy schema flex)
**Definition**: Deterministic Stage B fallback when no natural H1–H4 fires: forces `schema_flex=polymorphic` and collection `__variants` for catalog-qualifying dbs. Controlled by `TEND_FORCE_DOCUMENT_FLEX` (default on). Not Spider-signal-driven — intentional departure from proposal 03 §6 workload-preserving-only rule. `orchestra` is excluded to preserve the canonical anchor.  
**Owner**: build policy / [`tend/phase_a/sra.py`](../../tend/phase_a/sra.py)

## H1–H4 (schema-flex triggers)
**Definition**: Deterministic Stage B triggers — H1 polymorphic_subtype, H2 sparse_attribute_bag, H3 temporal_schema_version, H4 eav_promote. Priority H4 > H1 > H2 > H3.  
**Owner**: [03 §03-6](../03_spider_anchored_dataworld.md#03-6)

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
