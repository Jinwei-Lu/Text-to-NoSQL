# Codex-native MongoDB DataWorld and NL-MQL Pipeline Design

## Status

Approved direction from discussion on 2026-06-03. This document defines the design to replace the current rule-based relational-to-document migration and schema-driven question construction with a Codex-designed MongoDB-native DataWorld plus a feature-manifest-driven NL-MQL construction pipeline.

## Goal

TEND should build a benchmark whose core tasks cannot be fairly described as "NL2SQL plus SQL-to-MQL translation." The construction process must actively create MongoDB-native database structure and then generate questions that depend on that structure.

The target behavior is:

1. Codex designs each MongoDB DataWorld from the original SQL schema, foreign keys, workload, data samples, and data distributions.
2. Codex outputs a structured migration recipe, not final JSON data.
3. A deterministic Python executor materializes MongoDB data from the recipe.
4. Every generated field is traceable to original source columns or an explicit derivation rule.
5. The DataWorld records MongoDB-native features such as polymorphic collections, dynamic keys, attribute bags, derived tags, versioned documents, missing-field semantics, and nested event streams.
6. The NL-MQL pipeline constructs records from the native feature manifest, not by opportunistically scanning ordinary schema fields.
7. Each benchmark record carries evidence that it uses a MongoDB-native feature and is not just a straightforward SQL-style join, filter, or group-by task.

## Non-goals

This design does not allow Codex to directly generate final `mongodb_data/*.json`. That would be hard to audit and easy to criticize as fabricated data.

This design does not remove deterministic validation. Codex may design the DataWorld, but the executor, verifier, and final release gates must remain deterministic.

This design does not require every record to be maximum difficulty. A release may include some lower-complexity records, but the core benchmark claim must be backed by explicit MongoDB-native coverage and anti-SQL-transfer evidence.

## Current Problem

The current migration path is deterministic and conservative. `DataMigrator` builds a plan from foreign-key cardinalities, materializes embedded documents and array projections, then derives schema-less features from the resulting documents. This produces MongoDB-shaped data, but many tasks remain easy to solve through a SQL-first path.

For example, an `account` table and a `transaction` table may become an account document with a `transactions` array. A question such as "sum transaction amount by transaction type" uses `$unwind` and `$group`, but it is still equivalent to a simple SQL `GROUP BY` over the original child table.

The benchmark needs a stronger construction contract: the database conversion stage must deliberately create MongoDB-native structure, and the NL-MQL stage must require questions to use that structure.

## Architecture

The new construction path has two coordinated phases.

### Phase A: Codex-native DataWorld construction

Phase A becomes a three-step process:

1. `CodexMigrationDesigner` reads the source database context and emits a migration recipe.
2. `MigrationRecipeVerifier` checks the recipe for provenance, executability, and MongoDB-native complexity.
3. `RecipeExecutor` materializes MongoDB data and emits a native feature manifest.

The old deterministic migrator remains useful as a fallback baseline and as a source of helper primitives, but it is no longer the primary designer.

### Phase B: Native-feature NL-MQL construction

Phase B becomes a manifest-driven pipeline:

1. `NativeCoveragePlanner` chooses feature and query-pattern targets from the manifest.
2. `NativeIntentBuilder` generates task intents bound to those features.
3. `NativeGoldCompiler` compiles gold MQL for supported patterns when possible.
4. `NativeNlGenerator` writes canonical and colloquial English questions.
5. `NativeVerifier` checks execution, semantic alignment, and feature usage.
6. `AntiSqlTransferGate` rejects records that are only SQL-style tasks.
7. `NativeDiversityLedger` limits repetition across feature, pattern, operator skeleton, MQL, and natural language.

The existing agent lifecycle, logging, progress, LLM client, and final dataset writer can stay, but the slot source and validation contracts must change.

## Migration Recipe

Codex writes one recipe per database. The recipe is a structured YAML or JSON document. It is the only source of authority for how the MongoDB DataWorld is built.

Example shape:

```yaml
db_id: financial
recipe_version: 1
design_goal: "Create document-native financial entities and account activity structures."
collections:
  financial_entities:
    purpose: "Polymorphic financial products and account records."
    source_tables: ["account", "loan", "card", "deposit"]
    transforms:
      - id: entity_union
        type: polymorphic_union
        discriminator: entity_type
        variants:
          account:
            source_table: account
            fields:
              entity_id:
                expr: "concat('account:', account.account_id)"
                provenance: ["account.account_id"]
              balance:
                source: "account.balance"
          loan:
            source_table: loan
            fields:
              entity_id:
                expr: "concat('loan:', loan.loan_id)"
                provenance: ["loan.loan_id"]
              principal:
                source: "loan.amount"
              status:
                source: "loan.status"
      - id: risk_tags
        type: derived_tag_array
        target_field: risk_tags
        tags:
          active_debt:
            condition: "loan.status == 'active'"
            provenance: ["loan.status"]
          large_balance:
            condition: "account.balance >= percentile(account.balance, 0.9)"
            provenance: ["account.balance"]

  account_activity:
    purpose: "Monthly account activity represented by dynamic keys and nested event streams."
    source_tables: ["account", "transaction"]
    transforms:
      - id: activity_by_month
        type: dynamic_key_object
        parent_table: account
        child_table: transaction
        join:
          left: "account.account_id"
          right: "transaction.account_id"
        target_field: activity_by_month
        key:
          expr: "month(transaction.date)"
          provenance: ["transaction.date"]
        values:
          credit:
            expr: "sum(transaction.amount where transaction.type == 'credit')"
            provenance: ["transaction.amount", "transaction.type"]
          withdrawal:
            expr: "sum(transaction.amount where transaction.type == 'withdrawal')"
            provenance: ["transaction.amount", "transaction.type"]
      - id: event_stream
        type: nested_event_stream
        target_field: events
        event_source_table: transaction
        event_type_field: "transaction.type"
        event_time_field: "transaction.date"
        event_payload:
          amount: "transaction.amount"
          channel: "transaction.operation"
```

The recipe supports a controlled set of transform types:

- `polymorphic_union`
- `optional_embed`
- `dynamic_key_object`
- `attribute_bag`
- `derived_tag_array`
- `versioned_document`
- `nested_event_stream`
- `shape_preserving_projection`
- `reference_collection`

Each transform must declare source tables, source columns, target fields, and derivation logic. If the declarative transform set cannot express a needed operation, Codex may provide restricted helper code. Helper code is allowed only when the verifier can run it deterministically, extract source-column provenance, and compare output against source-derived invariants.

## Recipe Verification

The verifier rejects a recipe before materialization when any of these checks fail:

1. Every referenced source table and column exists.
2. Every target field has either direct source provenance or an explicit derivation rule.
3. Join paths are valid and do not create uncontrolled Cartesian products.
4. Dynamic-key transforms declare the source of keys and values.
5. Derived tags declare deterministic conditions.
6. Polymorphic unions declare discriminator values and per-variant fields.
7. Versioned documents declare version source, version ordering, and payload provenance.
8. Nested event streams declare event ordering and parent linkage.
9. The recipe produces at least one meaningful MongoDB-native feature for the database.
10. The recipe records enough examples and coverage estimates to support question planning.

The verifier should also score native complexity. A recipe that only embeds child rows under parents is valid as a data transformation but not valid as a research-grade DataWorld unless it includes stronger native features.

## Materialized Artifacts

The dataset directory should include the existing release files plus new audit artifacts:

```text
mongodb_data/<db_id>.json
mongodb_schema/<db_id>.json
agent_design_rationale/<db_id>.yaml
bird_db_catalog.json
test.json
TEND.json
migration_recipe/<db_id>.yaml
native_feature_manifest/<db_id>.yaml
provenance/<db_id>.json
```

`migration_recipe` records Codex's design.

`native_feature_manifest` records the MongoDB-native features that downstream question construction can use.

`provenance` records source-to-target lineage at field and transform granularity.

## Native Feature Manifest

The feature manifest is the contract between Phase A and Phase B.

Example:

```yaml
db_id: financial
features:
  - id: financial_entities.product_polymorphism
    type: polymorphic_collection
    collection: financial_entities
    discriminator: entity_type
    variants:
      account:
        fields: ["balance", "district_id", "risk_tags"]
      loan:
        fields: ["principal", "status", "risk_tags"]
      card:
        fields: ["credit_limit", "used_amount", "risk_tags"]
    coverage:
      document_count: 1850
      variant_counts:
        account: 1000
        loan: 300
        card: 550
    supported_query_patterns:
      - subtype_field_dispatch
      - subtype_specific_projection
      - mixed_entity_exposure
    required_native_constructs:
      - "$switch"
      - "discriminator_branch"
    provenance_refs:
      - "account.balance"
      - "loan.amount"
      - "card.used_amount"

  - id: account_activity.monthly_dynamic_keys
    type: dynamic_key_object
    collection: account_activity
    field: activity_by_month
    key_meaning: month
    coverage:
      document_count: 1000
      non_empty_count: 872
      distinct_keys: 24
    supported_query_patterns:
      - object_to_array_filter
      - dynamic_key_comparison
      - key_range_projection
    required_native_constructs:
      - "$objectToArray"
      - "$filter"
    provenance_refs:
      - "transaction.date"
      - "transaction.amount"
      - "transaction.type"
```

Feature records must be concrete enough for Phase B to compile or verify query patterns. A vague feature such as "uses nested data" is not sufficient.

## Phase B Pipeline Redesign

The old schema-derived slots are replaced by native feature slots.

Each slot has:

```yaml
slot_id: financial:account_activity.monthly_dynamic_keys:dynamic_key_comparison:001
db_id: financial
feature_id: account_activity.monthly_dynamic_keys
feature_type: dynamic_key_object
query_pattern: dynamic_key_comparison
anti_sql_transfer_target: strong
target_shape_policy: preserve
target_difficulty: L4
required_native_constructs: ["$objectToArray", "$filter"]
forbidden_degenerate_patterns:
  - "simple_group_by"
  - "plain_unwind_group"
```

### NativeCoveragePlanner

The planner controls distribution across:

- database
- feature type
- query pattern
- native operator family
- output shape policy
- difficulty
- anti-SQL-transfer level

Release targets should include minimum coverage for dynamic keys, polymorphism, nested event streams, missing-field semantics, derived tags, and versioned or attribute-bag structures when the source supply supports them.

### NativeIntentBuilder

The intent builder receives the selected feature, its provenance, example documents, and supported query pattern. It must produce an intent that explicitly uses the feature's business meaning.

For a dynamic month-key feature, a good intent is:

```text
Find accounts that have any month where withdrawal exceeded credit, and return the months that triggered the condition.
```

A bad intent is:

```text
Group transactions by month.
```

The bad intent is rejected because it collapses back to a SQL-style aggregation over a child table.

### NativeGoldCompiler

Supported native query patterns should be compiled deterministically whenever possible.

Examples:

- `dynamic_key_comparison` compiles to `$objectToArray`, `$filter`, and shape-preserving projection.
- `subtype_field_dispatch` compiles to `$switch` over the discriminator.
- `missing_vs_present` compiles to `$exists`, `$type`, or explicit `$ifNull` logic.
- `tag_combination` compiles to `$all`, `$in`, `$setIntersection`, or related set expressions.
- `nested_event_window` compiles to `$filter`, `$map`, `$reduce`, and event-time predicates.
- `attribute_bag_lookup` compiles to `$objectToArray` or array filtering over key-value pairs.

LLM-generated MQL is allowed only for patterns that are not yet supported by the compiler. Those records must pass stricter execution, AST, and native-construct verification.

### NativeNlGenerator

The natural-language generator writes canonical and colloquial English questions from a verified intent and gold query. It must preserve the native semantic requirement.

For example, if the query distinguishes missing from null, the question must say "missing", "absent", "without", or an equivalent business phrase. If the query uses dynamic month keys, the question must mention months or period-specific values rather than generic grouping.

### NativeVerifier

The verifier checks:

1. The gold query executes and returns non-empty results when expected.
2. The query uses the required native constructs for the selected feature.
3. The query accesses the feature path declared in the manifest.
4. The natural language mentions the feature's business meaning.
5. The output shape matches the selected shape policy.
6. The record's provenance references are valid.
7. The record is not a degenerate SQL-style task.

### AntiSqlTransferGate

This gate labels and filters records by how much they resist a SQL-first solution.

Weak records are allowed only as a small minority. A weak record may use MongoDB syntax but still reduce to plain join, filter, or group-by.

Medium records require document semantics such as missing-field logic, subtype-specific branches, tag arrays, or shape-preserving projections.

Strong records require operations such as dynamic key iteration, nested array filtering without full flattening, version-window logic, or polymorphic field dispatch.

The gate stores evidence in the record rather than only assigning a label.

## Record Schema Extension

Generated records should include native metadata:

```json
{
  "record_id": 1001,
  "db_id": "financial",
  "nl_queries": {
    "canonical": "Find accounts that have any month where withdrawals exceeded credits.",
    "colloquial": "Which accounts had a month where they withdrew more than they received?"
  },
  "MQL": "db.account_activity.aggregate([...])",
  "native_feature_id": "account_activity.monthly_dynamic_keys",
  "native_feature_type": "dynamic_key_object",
  "native_query_pattern": "dynamic_key_comparison",
  "mongo_native_constructs": ["$objectToArray", "$filter"],
  "anti_sql_transfer_level": "strong",
  "anti_sql_transfer_evidence": [
    "query iterates object keys that are data values",
    "output preserves account documents with triggering months"
  ],
  "provenance_refs": [
    "transaction.date",
    "transaction.type",
    "transaction.amount"
  ],
  "migration_recipe_ref": "migration_recipe/financial.yaml#account_activity.activity_by_month",
  "native_verification": {
    "passed": true,
    "feature_path_used": "activity_by_month",
    "required_constructs_present": true
  }
}
```

Existing fields such as `canonical_form_set`, `difficulty`, `sql_infeasibility_class`, `shape_policy`, `world_signature`, and signatures remain.

## Validation and Publishing

Release validation must add native checks:

1. Every core record references a valid native feature.
2. Every referenced feature exists in `native_feature_manifest`.
3. The MQL uses required native constructs for its feature and pattern.
4. The record's provenance references resolve to the recipe or source columns.
5. Anti-SQL-transfer coverage meets release thresholds.
6. Native feature coverage meets release thresholds.
7. Degenerate SQL-style records stay below an explicit cap.

The validator should still check existing release requirements: field presence, JSON schema, world signature, MQL static checks, duplicate MQL, duplicate canonical natural language, MQL skeleton family size, per-database artifacts, and `test.json` / `TEND.json` equality.

## Observability

The run logs should expose:

- recipe generation status
- recipe verification failures
- materialization counts by collection
- native feature counts and coverage
- Phase B slot targets by feature type and pattern
- record drops by native verifier reason
- anti-SQL-transfer distribution
- final native coverage summary

Useful events include:

```text
codex_recipe_generated
recipe_verified
recipe_rejected
recipe_materialized
native_feature_manifest_written
native_slot_plan
native_record_built
native_record_dropped
native_verifier_failed
anti_sql_transfer_rejected
native_release_summary
```

## Testing Strategy

Unit tests:

- recipe parser accepts valid transform types and rejects malformed transforms
- recipe verifier rejects missing provenance, unknown columns, invalid joins, and unsupported dynamic-key definitions
- executor materializes deterministic output for representative transforms
- native feature manifest records expected coverage
- native slot planner balances feature types and query patterns
- gold compilers produce executable MQL for supported patterns
- native verifier rejects queries that do not use required constructs
- anti-SQL-transfer gate labels weak, medium, and strong records correctly

Integration tests:

- one small database builds a Codex-designed DataWorld in stub mode
- one manifest-driven Phase B run builds at least one dynamic-key record and one polymorphic record
- validation rejects a record that claims a dynamic-key feature but does not use dynamic-key constructs
- validation rejects a record with unresolved provenance
- smoke release validates with native artifacts present

Live validation:

- run a small `financial` build with a generated recipe
- inspect `events.jsonl`, `anomalies.jsonl`, `progress.jsonl`
- run `tend validate --smoke`
- compare the native anti-SQL-transfer distribution against the intended minimums

## Implementation Shape

The implementation should avoid one large rewrite. The preferred sequence is:

1. Add recipe and manifest dataclasses plus parser and verifier.
2. Add executor support for a small transform set: polymorphic union, dynamic-key object, derived tag array, and nested event stream.
3. Add artifact writers for `migration_recipe`, `native_feature_manifest`, and `provenance`.
4. Replace Phase B slot planning with manifest-derived slots for the new mode.
5. Add native gold compilers for the first supported query patterns.
6. Add native verifier and anti-SQL-transfer gate.
7. Extend record schema and release validation.
8. Keep old deterministic migration available behind a legacy or fallback path until the new pipeline is validated.

## Success Criteria

The design is successful when a small run can produce a dataset slice where:

- MongoDB data is generated from a Codex-authored recipe rather than a fixed foreign-key embedding plan.
- Every generated field can be traced to source data or a derivation rule.
- The feature manifest contains concrete MongoDB-native features with coverage evidence.
- Phase B records are planned from the feature manifest.
- At least one dynamic-key record and one polymorphic record are built and validated.
- Records include native feature metadata and anti-SQL-transfer evidence.
- Validation can reject a SQL-style record that falsely claims native complexity.

The research claim is successful only when a strong NL2SQL-transfer baseline is evaluated against the native subset and shows meaningful degradation compared with tasks that are SQL-style or weakly native.
