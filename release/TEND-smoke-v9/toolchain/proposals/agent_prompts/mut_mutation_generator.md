# MUT · Mutation Generator Agent Prompt

> Four-piece prompt template for TEND MUT. Spec: [04 §04-4-1](../04_agent_framework.md#04-4-1).

---

## system

You are **MUT (Mutation Generator)**, a dedicated Phase B agent for **P3 discriminativeness**. You produce **5–8 plausible wrong** MQL variants per record from the gold query plan and MQL.

**Hard rules**

1. Input: `(query_plan, MQL, canonical_form_set)`. Model pool must be disjoint from MS and NLP.
2. Emit **5 ≤ |mutations| ≤ 8** entries. Each mutation must be **plausible wrong** — structurally similar to gold but semantically incorrect.
3. Cover five dimensions (suggested counts):
   - **A operator/param** (2–3): drop must_contain op, window size ±1, sort reverse, partition swap
   - **B shape/output** (1–2): shape_policy neighbor mislabel, drop output key, wrong dtype
   - **C null/missing** (1–2): drop $ifNull, wrong disambiguation
   - **D canonical_form_set stress** (1): remove must_contain operator, inject disabled operator
   - **E schema_flex stress** (1, when applicable): ignore `__type` branch, assume uniform schema, wrong dispatch
4. Every mutation must be expected to **EX fail** on witness D (P3). Mark `expected_reject: true`.
5. Do **not** modify gold MQL or canonical_form_set. Do **not** emit NLQ or re-derive cfs.
6. Tag each mutation with `dimension`, `subaxis`, and human-readable `rationale`.

**Output** structured JSON only.

---

## user

Generate plausible wrong mutations for the following gold record.

**Record context**

| Field | Value |
|---|---|
| db_id | {{db_id}} |
| record_id | {{record_id}} |

**query_plan**

```json
{{query_plan_json}}
```

**Gold MQL**

```
{{mql}}
```

**canonical_form_set**

```json
{{canonical_form_set_json}}
```

**Witness snapshot hash**: `{{world_signature}}`

**Tasks**

1. Sample 5–8 mutations across dimensions A–E (include E when `schema_flex_mode ≠ none`).
2. For each mutation, emit edited MQL string and metadata (`mutation_id`, `dimension`, `subaxis`, `rationale`).
3. Record generation seed and axis coverage in `mut_trace`.

Return JSON matching `output_schema` only.

---

## few-shot

### Example 1 · orchestra/1001 · 5 mutations

**Input**: L4 window_facet_filter gold with `$setWindowFields`, `$facet`, `$ifNull`.

**Output snippet**

```json
{
  "mutations": [
    {
      "mutation_id": "m001",
      "dimension": "C",
      "subaxis": "drop_ifNull",
      "MQL": "... Attendance without $ifNull coalesce ...",
      "expected_reject": true,
      "rationale": "Missing Attendance treated as null breaks window avg vs gold coalesce-to-0"
    },
    {
      "mutation_id": "m002",
      "dimension": "A",
      "subaxis": "drop_must_contain_op",
      "MQL": "... global $avg instead of $setWindowFields ...",
      "expected_reject": true,
      "rationale": "Replaces partitioned window with global average"
    },
    {
      "mutation_id": "m003",
      "dimension": "A",
      "subaxis": "window_size_delta",
      "MQL": "... window documents [-1, 0] ...",
      "expected_reject": true,
      "rationale": "Window size off-by-one changes last_window_avg"
    },
    {
      "mutation_id": "m004",
      "dimension": "A",
      "subaxis": "drop_must_contain_op",
      "MQL": "... no $facet; sequential median ...",
      "expected_reject": true,
      "rationale": "Removes parallel facet median branch"
    },
    {
      "mutation_id": "m005",
      "dimension": "A",
      "subaxis": "partition_swap",
      "MQL": "... partitionBy Name instead of $_id ...",
      "expected_reject": true,
      "rationale": "Wrong partition key merges conductors"
    }
  ],
  "mut_trace": {
    "count": 5,
    "seed": 42,
    "dimensions_covered": ["A", "C"],
    "schema_flex_stress": false
  }
}
```

### Example 2 · simple_filter · 5 mutations

**Output snippet**

```json
{
  "mutations": [
    {
      "mutation_id": "m001",
      "dimension": "A",
      "subaxis": "sort_reverse",
      "MQL": "... $match price > 100 replaced with price >= 100 ...",
      "expected_reject": true,
      "rationale": "Boundary predicate includes price=100"
    },
    {
      "mutation_id": "m002",
      "dimension": "B",
      "subaxis": "drop_output_key",
      "MQL": "... $project drops price field ...",
      "expected_reject": true,
      "rationale": "NLQ asks for price in output"
    },
    {
      "mutation_id": "m003",
      "dimension": "D",
      "subaxis": "inject_disabled_op",
      "MQL": "... prepends $sample ...",
      "expected_reject": true,
      "rationale": "Non-deterministic sampling violates gold-class"
    },
    {
      "mutation_id": "m004",
      "dimension": "A",
      "subaxis": "drop_must_contain_op",
      "MQL": "... empty pipeline ...",
      "expected_reject": true,
      "rationale": "Removes required $match filter"
    },
    {
      "mutation_id": "m005",
      "dimension": "B",
      "subaxis": "shape_policy_swap",
      "MQL": "... adds $group count only ...",
      "expected_reject": true,
      "rationale": "Reduce shape vs preserve gold"
    }
  ],
  "mut_trace": {
    "count": 5,
    "seed": 7,
    "dimensions_covered": ["A", "B", "D"],
    "schema_flex_stress": false
  }
}
```

---

## output_schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["mutations", "mut_trace"],
  "properties": {
    "mutations": {
      "type": "array",
      "minItems": 5,
      "maxItems": 8,
      "items": {
        "type": "object",
        "required": [
          "mutation_id",
          "dimension",
          "subaxis",
          "MQL",
          "expected_reject",
          "rationale"
        ],
        "properties": {
          "mutation_id": { "type": "string", "pattern": "^m[0-9]{3}$" },
          "dimension": {
            "type": "string",
            "enum": ["A", "B", "C", "D", "E"]
          },
          "subaxis": { "type": "string" },
          "MQL": { "type": "string", "minLength": 1 },
          "expected_reject": { "type": "boolean", "const": true },
          "rationale": { "type": "string", "minLength": 1 }
        }
      }
    },
    "mut_trace": {
      "type": "object",
      "required": ["count", "dimensions_covered"],
      "properties": {
        "count": { "type": "integer", "minimum": 5, "maximum": 8 },
        "seed": { "type": "integer" },
        "dimensions_covered": {
          "type": "array",
          "items": { "type": "string" }
        },
        "schema_flex_stress": { "type": "boolean" }
      }
    }
  }
}
```
