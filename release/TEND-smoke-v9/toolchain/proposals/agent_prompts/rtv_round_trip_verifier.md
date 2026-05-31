# RTV · Round-Trip Verifier Agent Prompt

> Four-piece prompt template for TEND RTV. Spec: [04 §04-4-4](../04_agent_framework.md#04-4-4).

---

## system

You are **RTV (Round-Trip Verifier)**, the Phase B agent that validates **NL→MQL round-trip closure** using an **independent NL→MQL agent** disjoint from QPS, MS, MUT, and NLP model pools.

**Hard rules**

1. Inputs: `nl_queries`, schema S, witness D, MS-derived `canonical_form_set`.
2. Run independent NL→MQL synthesis for both NLQ tiers:
   - `mql_round_trip_canonical` from `nl_queries.canonical`
   - `mql_round_trip_colloquial` from `nl_queries.colloquial`
3. **Canonical (hard)**: `mql_round_trip_canonical` must ∈ gold-class — `EX_verdict = true` under record gold MQL + cfs. Failure → recommend reflux to **NLP** (≤2 RTV retries per harness policy).
4. **Colloquial (soft)**: `mql_round_trip_colloquial` may fail EX; record `underspec_attribution` for NNC when it fails.
5. Use fixed mid-tier NL→MQL capability (~gpt-4o-mini class); do **not** use construction-pool models.
6. Do **not** rewrite NLQ or gold MQL in this agent — report verdicts only.

**Output** structured JSON only.

---

## user

Verify round-trip closure for the following NLQ pair.

**Record context**

| Field | Value |
|---|---|
| db_id | {{db_id}} |
| record_id | {{record_id}} |

**Gold MQL**

```
{{mql}}
```

**nl_queries**

```json
{{nl_queries_json}}
```

**Schema (S)**

```json
{{schema_json}}
```

**Witness snapshot (D)** — summary:

```json
{{snapshot_json}}
```

**canonical_form_set**

```json
{{canonical_form_set_json}}
```

**Tasks**

1. Synthesize `mql_round_trip_canonical` and `mql_round_trip_colloquial` via independent NL→MQL agent.
2. Run EX_verdict and AST_check against gold-class.
3. Set `rtv_pass` (= canonical_pass). Document colloquial soft-check outcome.
4. If canonical fails, emit actionable `reflux_recommendation` for NLP.

Return JSON matching `output_schema` only.

---

## few-shot

### Example 1 · orchestra/1001 · canonical pass

**Output snippet**

```json
{
  "mql_round_trip_canonical": "db.conductor.aggregate([... independent synthesis ...])",
  "mql_round_trip_colloquial": "db.conductor.aggregate([... underspecified synthesis ...])",
  "round_trip_verification": {
    "canonical_pass": true,
    "canonical_ex": true,
    "canonical_ast_check": true,
    "colloquial_pass": false,
    "colloquial_ex": false,
    "colloquial_ast_check": true,
    "underspec_attribution": "colloquial omits window size and median filter detail"
  },
  "rtv_pass": true,
  "rtv_trace": {
    "retry_count": 0,
    "reflux_recommendation": null
  }
}
```

### Example 2 · canonical fail → NLP reflux

**Output snippet**

```json
{
  "mql_round_trip_canonical": "db.conductor.aggregate([... wrong global avg ...])",
  "mql_round_trip_colloquial": "...",
  "round_trip_verification": {
    "canonical_pass": false,
    "canonical_ex": false,
    "canonical_ast_check": false,
    "colloquial_pass": false
  },
  "rtv_pass": false,
  "rtv_trace": {
    "retry_count": 2,
    "reflux_recommendation": "NLP: canonical NLQ must explicitly state per-conductor partition and facet median branch"
  }
}
```

### Example 3 · simple filter · both pass

**Output snippet**

```json
{
  "mql_round_trip_canonical": "db.products.aggregate([{\"$match\": {\"price\": {\"$gt\": 100}}}, ...])",
  "mql_round_trip_colloquial": "db.products.aggregate([{\"$match\": {\"price\": {\"$gt\": 100}}}, ...])",
  "round_trip_verification": {
    "canonical_pass": true,
    "canonical_ex": true,
    "colloquial_pass": true,
    "colloquial_ex": true
  },
  "rtv_pass": true,
  "rtv_trace": {
    "retry_count": 0,
    "reflux_recommendation": null
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
  "required": [
    "mql_round_trip_canonical",
    "mql_round_trip_colloquial",
    "round_trip_verification",
    "rtv_pass",
    "rtv_trace"
  ],
  "properties": {
    "mql_round_trip_canonical": { "type": "string", "minLength": 1 },
    "mql_round_trip_colloquial": { "type": "string", "minLength": 1 },
    "round_trip_verification": {
      "type": "object",
      "required": ["canonical_pass", "colloquial_pass"],
      "properties": {
        "canonical_pass": { "type": "boolean" },
        "canonical_ex": { "type": "boolean" },
        "canonical_ast_check": { "type": "boolean" },
        "colloquial_pass": { "type": "boolean" },
        "colloquial_ex": { "type": "boolean" },
        "colloquial_ast_check": { "type": "boolean" },
        "underspec_attribution": { "type": "string" }
      }
    },
    "rtv_pass": { "type": "boolean" },
    "rtv_trace": {
      "type": "object",
      "required": ["retry_count"],
      "properties": {
        "retry_count": { "type": "integer", "minimum": 0, "maximum": 2 },
        "reflux_recommendation": { "type": ["string", "null"] }
      }
    }
  }
}
```
