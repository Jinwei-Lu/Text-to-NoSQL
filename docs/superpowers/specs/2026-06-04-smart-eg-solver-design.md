# SMART-EG Solver Design

Date: 2026-06-04

## Purpose

SMART-EG is a provider-native ReAct solver for schema-less MongoDB Text-to-NoSQL tasks.
The solver input is only:

- `nlq`: the natural-language query.
- `db_handle`: an interactive read-only MongoDB database handle.
- runtime policy: budgets, provider settings, and boundary rules.

The solver must not depend on release-only fields such as difficulty, precomputed schema
files, public metadata, `shape_policy`, gold MQL, canonical form sets, train examples, or
audit artifacts.

The design replaces the current structured SMART workflow for this new solver path. The
existing structured solver remains useful as a baseline. SMART-EG is a new parallel solver
entrypoint rather than an in-place rewrite of `LLMAgent`.

## Design Position

The previous four SMART stages are retained as auditable working-memory milestones, not as
a rigid waterfall:

1. `EnvironmentModel`
2. `IntentHypothesis`
3. `QueryPlan`
4. `FinalMQL`

The runtime is one provider-native tool-call agent loop with mode-specific tools. The
agent may revisit earlier milestones when execution evidence challenges a prior claim.
The stability mechanism is not perplexity and not LLM debate. The stability mechanism is
evidence debt:

```text
claim -> evidence -> debt -> probe -> submit gate -> revisit or finalize
```

Every important assumption that can affect MQL must be supported by tool evidence. Missing
support becomes `EvidenceDebt`. Submit tools are rejected until blocking evidence debt is
cleared or the solver explicitly abandons with a typed failure.

## Non-Goals

- Do not expose arbitrary shell, filesystem, or unrestricted Mongo tools to the solver.
- Do not accept natural-language final answers as successful completion.
- Do not degrade live provider-native tool calls into content JSON actions.
- Do not use a second LLM debate mechanism.
- Do not treat provider timeout, stream stall, 429, 5xx, or route failover as agent
  observations.
- Do not return raw database rows to the model-visible observation unless a strict
  redaction policy explicitly permits a tiny bounded sample.

## LLM Substrate

SMART-EG requires a provider-native tool-call substrate. The LLM client must support:

- OpenAI-compatible `tools` schemas.
- Optional `tool_choice`.
- Assistant messages with provider-native `tool_calls`.
- Tool result messages with `role="tool"`, `tool_call_id`, `name`, and bounded content.
- Streaming live calls.
- Fixed first-token timeout of 6 seconds for live SMART-EG calls.
- A longer inter-token and total-call timeout after the stream has started.
- Transparent transport retry for provider problems.
- Honest usage and cost accounting with `api`, `estimated`, `unavailable`, and `error`
  cost sources.

Provider errors are handled below the agent loop. A first-token timeout, stream stall,
429, 5xx, connection reset, SSE interruption, or route fallback must not consume an agent
turn or appear as evidence debt. These events are written to logs and progress streams.

If a provider does not support hard `tool_choice`, the substrate may retry without
`tool_choice` while still sending `tools`, plus a soft instruction naming the required
tool. The solver loop must then verify that the model actually called the required tool.
If it did not, the response is a solver-level protocol violation and can be repaired or
counted toward convergence. The live path never falls back to content JSON actions.

## Agent Protocol

SMART-EG follows OpenAI function-calling message invariants:

- Every turn sends valid `messages`, `tools`, and optional `tool_choice`.
- An assistant message with tool calls is appended to history before tools execute.
- Every assistant tool call receives exactly one matching tool result message.
- History compaction must preserve or repair assistant/tool message pairs.
- No submit can be inferred from natural-language content.

Legal terminal actions:

- `submit_final_mql`: successful solver completion if accepted by deterministic gates.
- `abandon_with_failure`: normal typed solver failure if accepted by runtime.

Milestone submit tools:

- `submit_environment_model`
- `submit_intent_hypothesis`
- `submit_query_plan`
- `submit_final_mql`

Each submit tool is both a model action and a deterministic runtime gate. The runtime
accepts, rejects with structured debt, or marks downstream milestones stale.

## Runtime State

```python
@dataclass
class SmartEGState:
    nlq: str
    db_id: str
    mode: Literal["environment", "intent", "planning", "execution", "repair"]
    environment: EnvironmentModel | None
    intent: IntentHypothesis | None
    query_plan: QueryPlan | None
    execution_trace: ExecutionTrace
    evidence_ledger: EvidenceLedger
    debt_queue: list[EvidenceDebt]
    candidates: list[QueryCandidate]
    best_candidate_id: str | None
    budgets: SmartEGBudgets
    counters: SmartEGCounters
    stale_milestones: set[str]
    terminal: bool
```

Runtime state is owned by code. The model reads clipped state through inspect tools and
observations. It cannot mutate runtime state except through provider-native tool calls.

## Working Memory Contracts

### EnvironmentModel

`EnvironmentModel` is a working model discovered from the interactive database.

```python
@dataclass
class EnvironmentModel:
    collections: list[CollectionProfile]
    candidate_collections: list[str]
    field_index: list[FieldProfile]
    value_groundings: list[ValueGrounding]
    relationship_candidates: list[RelationshipCandidate]
    shape_branches: list[ShapeBranch]
    dynamic_key_profiles: list[DynamicKeyProfile]
    array_profiles: list[ArrayProfile]
    open_questions: list[str]
    evidence_refs: list[str]
```

Gate requirements:

- Collections have been listed.
- NLQ-relevant collections have path discovery evidence.
- NLQ constants or entities have attempted value grounding.
- Array, dynamic-key, and relationship assumptions have matching profiles when relevant.
- Claims about field existence have evidence from `discover_paths`, `profile_path`, or
  equivalent bounded probes.

### IntentHypothesis

`IntentHypothesis` describes what the query asks, without Mongo operators.

```python
@dataclass
class IntentHypothesis:
    task_kind: Literal[
        "filter",
        "lookup",
        "aggregate",
        "reshape",
        "preserve",
        "ranking",
        "comparison",
        "unknown",
    ]
    target_entities: list[str]
    clauses: list[IntentClause]
    filters: list[IntentPredicate]
    computations: list[IntentComputation]
    output_contract: OutputContract
    missing_null_policy: list[MissingNullPolicy]
    ambiguity_notes: list[str]
    evidence_refs: list[str]
```

Gate requirements:

- Each NLQ clause is represented.
- Entities, fields, values, and output requirements link to supported evidence claims.
- No Mongo operators appear in the payload.
- Ambiguity is allowed only if it produces explicit evidence debt.

### QueryPlan

`QueryPlan` is a Mongo-native plan but not the final answer.

```python
@dataclass
class QueryPlan:
    collection: str
    stages: list[PlanStage]
    variant_strategy: list[VariantStrategy]
    relationship_strategy: list[RelationshipStrategy]
    operator_idioms: list[OperatorIdiomUse]
    expected_output: OutputContract
    risk_register: list[PlanRisk]
    evidence_refs: list[str]
```

Gate requirements:

- Each stage has rationale and linked evidence.
- Referenced paths are supported by the environment model or create blocking debt.
- Schema-less branches have variant, dynamic-key, array, or missing/null evidence.
- Native operator idioms are justified, for example dynamic keys requiring
  `$objectToArray`.
- Static safety scan passes forbidden operator checks.
- Counterexample gate runs before acceptance.

### ExecutionTrace

```python
@dataclass
class ExecutionTrace:
    prefix_runs: list[PrefixExecution]
    checkpoint_results: list[CheckpointResult]
    final_sanity_runs: list[FinalSanityRun]
    failures: list[ExecutionFailure]
    repaired_candidate_ids: list[str]
```

Requirements:

- Final candidates receive static checks.
- Important pipeline prefixes are executed or intentionally skipped with a structured
  reason.
- Execution failures have normalized signatures.
- Observations are bounded summaries, never raw gold-derived output.

## Evidence Ledger

Evidence is the central mechanism.

```python
@dataclass
class EvidenceClaim:
    claim_id: str
    claim_type: Literal[
        "collection_selection",
        "field_grounding",
        "value_grounding",
        "shape_branch",
        "relationship_grounding",
        "missing_null_semantics",
        "operator_idiom",
        "output_contract",
        "execution_checkpoint",
    ]
    statement: str
    status: Literal["unsupported", "partial", "supported", "challenged", "rejected"]
    required_evidence: list[str]
    evidence_refs: list[str]
    used_by: list[str]
```

```python
@dataclass
class EvidenceRecord:
    evidence_id: str
    source_tool: str
    tool_call_id: str
    observation_ref: str
    summary: dict
    supports_claims: list[str]
    contradicts_claims: list[str]
    redaction: dict
```

Rules:

- Tool observations create evidence records.
- The runtime can automatically link obvious evidence to claims.
- The agent can call `link_evidence` for explicit linkage.
- Submit gates inspect the ledger, not the model's prose.
- Counterexamples can challenge claims and mark milestones stale.

## Evidence Debt

```python
@dataclass
class EvidenceDebt:
    debt_id: str
    milestone: Literal["environment", "intent", "plan", "final"]
    claim_type: str
    blocking: bool
    missing_evidence: list[str]
    suggested_tools: list[str]
    normalized_signature: str
    attempts: int
```

Debt is the primary progress signal. A turn makes progress only if it reduces debt,
supports a claim, changes a candidate structurally, passes a new checkpoint, or accepts a
submit gate.

## Tools

Tools are exposed by mode. Each tool is provider-native and has a strict JSON schema.

### Environment Tools

- `list_collections`
- `sample_documents`
- `discover_paths`
- `profile_path`
- `profile_path_values`
- `search_values`
- `inspect_array_shape`
- `inspect_dynamic_keys`
- `profile_relationship_candidates`
- `run_readonly_probe`

All environment tools are read-only, bounded, and redacted. `run_readonly_probe` is the
only general probe and must require limits, reject write/random/time/function operators,
and return clipped summaries.

### Evidence Tools

- `add_evidence_claim`
- `link_evidence`
- `inspect_evidence_ledger`
- `inspect_evidence_debt`
- `mine_counterexamples`
- `check_environment_model`
- `check_intent_hypothesis`
- `check_query_plan`
- `check_final_candidate`

`mine_counterexamples` is both agent-callable and used automatically by submit gates for
query plan and final MQL submissions.

### Execution Tools

- `render_pipeline`
- `render_pipeline_prefix`
- `execute_pipeline_prefix`
- `check_prefix_checkpoint`
- `check_ast_filter`
- `run_final_sanity_execution`

Execution tools separate facts from interpretation. `execute_pipeline_prefix` runs a
bounded local prefix and returns an execution signal. `check_prefix_checkpoint` evaluates
that signal against target fields and shape requirements.

### Control and Submit Tools

- `request_mode_shift`
- `request_revisit`
- `submit_environment_model`
- `submit_intent_hypothesis`
- `submit_query_plan`
- `submit_final_mql`
- `abandon_with_failure`

`submit_final_mql` is the only successful terminal action. `abandon_with_failure` is the
only normal failure terminal action.

## Mode Tool Exposure

Environment mode exposes environment, evidence, `submit_environment_model`,
`request_mode_shift`, and `abandon_with_failure`.

Intent mode exposes relevant evidence inspection, value/path profiling, intent checks,
`submit_intent_hypothesis`, `request_revisit`, `request_mode_shift`, and
`abandon_with_failure`.

Planning mode exposes relevant evidence tools, shape and relationship inspection,
counterexample mining, static plan checks, rendering, `submit_query_plan`,
`request_revisit`, and `abandon_with_failure`.

Execution and repair modes expose rendering, prefix execution, checkpoint checks, AST
filtering, final sanity execution, counterexample mining, final checks,
`submit_final_mql`, `request_revisit`, `request_mode_shift`, and
`abandon_with_failure`.

The runtime may enter terminal-only mode. In terminal-only mode it exposes only approved
terminal tools and adds a user nudge requiring one of them.

## Submit Gates

Every submit gate returns:

```python
@dataclass
class SubmitGateResult:
    submit_tool: str
    accepted: bool
    milestone: str
    candidate_id: str | None
    violations: list[GateViolation]
    new_debts: list[EvidenceDebt]
    challenged_claims: list[str]
    stale_milestones: list[str]
    required_next_action: Literal[
        "continue",
        "revisit",
        "terminal_only",
        "abandon",
        "finalized",
    ]
```

All submit gates perform:

1. Schema validation.
2. Contract validation.
3. Evidence debt validation.

`submit_query_plan` also performs counterexample probing.

`submit_final_mql` also performs static safety checks, prefix checkpoint checks, and final
bounded sanity execution when enabled.

Every gate result is written to `submit_gates.jsonl` and returned as a tool observation if
rejected.

## Counterexample Gate

Counterexample probing is the new easy-but-effective mechanism. It is deterministic and
does not use LLM debate.

The gate extracts assumptions from a plan or final candidate, then probes likely failure
cases:

- target paths missing, null, or empty.
- dynamic keys absent or unexpectedly typed.
- `$unwind` on missing or non-array paths.
- unmatched relationship keys.
- target fields dropped by prefix stages.
- constants grounded to the wrong path.

High-risk hits create contradictory evidence, challenge related claims, add blocking
debt, and reject the submit.

## Revisit

`request_revisit` is a normal solver action, not an exception.

Rules:

- It must name a target milestone, reason, challenged claims, and relevant debt ids.
- It consumes revisit budget.
- Repeated normalized revisit signatures are rejected.
- It marks target and downstream milestones stale.

Stale propagation:

```text
environment stale -> intent, plan, final candidates stale
intent stale      -> plan, final candidates stale
plan stale        -> final candidates stale
```

Final submit cannot rely on stale milestones.

## Convergence

SMART-EG has a code-level `SmartEGConvergenceChecker`.

Hard stops:

- max tool turns.
- token/context budget.
- cost budget.

Soft terminal-only triggers:

- final turn window.
- mode budget exhausted.
- context high-water.
- evidence debt not decreasing.
- repeated submit rejection.
- repeated execution failure signature.
- revisit budget nearly exhausted.

System breakers:

- repeated unknown tool.
- repeated protocol invalid response.
- boundary rejection.
- infra fatal tool failure.

Progress signals:

- debt count or severity decreases.
- claim status improves.
- query candidate changes structurally.
- new prefix checkpoint passes.
- submit gate accepts.

Non-progress signals:

- repeated samples that do not link to debt.
- read-only browsing with no claim or debt update.
- same failed prefix signature.
- same rejected submit payload.

## Context Compaction

Compaction must preserve:

- original NLQ.
- compact environment facts.
- compact intent facts.
- compact query plan facts.
- evidence ledger summary.
- debt queue summary.
- submit rejection summary.
- mode and revisit counters.
- recent prefix executions and result summaries.
- current best candidate.
- recent assistant/tool tail.

The compactor must preserve OpenAI assistant/tool pair invariants. Runtime state remains
outside prompt history, so EvidenceLedger is not lost during compaction.

## Observability

SMART-EG adds first-class agent and evidence artifacts:

```text
runs/<run_id>/
  events.jsonl
  anomalies.jsonl
  progress.jsonl
  cost_summary.jsonl
  errors.jsonl
  llm/<agent>/<call_id>.md
  agent/<session_id>.md
  agent/<session_id>.jsonl
  evidence_ledger.jsonl
  submit_gates.jsonl
```

The markdown agent transcript is human-readable. JSONL files are the audit source of
truth.

Events written to `agent/<session_id>.jsonl`:

- `turn_start`
- `llm_request`
- `llm_response`
- `tool_call`
- `tool_observation`
- `evidence_added`
- `debt_updated`
- `submit_gate_checked`
- `submit_attempt`
- `revisit_requested`
- `retry`
- `final_outcome`

`progress.jsonl` should include solver id, db id, mode, tool turn, evidence debt count,
revisit budget, provider wait, cost, and tokens. Progress logging is best-effort and must
not affect solver correctness.

## Result Contracts

```python
@dataclass
class SmartEGPrediction:
    result_type: Literal["solver_prediction"]
    db_id: str
    nlq: str
    collection: str
    pipeline: list[dict]
    MQL: str
    disclosure: dict
    environment_model_ref: str
    intent_ref: str
    query_plan_ref: str
    execution_trace_ref: str
    evidence_ledger_ref: str
    agent_session_ref: str
    submit_gate_refs: list[str]
```

```python
@dataclass
class SmartEGFailure:
    result_type: Literal["solver_failure"]
    db_id: str
    nlq: str
    error_code: Literal[
        "INSUFFICIENT_EVIDENCE",
        "TOOL_BUDGET_EXHAUSTED",
        "PROVIDER_FAILURE",
        "BOUNDARY_REJECTED",
        "EXECUTION_UNRESOLVED",
        "NO_VALID_QUERY_FOUND",
    ]
    message: str
    last_candidate_ref: str | None
    unresolved_debts: list[str]
    evidence_ledger_ref: str
    execution_trace_ref: str
    agent_session_ref: str
```

Dummy `[]` is forbidden.

## Execution Algorithm

```python
state = initialize_state(nlq, db_handle, policy)
history = initialize_history(state)

while not state.terminal:
    convergence = checker.check(state)
    if convergence.hard_stop:
        return build_budget_failure(state)

    if convergence.terminal_only:
        tools = tool_api.terminal_tools(state)
        tool_choice = tool_api.force_terminal_choice(state)
        history.add_user(terminal_nudge(state, tools))
    else:
        tools = tool_api.tools_for_mode(state.mode, state)
        tool_choice = tool_api.force_tool_choice(state)

    response = llm.complete_with_tools(
        messages=history.build_messages(state),
        tools=tools,
        tool_choice=tool_choice,
        stream=True,
        first_token_timeout_s=6,
    )

    assistant = normalize_assistant_message(response)
    if not assistant.tool_calls:
        handle_missing_tool_call(state, history)
        continue

    history.add_assistant(assistant)

    for call in ordered_tool_calls(assistant.tool_calls):
        observation = tool_api.execute(call, state)
        history.add_tool_result(call.id, observation.llm_visible_content)
        apply_observation_to_state(observation, state)
        log_observation(observation)
        if state.terminal:
            break

    compact_if_needed(history, state)

return state.result
```

## Existing TEND Integration

SMART-EG should be introduced as a parallel solver path:

- `smart_solve_nlq_db_eg(...)`
- CLI option such as `tend solve --solver smart-eg`
- ablation support for `smart_eg_full` and mechanism-specific variants

The existing structured `smart_solve_record()` remains as a baseline.

## Ablations

Supported ablations:

- `structured_smart`
- `smart_eg_full`
- `smart_eg_no_evidence_gate`
- `smart_eg_no_counterexample`
- `smart_eg_no_value_grounding`
- `smart_eg_no_relationship_probe`
- `smart_eg_no_prefix_execution`
- `smart_eg_no_revisit`
- `smart_eg_no_probe_scheduler`
- `smart_eg_budget_low`
- `smart_eg_budget_medium`
- `smart_eg_budget_high`

These ablate mechanisms, not arbitrary stage names.

## Acceptance Criteria

Implementation is acceptable only when:

1. Live SMART-EG requests use provider-native tool calls.
2. Streaming first-token timeout is fixed at 6 seconds for live SMART-EG calls.
3. Provider retry and route fallback are invisible to the agent turn budget.
4. `submit_final_mql` is the only success exit.
5. `abandon_with_failure` is the only normal failure exit.
6. Every final prediction links to agent transcript, evidence ledger, execution trace, and
   submit gate records.
7. Submit gates reject unsupported claims with structured debt.
8. Counterexample gate can challenge claims and stale milestones.
9. No raw rows or forbidden data are returned to model-visible observations.
10. A small NLQ plus DB run shows actual tool calls, evidence updates, submit gates, and
    final prediction or typed failure in logs.

## Migration Notes from DynaDB

Patterns to migrate:

- Provider-native assistant/tool message invariants.
- ToolAPI-owned terminal state.
- Terminal-only allowlist under budget, stall, or repeated rejection.
- Streaming-first LLM calls with first-token stall detection.
- Provider retry and progress callback isolation from agent logic.
- Agent markdown transcript plus machine-readable JSONL sidecars.
- Context compaction that preserves tool-pair validity.

Patterns not to copy directly:

- Adaptive TTFT ceiling. SMART-EG uses fixed 6 seconds.
- Natural auto-submit for final answers. SMART-EG requires explicit `submit_final_mql`.
- Logging paths that omit structured evidence and gate ledgers.
- Infinite retry on one provider before route fallback.
- Generic terminal or shell tools.
