# SMART-EG Phase-Scoped Agent Workflow Design

Date: 2026-06-05
Status: Approved design for implementation planning
Scope: `src/tend/solver/eg/`, SMART-EG runtime, tools, observability, tests, and solver documentation.

## Context

The current SMART-EG runtime is implemented as one provider-native tool-call loop in `src/tend/solver/eg/runtime.py`. The loop carries one `SmartEGState`, one `SmartEGHistory`, one session observer, and advances through the work by mutating `state.mode`. Tool exposure is dynamically narrowed by `SmartEGToolAPI.tools_for_state()`, but later modes can still see broad mixtures of environment, evidence, execution, stage-control, and submit tools.

The current design preserves the important solver boundary: SMART-EG receives `NLQ`, `db_id`, `record_id`, and read-only MongoDB access. It must not consume gold MQL, canonical form, shape policy, difficulty, train examples, release-private schema, audit refs, or any other answer-bearing benchmark metadata.

The next design should keep provider-native ReAct, deterministic submit gates, Evidence Debt, typed failures, and transparent provider retry semantics, while fixing two structural problems:

1. One session carries all four conceptual phases, so history, prompt scope, and tool choice stay too broad.
2. Existing tools overlap in function. Similar operations should be consolidated into fewer canonical tools whose responsibilities are orthogonal.

The repository currently has unrelated uncommitted work across solver, observability, CLI, ablation, baseline, and tests. Implementation should stage and commit only intended files at each step.

## Goals

- Replace the all-phase SMART-EG loop with a phase-scoped workflow:
  - Environment agent session
  - Intent agent session
  - Planning agent session
  - Realization agent session
- Keep a shared deterministic runtime state:
  - `SmartEGState`
  - `EvidenceLedger`
  - submit gates
  - budget counters
  - revisit counters
  - typed prediction and failure results
- Consolidate overlapping tool names into a small canonical tool set with orthogonal responsibilities.
- Expose only phase-appropriate canonical tools and operations to each provider-native phase session.
- Preserve the public solver entrypoint `smart_solve_nlq_db_eg(...)` so CLI, baseline, ablation, and evaluation integration do not need a broad caller rewrite.
- Improve observability from one `agent.md` into a workflow transcript plus phase-local transcripts.
- Add tests that prove the new runtime is genuinely phase-scoped and does not leak deprecated tools or private benchmark fields.

## Non-Goals

- Do not return to structured JSON-call agents. Every phase must use provider-native tool calls.
- Do not make "four agents" the research mechanism. The durable mechanisms remain submit gates, Evidence Debt, schema-less probes, revisit, execution probes, value grounding, and final sanity evidence.
- Do not let final sanity replace benchmark correctness. `execute_probe(mode="final_sanity")` is only bounded execution evidence for the final gate.
- Do not remove old tool implementation paths in the first implementation step. Old names can remain as compatibility shims until tests and ablations migrate.
- Do not add answer-bearing solver inputs.

## Architecture

The public entrypoint stays in `src/tend/solver/eg/runtime.py`:

```text
smart_solve_nlq_db_eg(...)
  -> build SmartEGRunContext
  -> build SmartEGState
  -> SmartEGWorkflowRunner.run()
       -> EnvironmentAgentSession.run()
       -> IntentAgentSession.run()
       -> PlanningAgentSession.run()
       -> RealizationAgentSession.run()
       -> SmartEGPrediction | SmartEGFailure
```

New or refactored module boundaries:

```text
src/tend/solver/eg/
  runtime.py           # public shim; resolves wf/llm/db/run_dir/policy; delegates to workflow runner
  workflow.py          # SmartEGWorkflowRunner, phase sequencing, revisit routing
  phases.py            # phase session classes or phase session config objects
  phase_tools.py       # canonical tool allowlists and phase operation policies
  canonical_tools.py   # canonical tool schemas, dispatch, and old-tool compatibility shims
  tools.py             # deterministic submit gates and low-level tool behavior reused by canonical tools
  contracts.py         # shared state, result, phase result, and failure contracts
  observability.py     # workflow and phase session artifact writers
```

`runtime.py` should keep the current dependency-injected and `wf` shim forms. The new `SmartEGWorkflowRunner` owns phase order and revisit routing. Each phase owns its own `SmartEGHistory` and phase prompt, but all phases share one `SmartEGState`.

## Phase Results

Phase sessions should not communicate control flow only by mutating `state.mode`. They should return explicit typed outcomes:

```text
PhaseAccepted(phase, artifact_ref, gate_ref)
RevisitRequested(target_phase, reason, challenged_claims, debt_ids)
PhaseFailed(error_code, message)
Finalized(SmartEGPrediction)
```

The runner interprets these outcomes:

- `PhaseAccepted(environment)` advances to intent.
- `PhaseAccepted(intent)` advances to planning.
- `PhaseAccepted(planning)` advances to realization.
- `Finalized` returns the prediction.
- `RevisitRequested` marks downstream milestones stale and reruns the required phase suffix.
- `PhaseFailed` returns a typed `SmartEGFailure` unless a deterministic recovery path exists.

## Canonical Tools

The new phase agents should see only canonical tools:

```text
inspect_database
manage_evidence
validate_query
execute_probe
submit_milestone
submit_final
request_revisit
abandon_with_failure
```

Deprecated raw tool names such as `discover_paths`, `profile_path_values`, `run_readonly_probe`, `check_ast_filter`, `submit_query_plan`, and `request_mode_shift` may remain internally as compatibility shims, but they must not appear in new phase prompts or phase `tools.json` files.

### `inspect_database`

Responsibility: Produce bounded read-only database observations and evidence records.

Replaces or absorbs:

- `list_collections`
- `sample_documents`
- `discover_paths`
- `profile_path`
- `profile_path_values`
- `search_values`
- `inspect_array_shape`
- `inspect_dynamic_keys`
- `profile_relationship_candidates`

Schema shape:

```json
{
  "operation": "list_collections | sample_documents | discover_paths | profile_path | search_values | infer_relationships",
  "collection": "...",
  "path": "...",
  "query": "...",
  "limit": 5
}
```

`inspect_array_shape` and `inspect_dynamic_keys` should become part of path discovery/profile output rather than separate operations. Arrays, dynamic keys, missing/null counts, type counts, and value buckets are all path-profile facts.

`inspect_database` must not create claims, submit milestones, validate MQL, or execute candidate pipelines.

### `manage_evidence`

Responsibility: Inspect evidence/debt state and manage claim-to-evidence links.

Replaces or absorbs:

- `inspect_evidence_ledger`
- `inspect_evidence_debt`
- `add_evidence_claim`
- `link_evidence`

Schema shape:

```json
{
  "operation": "inspect | add_claim | link",
  "scope": "all | current_phase | blocking_debt",
  "claim": {},
  "claim_id": "...",
  "evidence_id": "..."
}
```

`manage_evidence` must not access MongoDB, validate a query, execute a query, submit a milestone, or change phase.

### `validate_query`

Responsibility: Render or normalize MQL and run static query checks.

Replaces or absorbs:

- `render_pipeline`
- `check_ast_filter`
- static portions of prefix checks

Schema shape:

```json
{
  "collection": "...",
  "pipeline": [],
  "MQL": "...",
  "checks": ["parse", "operator_allowlist", "field_path_contract", "output_contract"]
}
```

`validate_query` must not execute MongoDB and must not submit milestones. Field-path and output-contract diagnostics can rely on the current shared evidence ledger.

### `execute_probe`

Responsibility: Run bounded read-only MongoDB execution probes and record execution evidence.

Replaces or absorbs:

- `run_readonly_probe`
- `execute_pipeline_prefix`
- `check_prefix_checkpoint`
- `run_final_sanity_execution`

Schema shape:

```json
{
  "mode": "candidate | prefix | final_sanity",
  "collection": "...",
  "pipeline": [],
  "prefix_length": 2,
  "limit": 5
}
```

`execute_probe` must not submit milestones. `mode="final_sanity"` may produce evidence required by `submit_final`, but `submit_final` remains the only successful exit.

### `submit_milestone`

Responsibility: Submit the current non-final phase artifact through deterministic gates.

Replaces:

- `submit_environment_model`
- `submit_intent_hypothesis`
- `submit_query_plan`

Schema shape:

```json
{
  "milestone": "environment | intent | plan",
  "artifact": {},
  "evidence_refs": []
}
```

The runtime must reject a milestone that does not match the active phase. `submit_milestone(milestone="plan")` from the intent phase is a protocol violation.

### `submit_final`

Responsibility: Submit final MQL. This is the only successful solver exit.

Replaces:

- `submit_final_mql`

Schema shape:

```json
{
  "collection": "...",
  "pipeline": [],
  "MQL": "...",
  "candidate_id": "...",
  "evidence_refs": []
}
```

The deterministic final gate still checks static safety, blocking debts, value grounding, field paths, output contract, and final sanity evidence when enabled.

### `request_revisit`

Responsibility: Ask the workflow runner to rerun an upstream phase.

Replaces:

- `request_revisit`
- `request_mode_shift`
- proposed phase-specific `request_revisit_*` variants

Schema shape:

```json
{
  "target": "environment | intent | plan",
  "reason": "...",
  "challenged_claims": [],
  "debt_ids": []
}
```

There is no free-form mode shift. There is no `target="execution"`. Revisit signatures are deduplicated globally by `target + reason + challenged_claims + debt_ids`.

### `abandon_with_failure`

Responsibility: End with a typed normal failure. It must not fabricate an MQL.

## Phase Tool Exposure

Phase tool exposure should be expressed as both tool allowlists and operation allowlists.

### Environment Phase

Purpose: Build a database environment model from NLQ plus read-only MongoDB access.

Allowed tools:

```text
inspect_database
manage_evidence
submit_milestone
abandon_with_failure
```

Allowed `inspect_database` operations:

```text
list_collections
sample_documents
discover_paths
profile_path
search_values
infer_relationships
```

Successful output:

```text
submit_milestone(milestone="environment")
```

The submitted environment artifact must include candidate collections, relevant paths, relationship hypotheses when available, notes, and evidence refs. The existing environment gate remains deterministic.

### Intent Phase

Purpose: Ground the NLQ into a verifiable intent hypothesis.

Allowed tools:

```text
inspect_database
manage_evidence
submit_milestone
request_revisit
abandon_with_failure
```

Allowed `inspect_database` operations:

```text
profile_path
search_values
```

Intent can verify field/value grounding but cannot perform broad discovery. It cannot list collections, discover broad path inventories, infer relationships, validate MQL, or execute probes. If the accepted environment model is insufficient, it must use `request_revisit(target="environment")`.

Successful output:

```text
submit_milestone(milestone="intent")
```

The submitted intent artifact must include task kind, target collection, target fields, filters, aggregations, output contract, and evidence refs.

### Planning Phase

Purpose: Turn accepted intent into a MongoDB physical plan and catch obvious static or bounded execution issues.

Allowed tools:

```text
validate_query
execute_probe
manage_evidence
submit_milestone
request_revisit
abandon_with_failure
```

Allowed `execute_probe` modes:

```text
candidate
prefix
```

Planning cannot use broad database exploration tools. If a plan exposes wrong or missing upstream assumptions, it must request revisit to environment or intent.

Successful output:

```text
submit_milestone(milestone="plan")
```

The submitted plan artifact must include collection, stages, plan summary, variant strategy, sentinel checks, and evidence refs. Variant strategy and sentinel checks can start as prompt/test requirements before being hardened into submit gate requirements.

### Realization Phase

Purpose: Render final MQL, run final static and bounded execution checks, and submit the final answer.

Allowed tools:

```text
validate_query
execute_probe
manage_evidence
submit_final
request_revisit
abandon_with_failure
```

Allowed `execute_probe` modes:

```text
candidate
final_sanity
```

Realization cannot use broad database exploration or submit upstream milestones. If final checks reveal bad environment, intent, or plan assumptions, it must request revisit.

Successful output:

```text
submit_final
```

`submit_final` acceptance creates the only `SmartEGPrediction`.

## Revisit Semantics

The workflow runner owns revisit execution.

```text
Realization -> request_revisit(target="environment")
  stale: environment, intent, plan, final
  rerun: Environment -> Intent -> Planning -> Realization

Planning or Realization -> request_revisit(target="intent")
  stale: intent, plan, final
  rerun: Intent -> Planning -> Realization

Realization -> request_revisit(target="plan")
  stale: plan, final
  rerun: Planning -> Realization
```

Revisit must preserve the evidence ledger and append challenge context. Revisit must be limited by `max_revisits` and repeated signature checks. When revisit budget is exhausted, the phase should enter terminal-only behavior or produce a typed failure.

## Submit Rejection Repair

Submit rejection should not automatically widen a phase's tools.

- If a rejection is locally repairable in the current phase, the current phase may continue a bounded repair loop.
- If a rejection points to upstream debt, the current phase should request revisit.
- In terminal-only mode, expose only:
  - current phase submit tool or final submit tool
  - `manage_evidence` inspection or locally valid linking
  - `request_revisit` if allowed and relevant
  - `abandon_with_failure`

The runtime must not expose early exploration operations to later phases as a shortcut.

## Observability

The observability layout should be scoped by how developers and Codex debug the
run:

- Session-level files provide navigation, current diagnosis, global progress,
  and cross-phase indexes.
- Phase-level directories summarize each phase across attempts.
- Attempt-level directories contain one provider-native ReAct agent session.
- Cross-phase ledgers remain centralized so evidence, gates, and execution facts
  are not split across phases.

The layout should be:

```text
solve/sessions/<session_id>/
  session.md
  session.json
  timeline.jsonl
  progress.jsonl
  errors.jsonl

  ledgers/
    evidence.jsonl
    gates.jsonl
    execution.jsonl

  phases/
    environment/
      index.md
      attempts/
        001/
          agent.md
          events.jsonl
          tools.json
          llm_calls.jsonl
    intent/
      index.md
      attempts/
        001/
          agent.md
          events.jsonl
          tools.json
          llm_calls.jsonl
    planning/
      index.md
      attempts/
        001/
          agent.md
          events.jsonl
          tools.json
          llm_calls.jsonl
        002/
          agent.md
          events.jsonl
          tools.json
          llm_calls.jsonl
    realization/
      index.md
      attempts/
        001/
          agent.md
          events.jsonl
          tools.json
          llm_calls.jsonl
```

### Session-Level Files

`session.md` is the human entrypoint. It must be concise and navigational, not a
full transcript. It should include:

- outcome
- inputs
- current diagnosis
- phase summary
- latest blocking issue
- latest error
- latest gate
- revisit history
- next files and refs to inspect

`session.md` should let a developer or Codex understand where the run is stuck
without replaying JSONL files, then point to the specific phase attempt and
ledger lines needed for deeper inspection.

`session.json` is the machine index. It should include stable refs for the
session files, current phase, current attempt, phase indexes, latest attempt
files, centralized ledgers, and latest event/gate/error refs. It replaces any
separate manifest or refs files.

Example shape:

```json
{
  "schema_version": "smart_eg_session_v2",
  "session_id": "solve-smart-eg-financial-163297",
  "status": "running",
  "current_phase": "planning",
  "current_attempt": 2,
  "phase_refs": {
    "planning": {
      "index": "phases/planning/index.md",
      "latest_attempt": "phases/planning/attempts/002/agent.md"
    }
  },
  "ledger_refs": {
    "evidence": "ledgers/evidence.jsonl",
    "gates": "ledgers/gates.jsonl",
    "execution": "ledgers/execution.jsonl"
  },
  "latest_refs": {
    "event": "timeline.jsonl#31",
    "phase_event": "phases/planning/attempts/002/events.jsonl#9",
    "agent": "phases/planning/attempts/002/agent.md",
    "gate": "ledgers/gates.jsonl#7",
    "error": null
  }
}
```

`timeline.jsonl` is the cross-phase orchestration stream. It records only
workflow events:

- session started
- phase started
- phase accepted
- phase failed
- revisit requested
- revisit rerun started
- terminal-only transition
- final outcome

It must not duplicate every LLM turn or tool call; those belong in attempt-level
`events.jsonl`.

`progress.jsonl` contains time-series snapshots for terminal rendering and stall
detection. It is not a source of truth for tools, gates, or evidence.

`errors.jsonl` is the global error/anomaly index. It should include only records
that require attention, with refs to the phase attempt, ledger line, and LLM call.
Normal events do not belong here.

### Central Ledgers

`ledgers/evidence.jsonl` is the single evidence ledger event stream. It records:

- evidence
- claims
- claim-to-evidence links
- debts
- debt resolution

Do not split claims or debts into separate files.

`ledgers/gates.jsonl` is the single submit gate source of truth. It records every
accepted and rejected environment, intent, plan, and final submit gate.

`ledgers/execution.jsonl` is the single query validation and execution source of
truth. It records `validate_query` diagnostics, `execute_probe` results, candidate
probes, prefix probes, and final sanity evidence.

### Phase and Attempt Files

Each phase directory has an `index.md`. It summarizes that phase across attempts:

- attempts table
- latest diagnosis
- latest rejected gate for the phase
- debts relevant to the phase
- refs to each attempt

Each attempt directory is one provider-native ReAct agent session.

`agent.md` is the complete readable transcript for that attempt:

- system prompt
- user context
- allowed tools and operation limits
- turn-by-turn assistant content/reasoning
- tool calls
- tool results
- submit, revisit, or failure outcome

`events.jsonl` is the detailed attempt event stream:

- LLM request summary
- LLM response summary
- tool call
- tool result
- operation rejected
- submit attempt
- phase-local error

`tools.json` is the exact tool schema and operation policy exposed to that
attempt. It is attempt-local because revisit attempts may run with different
debt context and prompt context, even when the phase policy is the same.

`llm_calls.jsonl` is the attempt-local LLM diagnostics ledger. It replaces many
diagnostics sidecar files. It should include one record per provider call:

```json
{
  "turn_index": 5,
  "call_id": "call_xxx",
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "request_ref": "events.jsonl#18",
  "response_ref": "events.jsonl#19",
  "diagnostics": {
    "latency_s": 3.2,
    "first_token_latency_s": 0.9,
    "finish_reason": "tool_calls",
    "usage": {},
    "cost": {},
    "retry_count": 0,
    "route": "primary"
  }
}
```

If the LLM client must still emit per-call diagnostics sidecars for compatibility,
place them under the attempt directory as `llm/<call_id>.json`. The filename must
not duplicate phase, turn, or diagnostics labels already represented by the
directory and JSON fields.

### Reference Rules

Avoid duplicate sources of truth:

- `session.md` and `session.json` point to phase indexes, latest attempts, and
  ledger lines.
- `timeline.jsonl` points to phase attempts and ledger refs but does not repeat
  attempt-level details.
- phase `events.jsonl` points to `agent.md` anchors, centralized ledger lines,
  and `llm_calls.jsonl` lines.
- `agent.md` is readable transcript context and can include summarized tool
  results, but ledger facts remain authoritative in `ledgers/`.
- `errors.jsonl` is an index of problems and must link to the relevant attempt,
  ledger, and LLM call records.

Submit gate facts are authoritative only in `ledgers/gates.jsonl`. Evidence facts
are authoritative only in `ledgers/evidence.jsonl`. Query validation/execution
facts are authoritative only in `ledgers/execution.jsonl`.

### Terminal Progress Display

The terminal panel should surface timely diagnosis and the next useful refs. It
should not render full prompts, raw JSON payloads, or full tool results.

The SMART-EG task detail should include:

```text
phase=planning attempt=002 turn=5/12 global=18/80
last=submit_rejected debt=1 top=plan_field_path_missing
reject=2 revisit=1/4 tokens=18420 cost=$0.031
agent=phases/planning/attempts/002/agent.md gate=ledgers/gates.jsonl#7
```

The footer should keep run-level totals and warnings:

```text
running=1 ok=0 fail=0 retry=2
watch: submit_rejected_repeated=1 operation_not_allowed_for_phase=1
latest: solve/sessions/<session_id>/session.md
```

The `smart_eg_progress` event should therefore include:

```json
{
  "event": "smart_eg_progress",
  "session_id": "solve-smart-eg-financial-163297",
  "phase": "planning",
  "phase_attempt": 2,
  "phase_status": "running",
  "phase_turns_completed": 5,
  "max_phase_turns": 12,
  "global_turns_completed": 18,
  "max_global_turns": 80,
  "last_tool": "execute_probe",
  "last_operation": "candidate",
  "blocking_debt_count": 1,
  "top_debt_code": "plan_field_path_missing",
  "submit_rejections": 2,
  "revisits": 1,
  "max_revisits": 4,
  "tokens": 18420,
  "cost_usd": 0.031,
  "agent_session_ref": "solve/sessions/.../session.md",
  "phase_session_ref": "solve/sessions/.../phases/planning/attempts/002/agent.md",
  "latest_gate_ref": "solve/sessions/.../ledgers/gates.jsonl#7",
  "latest_error_ref": null
}
```

### Final Result Refs

Final result refs:

```text
agent_session_ref     -> session.md
transcript_refs       -> phase attempt agent.md files used by the final path
diagnostics_refs      -> attempt-local llm_calls.jsonl refs or sidecar refs
evidence_ledger_ref   -> ledgers/evidence.jsonl
execution_trace_ref   -> ledgers/execution.jsonl
submit_gate_refs      -> ledgers/gates.jsonl line refs
phase_session_refs    -> explicit phase/attempt map, preferably as a first-class result field
```

If adding `phase_session_refs` as a first-class result field is too disruptive in the first patch, store it in `disclosure` temporarily and migrate it later.

## Budgets and Convergence

Budgeting should have global and phase-local levels.

Global budget:

```text
max_turns
max_tokens
max_cost_usd
max_revisits
```

Phase budget:

```text
max_phase_turns
max_submit_rejections_per_phase
max_protocol_violations_per_phase
terminal_turn_window_per_phase
```

Rules:

- Provider first-token timeout, stream stall, transient 429, 5xx, connection interruption, and route failover stay below the agent loop and do not count as agent turns.
- Global counters remain in `SmartEGState.counters`.
- Phase-local counters decide terminal-only behavior for the active phase.
- Repeated protocol violations should not corrupt downstream phases.
- Repeated revisit signatures are rejected globally.
- Global budget exhaustion returns `SmartEGFailure(error_code="AGENT_ITERATION_LIMIT_EXHAUSTED")`.

## Red-Line Constraints

The implementation is not complete unless these constraints are enforced by tests:

1. SMART-EG solver inputs remain `NLQ + read-only MongoDB db_handle + db_id/record_id`.
2. Every phase uses provider-native tool calls, not structured JSON generation.
3. Only `submit_final` can produce `SmartEGPrediction`.
4. New phase `tools.json` files contain only canonical tool names.
5. Deprecated raw tool names do not appear in new phase prompts or phase `tools.json`.
6. Phase operation limits are enforced at runtime, not just described in prompts.
7. Later phases cannot directly repair upstream state using early exploration operations.
8. Evidence refs must exist and be milestone-relevant.
9. Final sanity evidence does not bypass field-path, value-grounding, output, or debt gates.
10. Typed failure must preserve session, phase attempt, diagnostics, evidence, gate, and execution refs.
11. Logs remain scoped and non-duplicative: session files navigate, phase attempts hold agent context, and centralized ledgers are the only sources of truth for evidence, gates, and execution.

## Test Matrix

Add or update tests to cover these behaviors.

```text
tests/test_smart_eg_canonical_tools.py
  - old tool shims map to canonical operations
  - inspect_database operations preserve old evidence semantics
  - validate_query never executes Mongo
  - execute_probe never submits milestones
  - manage_evidence never accesses Mongo

tests/test_smart_eg_phase_policy.py
  - Environment allowed tools and operations match exactly
  - Intent cannot list_collections/discover_paths/execute_probe
  - Planning cannot inspect broad database shape
  - Realization cannot profile_path_values/discover_paths
  - submit_milestone rejects wrong milestone for active phase
  - submit_final is unavailable outside Realization

tests/test_smart_eg_workflow_runner.py
  - happy path runs Environment -> Intent -> Planning -> Realization
  - locally repairable submit rejection stays in current phase
  - request_revisit(target="intent") reruns Intent -> Planning -> Realization
  - request_revisit(target="environment") reruns all downstream phases
  - repeated revisit signature is rejected
  - global budget exhaustion returns typed SmartEGFailure

tests/test_smart_eg_phase_observability.py
  - session.md and session.json exist
  - timeline.jsonl records only orchestration events
  - ledgers/evidence.jsonl, ledgers/gates.jsonl, and ledgers/execution.jsonl exist
  - each executed phase has index.md
  - each phase attempt has agent.md, events.jsonl, tools.json, and llm_calls.jsonl
  - final transcript_refs include the phase attempt agent.md files on the final path
  - submit_gate_refs include ledgers/gates.jsonl line refs for rejected and accepted gates
  - diagnostics_refs aggregate attempt-local llm_calls.jsonl refs or compatible sidecar refs
  - session.json latest_refs point to real files and line refs
  - terminal progress events include phase_attempt, phase_session_ref, latest_gate_ref, and top_debt_code

tests/test_smart_eg_boundary_contracts.py
  - release shim does not pass gold or private fields
  - phase prompts do not include gold MQL/canonical form/private schema
  - new phase tools.json files do not contain deprecated raw tool names
  - per-call diagnostics sidecars, if emitted, are named llm/<call_id>.json rather than duplicating phase/turn in filenames
```

Existing tests such as `tests/test_smart_eg_runtime.py`, `tests/test_smart_eg_observability.py`, and `tests/test_solver_observability.py` can be migrated into these groups instead of duplicating coverage.

## Live Smoke Validation

After unit tests pass, run a small SMART-EG solve against an available MongoDB-backed financial slice. The smoke is valid only if artifacts are inspected, not merely because the command exits.

Required checks:

```text
solve/sessions/<id>/session.md exists and shows current diagnosis or final outcome
solve/sessions/<id>/session.json latest_refs point to existing artifacts
timeline.jsonl contains phase_started/phase_accepted/revisit/final orchestration events
ledgers/evidence.jsonl contains canonical tool evidence records
ledgers/gates.jsonl contains environment/intent/plan/final milestone entries
ledgers/execution.jsonl contains validate_query and execute_probe records when used
phases/environment/index.md exists after environment runs
phases/*/attempts/*/agent.md contains the readable provider-native transcript
phases/*/attempts/*/tools.json contains only canonical tool names and phase operation limits
phases/*/attempts/*/llm_calls.jsonl contains one diagnostics record per provider call
terminal progress snapshot includes phase_attempt, top_debt_code, phase_session_ref, and latest_gate_ref
final prediction or typed failure preserves refs
```

If MongoDB is unavailable, fixture-level tests may prove contracts, but they do not prove solver quality.

## Ablations

The new runtime architecture should not make "four agents" the primary mechanism variable. Ablations should target mechanisms:

| Ablation | Disabled Mechanism | Purpose |
|---|---|---|
| `smart_eg_full` | none | Full phase workflow plus canonical tools |
| `smart_eg_no_evidence_debt` | blocking debt gate relaxed | Evidence Debt impact |
| `smart_eg_no_revisit` | `request_revisit` disabled | Revisit impact on schema-less errors |
| `smart_eg_no_execution_probe` | `execute_probe` disabled or restricted | Execution feedback impact |
| `smart_eg_no_value_grounding` | value/path grounding relaxed | Value grounding impact |
| `smart_eg_single_session_compat` | new workflow disabled | Migration comparison only |

`smart_eg_single_session_compat` should be temporary. It can be removed once the phase workflow is stable.

## Migration Plan

Implementation should be incremental:

1. Add canonical tool layer without changing public solver behavior.
   - Old raw tool names remain as shims.
   - Canonical tool tests establish equivalent behavior.
2. Add phase tool policy.
   - Define canonical tools and operation limits per phase.
   - Existing single loop may use this policy before the workflow split.
3. Add phase session classes and `SmartEGWorkflowRunner`.
   - Reuse `_complete_with_tools`, `SmartEGHistory`, policy, and observer patterns.
   - Keep `smart_solve_nlq_db_eg(...)` as the public entrypoint.
4. Extend observability to the session/phase/attempt layout.
   - Keep existing provenance fields compatible.
   - Add `session.md`, `session.json`, `timeline.jsonl`, centralized ledgers, phase indexes, and attempt-local agent transcripts.
   - Add `phase_session_refs` directly or in `disclosure`.
5. Update phase prompts and tests.
   - Prompts describe only the current phase.
   - Tests enforce canonical tool names and operation limits.
6. Stop exposing deprecated raw tool names to new phase agents.
   - Compatibility shims can remain internally.
   - Remove obsolete schemas only after ablation and runtime tests pass.

## Failure Handling

| Failure | Handling |
|---|---|
| Phase provider failure | `SmartEGFailure(PROVIDER_FAILURE)` with phase diagnostics refs |
| Phase max turns | terminal-only for the current phase; typed failure if no valid submit/revisit/abandon path |
| Repeated submit rejection | terminal-only with current submit, evidence inspection/linking, revisit, abandon |
| Wrong tool or operation | protocol violation; repeated violations trigger terminal-only |
| Revisit loop | repeated signature rejection; `max_revisits` exhaustion leads to terminal-only or typed failure |
| Final sanity ok but semantically empty output | final gate rejects missing-only grounding, raw complex objects, or all-unknown outputs |
| Deprecated raw tool leaked to phase prompt/tools.json | test failure |
| Missing provenance refs | test failure |

## Documentation Updates

After implementation, update:

```text
proposals/06_solution_design.md
README.md SMART solver section
docs/ARCHITECTURE.md solver and observability sections
proposals/_meta/CHANGELOG.md if the repository uses it for proposal changes
```

The documentation should describe SMART-EG as a phase-scoped provider-native ReAct workflow with shared deterministic gates and canonical orthogonal tools.
