# TEND v2-Agent Glossary

> Single source of truth for cross-volume terminology. Each term lists its owning volume and primary anchor.

## Core Task Terms

## AST_check
**Definition**: Static boolean check that a candidate MQL string satisfies all four subsets of `canonical_form_set` (collection, stage_shape, operator_allowlist, projection_shape).  
**Owner**: [01 §01-3](./01_task_definition.md#01-3)  
**See also**: EX, canonical_form_set

<a id="canonical_form_set"></a>
## canonical_form_set
**Definition**: Four-tuple equivalence-class specification `{collection, stage_shape, operator_allowlist, projection_shape}` defining gold-as-class membership for a record.  
**Owner**: [01 §01-3](./01_task_definition.md#01-3), derived in [04 §04-4](./04_agent_framework.md#04-4)  
**See also**: gold-as-class, AST_check

## canonical representative (MQL)
**Definition**: The named representative query stored in record field `MQL`; one member of `gold-class(r)`.  
**Owner**: [01 §01-3](./01_task_definition.md#01-3)

## db_id
**Definition**: Unique database identifier indexing schema `S` and frozen snapshot `D(db_id)`. In v2-Agent, sourced from Spider 1.0 DB ids.  
**Owner**: [01 §01-1](./01_task_definition.md#01-1), [02 §02-2](./02_dataset_design.md#02-2)

## dual-bridge defeat
**Definition**: Validation gate rejecting queries trivially derivable from SQL translation or a fixed template without NoSQL-native reasoning.  
**Owner**: [04 §04-3](./04_agent_framework.md#04-3)

## EX (Execution Match)
**Definition**: Primary correctness metric: `AST_check = pass` AND `NormExec(q_p, D) ≡_rec NormExec(q_g, D)`.  
**Owner**: [01 §01-2](./01_task_definition.md#01-2), [05 §05-2](./05_evaluation_methodology.md#05-2)

## gold-as-class
**Definition**: Correctness anchor treating gold as an equivalence class, not a single MQL literal.  
**Owner**: [01 §01-3](./01_task_definition.md#01-3)

## Norm
**Definition**: Four-layer result normalization (scalar / composite / null-vs-missing / _id + shape-preserving).  
**Owner**: [01 §01-4](./01_task_definition.md#01-4)

## NormExec
**Definition**: Composite operator `Norm(Exec(Parse(q), D))`; all execution equality uses NormExec, never raw Exec.  
**Owner**: [01 §01-1](./01_task_definition.md#01-1)

## ≡_rec
**Definition**: Recursive structural equality on normalized BSON results; the execution-layer equality relation.  
**Owner**: [01 §01-5](./01_task_definition.md#01-5)

## P1–P4
**Definition**: Instance-level root principles — P1 execution correctness, P2 semantic uniqueness, P3 discriminability, P4 world non-triviality.  
**Owner**: [01 §01-6](./01_task_definition.md#01-6)

## QIM (Query Intent Match)
**Definition**: Structural half of EX; AST-level alignment without requiring execution match.  
**Owner**: [05 §05-2](./05_evaluation_methodology.md#05-2)

## Schema (S)
**Definition**: Complete schema description for `db_id`: collections, field tree, types, and design rationale.  
**Owner**: [01 §01-1](./01_task_definition.md#01-1), [03 §03-3](./03_spider_anchored_dataworld.md#03-3)

## world_signature
**Definition**: SHA-256 of canonical BSON serialization of `D(db_id)`; pins evaluation reproducibility.  
**Owner**: [02 §02-2](./02_dataset_design.md#02-2)

## Agent Framework Terms

## WP (Workload Profiler)
**Definition**: Agent extracting access patterns from Spider (NL, SQL) pairs to drive MongoDB schema design.  
**Owner**: [03 §03-4](./03_spider_anchored_dataworld.md#03-4)

## SRA (Schema Re-architect)
**Definition**: Agent redesigning relational Spider schema into workload-driven MongoDB-native layout.  
**Owner**: [03 §03-4](./03_spider_anchored_dataworld.md#03-4)

## SC (Schema Critic)
**Definition**: Agent adversarially reviewing SRA output for anti-patterns and missing workload coverage.  
**Owner**: [03 §03-4](./03_spider_anchored_dataworld.md#03-4)

## DM (Data Migrator)
**Definition**: Agent migrating Spider relational data into the SRA-designed MongoDB collections.  
**Owner**: [03 §03-4](./03_spider_anchored_dataworld.md#03-4)

## QRA (Query Re-author)
**Definition**: Agent producing NoSQL-native MQL from Spider SQL/NL workload (translate + generate dual track).  
**Owner**: [04 §04-2](./04_agent_framework.md#04-2)

## NNC (NoSQL Nativeness Critic)
**Definition**: Agent enforcing dual-bridge defeat and L0–L4 difficulty labeling.  
**Owner**: [04 §04-3](./04_agent_framework.md#04-3)

## RA (Realism Auditor)
**Definition**: Agent validating record realism against production MongoDB usage patterns.  
**Owner**: [04 §04-5](./04_agent_framework.md#04-5)

## Coverage & Split Terms

## cross-domain holdout
**Definition**: Train/test split disjoint at Spider domain level; no domain overlap between splits.  
**Owner**: [02 §02-3](./02_dataset_design.md#02-3)

## L0–L4
**Definition**: NoSQL nativeness difficulty tiers; L4 = highly NoSQL-native; dataset hard constraint L4 ≥ 15%.  
**Owner**: [04 §04-3](./04_agent_framework.md#04-3), [02 §02-4](./02_dataset_design.md#02-4)

## five-axis coverage
**Definition**: Simplified coverage axes replacing v2 9+1: domain, join_depth, aggregation_depth, schema_pattern, difficulty_tier.  
**Owner**: [02 §02-4](./02_dataset_design.md#02-4)
