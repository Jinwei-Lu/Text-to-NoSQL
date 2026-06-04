"""SMART-EG provider-native tool-call loop."""
from __future__ import annotations

from copy import deepcopy
import inspect
from typing import Any

from .contracts import SmartEGBudgets, SmartEGFailure, SmartEGState, ToolObservation
from .history import SmartEGHistory
from .observability import SmartEGObserver, build_session_id
from .policy import SmartEGConvergenceChecker, SmartEGPolicy
from .tools import SmartEGToolAPI, tool_schemas as _base_tool_schemas
from ..inputs import _canonical_nlq

SYSTEM_PROMPT = """# SMART-EG Solver

You solve one NLQ against one interactive MongoDB database using provider-native tool
calls. Do not answer in natural language. Every productive step is a tool call.

## Stage Contract

Stage order is mandatory.

1. Environment: inspect the database with `list_collections` and compact Mongo tools.
   Submit `submit_environment_model` only after you have observed candidate
   collections plus shape/path evidence. The submit requires `candidate_collections`
   and `evidence_refs`.
2. Intent: accepted environment is required before intent submit. Ground the NLQ in
   observed paths, observed value buckets, or relationship evidence, then call
   `submit_intent_hypothesis` with `evidence_refs`.
3. Planning: accepted intent is required before plan submit. Build one MongoDB
   aggregation plan from the grounded intent, then call `submit_query_plan`.
4. Execution: accepted query_plan is required before final submit. Validate the
   candidate query with read-only probes and boundary checks, then call
   `submit_final_mql`.

## Tool Boundary

- Only call tools exposed in the current turn. The exact exposed tools are the
  provider-native tools attached to this request; do not assume every SMART-EG tool
  is available in every stage.
- `read_documents` and `run_query` are not available tools.
- Use the exposed subset of these Batch 2 tool names:
  `list_collections`, `sample_documents`, `discover_paths`, `profile_path`,
  `profile_path_values`, `search_values`, `inspect_array_shape`,
  `inspect_dynamic_keys`, `profile_relationship_candidates`, `run_readonly_probe`,
  `add_evidence_claim`, `link_evidence`, `inspect_evidence_ledger`,
  `inspect_evidence_debt`, `mine_counterexamples`, `render_pipeline`,
  `check_ast_filter`, `run_final_sanity_execution`,
  `render_pipeline_prefix`, `execute_pipeline_prefix`, `check_prefix_checkpoint`,
  `submit_environment_model`, `submit_intent_hypothesis`, `submit_query_plan`,
  `submit_final_mql`, `request_revisit`, `request_mode_shift`,
  `abandon_with_failure`.
- Use `evidence_id` values returned by tools in later `evidence_refs`.
- If `profile_relationship_candidates` is exposed, treat it as the relationship
  probe. Use it before `$lookup` or relationship cardinality assumptions.
- If prefix tools are exposed, use them to render and execute bounded aggregation
  prefixes. Their observations are typed stage-local feedback for repairing a
  candidate before final submit.

## Evidence Grounding

- `evidence_refs` must cite typed evidence from observations. Typed evidence means
  source tool, observed collection/path/type information, observed value buckets,
  relationship candidates, counterexamples, and read-only probe outputs.
- Use typed value grounding before constants, enums, dates, ObjectId filters, regexes,
  or comparisons. Do not invent values that were not observed or profiled.
- Tool rejections return typed feedback such as `reason`, `error_code`,
  `required_tool`, `missing_evidence`, `debt_ids`, and `gate_ref`. Repair from those
  fields instead of repeating the same invalid call.

## Exit Contract

- Successful completion is only `submit_final_mql`.
- Normal failure is only `abandon_with_failure`.
- Production success requires final sanity execution. `submit_final_mql` runs the
  final sanity execution when enabled; if the final sanity observation is not ok,
  the solver must repair or use typed failure.
- Keep exploration bounded: prefer one or two targeted tool calls per turn and stop
  probing once the current stage has enough evidence.

## MongoDB Idioms

- Use MongoDB aggregation idioms: choose one base collection, provide `collection`
  plus `pipeline`, `$match` early, `$unwind` arrays before filtering nested array
  members, `$group` only for real aggregation, and `$project` final answer fields.
- Use `$lookup` only after proving relationship keys with evidence. Avoid SQL table,
  join, and column language in the final MQL.
- Use ObjectId and ISODate only when observed values prove those BSON/date types.

## Semantic Plan Fidelity

- Translate the NLQ into result semantics before choosing stages. Preserve requested
  grouping dimensions, comparison axes, context fields, row limits, and sort intent.
- Phrases such as share, ratio, rate, pool, context, concentrated, top, and compare
  usually imply derived metrics, counts, thresholds, sort order, or final projection
  fields. Encode those semantics explicitly instead of returning only raw paths.
- For dynamic-key objects, use `$objectToArray` and keep both key and value meaning:
  project the key as an answer dimension and compute counts from the value array.
- When the NLQ asks for context, include the scalar context fields and derived
  summary fields needed to make the comparison interpretable.
"""

_RUNTIME_TOOL_DESCRIPTION_OVERRIDES = {
    "list_collections": (
        "List the MongoDB collections available for the current database. This is "
        "environment typed evidence and should precede submit_environment_model."
    ),
    "sample_documents": (
        "Return a compact redacted shape summary for sampled documents. It produces "
        "typed evidence for paths and types, never raw rows."
    ),
    "discover_paths": (
        "Discover bounded document paths and value type counts for one collection. "
        "Use this typed evidence for environment, intent, and query_plan grounding."
    ),
    "profile_path": (
        "Profile presence, missing count, value count, and type counts for one path. "
        "Use the typed path evidence before filters or projections depend on that path."
    ),
    "profile_path_values": (
        "Profile hashed value buckets for one path as typed value grounding. Use this "
        "before constants, enum filters, ObjectId filters, ISODate filters, regexes, "
        "or comparisons."
    ),
    "search_values": (
        "Search sampled scalar values for a user-mentioned term and return redacted "
        "path matches. Use the typed value evidence to ground NLQ constants."
    ),
    "inspect_array_shape": (
        "Inspect array lengths, element types, and object subpaths at a path. Use this "
        "typed evidence before $unwind or nested array filtering."
    ),
    "inspect_dynamic_keys": (
        "Inspect dynamic object keys at a path with hashed key samples and value type "
        "counts. Use this typed evidence before dynamic-key query construction."
    ),
    "profile_relationship_candidates": (
        "Relationship probe, if exposed: find sampled _id and *_id relationship "
        "candidates across collections. Use before $lookup, relationship filters, or "
        "cardinality assumptions."
    ),
    "run_readonly_probe": (
        "Run a bounded read-only aggregate probe from MQL/mql or collection plus "
        "pipeline. Disabled operators are rejected and failures return typed feedback "
        "for repair."
    ),
    "add_evidence_claim": (
        "Record a typed evidence claim that must be linked to evidence ids before "
        "gated submission."
    ),
    "link_evidence": "Link an existing evidence id to a typed evidence claim id.",
    "inspect_evidence_ledger": (
        "Inspect typed evidence records, linked claims, and unresolved grounding debt."
    ),
    "inspect_evidence_debt": (
        "Inspect blocking evidence debt and suggested exposed tools for the next repair."
    ),
    "mine_counterexamples": (
        "Mine counterexample risks from the current plan and evidence, returning typed "
        "feedback that must be resolved or cited."
    ),
    "render_pipeline": (
        "Render collection plus pipeline into MQL for inspection. Rendering is not "
        "execution proof."
    ),
    "check_ast_filter": (
        "Check MQL against the read-only safety boundary and return typed feedback for "
        "disabled operators or parse errors."
    ),
    "run_final_sanity_execution": (
        "Run final sanity execution for the candidate MQL. Production success requires "
        "an ok final sanity execution before or during submit_final_mql."
    ),
    "render_pipeline_prefix": (
        "Render a bounded aggregation prefix for stage-local inspection. Rendering is "
        "not execution proof; use execute_pipeline_prefix or check_prefix_checkpoint "
        "for execution feedback."
    ),
    "execute_pipeline_prefix": (
        "Execute a bounded aggregation prefix and return typed stage-local feedback. "
        "Use it to localize row collapse, missing target fields, or operator errors."
    ),
    "check_prefix_checkpoint": (
        "Check a bounded aggregation prefix against expected checkpoint behavior and "
        "return typed repair feedback before final submit."
    ),
    "submit_environment_model": (
        "Submit the accepted environment model after collection and shape/path "
        "exploration. Requires candidate_collections and evidence_refs."
    ),
    "submit_intent_hypothesis": (
        "Submit the grounded NLQ intent after accepted environment evidence. Requires "
        "evidence_refs that support task kind, target collection, fields, filters, or "
        "aggregations."
    ),
    "submit_query_plan": (
        "Submit a MongoDB aggregation plan after accepted intent evidence. Requires "
        "collection, stages, and evidence_refs that support the query_plan."
    ),
    "submit_final_mql": (
        "Submit the final MQL. This is the only successful solver exit, requires "
        "accepted environment, intent, and query_plan plus evidence_refs, and "
        "production success requires final sanity execution to be ok."
    ),
    "request_revisit": (
        "Request a controlled revisit when typed feedback shows an accepted earlier "
        "milestone is stale."
    ),
    "request_mode_shift": (
        "Request a mode shift only when typed feedback requires repair in another stage."
    ),
    "abandon_with_failure": (
        "Terminate with a typed failure when no valid query can be produced. Use an "
        "allowed error_code and concise message."
    ),
}

SUBMIT_FOCUS_HISTORY_MAX_MESSAGES = 2
RECENT_EVIDENCE_RECORD_LIMIT = 12
SUBMIT_TOOLS = {
    "submit_environment_model",
    "submit_intent_hypothesis",
    "submit_query_plan",
    "submit_final_mql",
}
SUBMIT_FOCUS_ALLOWED_TOOLS = SUBMIT_TOOLS | {
    "abandon_with_failure",
    "inspect_evidence_debt",
    "inspect_evidence_ledger",
}


async def smart_solve_nlq_db_eg(
    wf: Any | None = None,
    *,
    llm: Any | None = None,
    db_handle: Any | None = None,
    db_id: str,
    nlq: str,
    run_dir: Any | None = None,
    session_id: str | None = None,
    executor: Any = None,
    policy: SmartEGPolicy | None = None,
    record_id: str | int | None = None,
) -> Any:
    """Solve from NLQ + DB using a provider-native SMART-EG tool loop.

    The direct dependency-injected form is used by tests and small validations. A ``wf``
    positional shim is accepted for future integration with the existing Workflow object.
    """

    ctx = None
    if wf is not None:
        ctx = wf.ctx
        llm = llm or getattr(ctx, "llm", None)
        db_handle = db_handle or getattr(ctx, "mongo", None)
        mongo = getattr(ctx, "mongo", None)
        executor = executor if executor is not None else mongo
        run_dir = run_dir or getattr(getattr(ctx, "log", None), "run_dir", None)
    if llm is None:
        raise TypeError("smart_solve_nlq_db_eg requires llm or wf.ctx.llm")
    if db_handle is None:
        raise TypeError("smart_solve_nlq_db_eg requires db_handle or wf.ctx.mongo")
    if run_dir is None:
        raise TypeError("smart_solve_nlq_db_eg requires run_dir or wf.ctx.log.run_dir")

    policy = policy or SmartEGPolicy()
    resolved_session_id = session_id or build_session_id(
        stage="solve",
        task="smart_eg",
        db_id=db_id,
        record_id=record_id,
    )
    observer = SmartEGObserver(
        run_dir,
        session_id=resolved_session_id,
        run_logger=getattr(ctx, "log", None) if ctx is not None else None,
    )
    session_logger = _session_logger(
        ctx,
        observer,
        db_id=db_id,
        record_id=record_id,
    )
    state = SmartEGState(
        nlq=nlq,
        db_id=db_id,
        record_id=record_id,
        budgets=policy.budgets,
        session_id=observer.session_id,
    )
    initial_user_message = _initial_user_message(nlq=nlq, db_id=db_id, record_id=record_id)
    system_prompt = _system_prompt_for_policy(policy)
    history = SmartEGHistory(system_prompt=system_prompt)
    history.add_user(initial_user_message)
    observer.start_session(
        stage="solve",
        task="smart_eg",
        model=_model_name(ctx),
        db_id=db_id,
        record_id=record_id,
        system_prompt=system_prompt,
        user_message=initial_user_message,
        tools=_tool_schemas(policy=policy),
        max_turns=policy.budgets.max_tool_turns,
    )
    if session_logger is not None and hasattr(session_logger, "open_agent_session"):
        session_logger.open_agent_session(
            stage="solve",
            task="smart_eg",
            session_id=observer.session_id,
            model=_model_name(ctx),
            system_prompt=system_prompt,
            user_message=initial_user_message,
            tools=_tool_schemas(policy=policy),
            session_ref=observer.agent_ref(),
            write_header=False,
        )
    api = SmartEGToolAPI(policy, observer=observer, db_handle=db_handle, executor=executor)
    checker = SmartEGConvergenceChecker(policy)

    try:
        while not state.terminal:
            convergence = checker.check(state)
            if convergence.hard_stop:
                state.result = _budget_failure(state, observer, convergence.reason or "budget")
                state.terminal = True
                state.terminal_reason = convergence.reason
                break
            if convergence.terminal_only:
                state.terminal_only = True
                state.terminal_reason = convergence.reason

            tool_choice = api.tool_choice_for_state(state)
            submit_focus_tool = _submit_focus_tool(tool_choice)
            exposed_tools = _document_runtime_tool_schemas(api.tools_for_state(state))
            if submit_focus_tool is not None:
                narrowed_tools = _narrow_tools_to_required_submit(exposed_tools, submit_focus_tool)
                if len(narrowed_tools) != len(exposed_tools):
                    observer.agent_event(
                        "tools_narrowed_to_required_submit",
                        {
                            "required_next_tool": submit_focus_tool,
                            "before": [tool["function"]["name"] for tool in exposed_tools],
                            "after": [tool["function"]["name"] for tool in narrowed_tools],
                        },
                    )
                exposed_tools = narrowed_tools
            exposed_tool_names = {tool["function"]["name"] for tool in exposed_tools}
            turn_index = state.counters.llm_turns + 1
            observer.set_current_turn(turn_index)
            observer.agent_event(
                "turn_start",
                {
                    "turn_index": turn_index,
                    "mode": state.mode,
                    "terminal_only": state.terminal_only,
                    "tool_turn": state.counters.tool_turns,
                    "debt_count": len(state.evidence_ledger.blocking_debts()),
                },
            )
            observer.record_progress(
                {
                    "db_id": state.db_id,
                    "mode": state.mode,
                    "tool_turn": state.counters.tool_turns,
                    "evidence_debt_count": len(state.evidence_ledger.blocking_debts()),
                    "revisit_budget": state.budgets.max_revisits - state.counters.revisits,
                    "provider_wait_s": 0.0,
                    "cost": state.counters.cost_usd,
                    "tokens": state.counters.tokens,
                }
            )
            if submit_focus_tool not in exposed_tool_names:
                submit_focus_tool = None
            submit_focus_tool = submit_focus_tool or _single_exposed_submit_tool(exposed_tool_names)
            if submit_focus_tool is not None and history.compact(
                max_messages=SUBMIT_FOCUS_HISTORY_MAX_MESSAGES,
                state_summary=_submit_focus_summary(state, submit_focus_tool),
            ):
                observer.agent_event(
                    "history_compacted",
                    {
                        "turn_index": turn_index,
                        "mode": state.mode,
                        "reason": "submit_focus",
                        "required_next_tool": submit_focus_tool,
                        "message_count": len(history.messages),
                    },
                )
            provider_messages = _messages_for_turn(
                history,
                state,
                required_next_tool=submit_focus_tool,
            )
            observer.agent_event(
                "llm_request",
                {
                    "turn_index": turn_index,
                    "mode": state.mode,
                    "tools": [tool["function"]["name"] for tool in exposed_tools],
                    "tool_schemas": exposed_tools,
                    "tool_choice": tool_choice,
                    "messages": provider_messages,
                },
            )
            try:
                request_kwargs: dict[str, Any] = {
                    "messages": provider_messages,
                    "tools": exposed_tools,
                    "tool_choice": tool_choice,
                    "agent": "smart_eg",
                    "stream": policy.stream,
                    "first_token_timeout_s": policy.first_token_timeout_s,
                }
                if session_logger is not None:
                    request_kwargs["logger"] = session_logger
                response = await _complete_with_tools(llm, **request_kwargs)
            except Exception as exc:  # noqa: BLE001 - return typed provider failure
                observer.record_error(
                    {
                        "error_code": "PROVIDER_FAILURE",
                        "message": str(exc)[:500],
                        "error_type": type(exc).__name__,
                    }
                )
                observer.consume_error_refs()
                state.result = SmartEGFailure(
                    result_type="solver_failure",
                    db_id=state.db_id,
                    record_id=state.record_id,
                    nlq=state.nlq,
                    error_code="PROVIDER_FAILURE",
                    message=str(exc)[:500],
                    last_candidate_ref=None,
                    unresolved_debts=[
                        debt.debt_id for debt in state.evidence_ledger.blocking_debts()
                    ],
                    evidence_ledger_ref=observer.evidence_ref(),
                    execution_trace_ref=observer.execution_trace_ref(),
                    agent_session_ref=observer.agent_ref(),
                )
                state.terminal = True
                state.terminal_reason = "provider_failure"
                break

            _record_usage(response, state, observer)
            state.counters.llm_turns += 1
            observer.agent_event(
                "llm_response",
                {
                    "turn_index": turn_index,
                    "call_id": response.get("call_id"),
                    "model": response.get("model"),
                    "transcript_ref": response.get("transcript_ref"),
                    "diagnostics_ref": response.get("diagnostics_ref"),
                    "finish_reason": response.get("finish_reason"),
                    "latency_s": response.get("latency_s"),
                    "usage": response.get("usage"),
                    "cost": response.get("cost"),
                    "cumulative_tokens": state.counters.tokens,
                    "cumulative_cost_usd": state.counters.cost_usd,
                    "content": response.get("content") or response.get("response_text"),
                    "assistant_message": response.get("assistant_message"),
                    "tool_calls": list(response.get("tool_calls") or []),
                    "has_tool_calls": bool(response.get("tool_calls")),
                    "tool_call_count": len(response.get("tool_calls") or []),
                },
            )
            assistant = _assistant_message(response)
            history.add_assistant(assistant)
            tool_calls = list(assistant.get("tool_calls") or [])
            if not tool_calls:
                state.counters.protocol_violations += 1
                observer.record_error(
                    {
                        "error_code": "PROTOCOL_INVALID",
                        "message": "assistant response did not include a tool call",
                        "mode": state.mode,
                    }
                )
                observer.consume_error_refs()
                history.add_user(
                    "Protocol violation: call one exposed SMART-EG tool. "
                    "Natural-language answers cannot submit or fail the solver."
                )
                continue

            for call in tool_calls:
                observer.agent_event(
                    "tool_call",
                    {
                        "turn_index": turn_index,
                        "tool_call_id": call.get("id"),
                        "tool": (call.get("function") or {}).get("name"),
                        "raw_tool_call": call,
                        "arguments": _tool_arguments(call),
                    },
                )
                observation = api.execute(call, state, exposed_tool_names=exposed_tool_names)
                error_refs = observer.consume_error_refs()
                if error_refs:
                    observation.result = {**observation.result, "error_refs": error_refs}
                    observation.llm_visible_content = {
                        **observation.llm_visible_content,
                        "error_refs": error_refs,
                    }
                if _counts_against_tool_budget(observation):
                    state.counters.tool_turns += 1
                history.add_tool_result(
                    observation.tool_call_id,
                    observation.name,
                    observation.llm_visible_content,
                )
                observation_ref = observer.agent_event(
                    "tool_observation",
                    {
                        "turn_index": turn_index,
                        "tool_call_id": observation.tool_call_id,
                        "tool": observation.name,
                        "ok": observation.ok,
                        "gate_ref": observation.gate_ref,
                        "error_refs": error_refs or None,
                        "content": observation.llm_visible_content,
                    },
                )
                _record_observation_evidence(state, observer, observation, observation_ref)
                if state.terminal:
                    break

            if len(history.messages) > policy.budgets.history_max_messages:
                history.compact(
                    max_messages=policy.budgets.history_max_messages,
                    state_summary=state.summary(),
                )

        observer.set_current_turn(None)
        if state.result is None:
            state.result = _budget_failure(state, observer, "terminal_without_result")
        _normalize_result_refs(state.result, observer)
        observer.record_execution_trace(
            {
                "event": "final_state",
                "state": state.summary(),
                "result_type": state.result.result_type,
            }
        )
        observer.agent_event(
            "final_outcome",
            {
                "result_type": state.result.result_type,
                "terminal_reason": state.terminal_reason,
            },
        )
        observer.finalize_markdown(
            final_status=state.result.result_type,
            state_summary=state.summary(),
        )
        if session_logger is not None and hasattr(session_logger, "close_agent_session"):
            session_logger.close_agent_session(
                turns=state.counters.llm_turns,
                tool_calls_made=state.counters.tool_turns,
                total_tokens=state.counters.tokens,
                total_cost=state.counters.cost_usd,
                total_cost_source="api" if state.counters.cost_usd else "unavailable",
                completed=state.result.result_type != "solver_failure",
                reason=state.terminal_reason,
                outcome=state.result.result_type,
                write_footer=False,
            )
        return state.result
    finally:
        observer.close()


async def smart_solve_record_eg(
    wf: Any,
    record: dict[str, Any],
    schema: dict[str, Any] | None = None,
    *,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    policy: SmartEGPolicy | None = None,
    max_tool_turns: int | None = None,
    max_revisits: int | None = None,
    cost_budget_usd: float | None = None,
    witness_preloaded: bool = False,
    options: dict[str, Any] | None = None,
) -> Any:
    """Release-record shim that passes only NLQ plus Mongo access into SMART-EG."""
    del schema
    db_id = str(record.get("db_id") or "")
    if local_data and not witness_preloaded and getattr(wf.ctx, "mongo", None) is not None:
        load_witness = getattr(wf.ctx.mongo, "load_witness", None)
        if callable(load_witness):
            load_witness(db_id, local_data)
    if policy is None:
        policy = _policy_from_options(
            options or {},
            max_tool_turns=max_tool_turns,
            max_revisits=max_revisits,
            cost_budget_usd=cost_budget_usd,
        )
    return await smart_solve_nlq_db_eg(
        wf,
        db_id=db_id,
        nlq=_canonical_nlq(record),
        record_id=record.get("record_id"),
        policy=policy,
    )


def _session_logger(
    ctx: Any | None,
    observer: SmartEGObserver,
    *,
    db_id: str,
    record_id: str | int | None,
) -> Any | None:
    logger = getattr(ctx, "log", None) if ctx is not None else None
    bind = getattr(logger, "bind", None)
    if not callable(bind):
        return None
    fields: dict[str, Any] = {
        "agent_session_ref": observer.agent_ref(),
        "session_id": observer.session_id,
        "db_id": db_id,
        "record_id": record_id,
    }
    extra = getattr(ctx, "extra", None) if ctx is not None else None
    if isinstance(extra, dict):
        for key in ("ablation_id", "batch_index", "work_item_id"):
            value = extra.get(key)
            if value is not None:
                fields[key] = value
        solver_options = extra.get("solver_options")
        if isinstance(solver_options, dict):
            solver_variant = solver_options.get("solver_variant")
            if solver_variant is not None:
                fields["solver_variant"] = solver_variant
        solver_variant = extra.get("solver_variant")
        if solver_variant is not None:
            fields["solver_variant"] = solver_variant
    return bind(**fields)


def _normalize_result_refs(result: Any, observer: SmartEGObserver) -> None:
    for attr, value in (
        ("agent_session_ref", observer.agent_ref()),
        ("evidence_ledger_ref", observer.evidence_ref()),
        ("execution_trace_ref", observer.execution_trace_ref()),
        ("transcript_refs", observer.transcript_refs()),
        ("diagnostics_refs", observer.diagnostics_refs()),
        ("error_refs", observer.error_refs()),
    ):
        if hasattr(result, attr):
            setattr(result, attr, value)


async def _complete_with_tools(llm: Any, **kwargs: Any) -> dict[str, Any]:
    if hasattr(llm, "complete_with_tools"):
        result = llm.complete_with_tools(**kwargs)
    elif callable(llm):
        result = llm(**kwargs)
    else:
        raise TypeError("llm must expose complete_with_tools or be callable")
    if inspect.isawaitable(result):
        result = await result
    if hasattr(result, "assistant_message") and hasattr(result, "tool_calls"):
        assistant = getattr(result, "assistant_message", {}) or {}
        content = assistant.get("content") if isinstance(assistant, dict) else None
        if content is None:
            content = getattr(result, "text", "")
        return {
            "role": "assistant",
            "content": content,
            "assistant_message": assistant if isinstance(assistant, dict) else {},
            "tool_calls": [call_to_json(call) for call in result.tool_calls],
            "usage": getattr(result, "usage", {}),
            "cost": getattr(result, "cost", {}),
            "call_id": getattr(result, "call_id", None),
            "model": getattr(result, "model", None),
            "finish_reason": getattr(result, "finish_reason", None),
            "latency_s": getattr(result, "latency_s", None),
            "attempts": getattr(result, "attempts", None),
            "transcript_ref": getattr(result, "transcript_ref", None),
            "diagnostics_ref": getattr(result, "diagnostics_ref", None),
            "response_text": getattr(result, "text", ""),
            "provider_metadata": getattr(result, "provider_metadata", {}),
        }
    if not isinstance(result, dict):
        raise TypeError("complete_with_tools must return an assistant message dict")
    return result


def call_to_json(call: Any) -> dict[str, Any]:
    if isinstance(call, dict):
        return call
    return {
        "id": str(getattr(call, "id", "")),
        "type": "function",
        "function": {
            "name": str(getattr(call, "name", "")),
            "arguments": str(
                getattr(call, "raw_arguments", "")
                or json_dumps(getattr(call, "arguments", {}))
            ),
        },
        "parsed_arguments": getattr(call, "arguments", {}),
    }


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


def _initial_user_message(*, nlq: str, db_id: str, record_id: str | int | None) -> str:
    record = "not provided" if record_id is None else str(record_id)
    return f"Task input:\nNLQ: {nlq}\nDatabase: {db_id}\nRecord ID: {record}"


def _submit_focus_tool(tool_choice: Any) -> str | None:
    if not isinstance(tool_choice, dict):
        return None
    function = tool_choice.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if name in SUBMIT_TOOLS:
        return str(name)
    return None


def _narrow_tools_to_required_submit(
    tools: list[dict[str, Any]],
    required_tool: str,
) -> list[dict[str, Any]]:
    narrowed = [
        tool for tool in tools
        if isinstance(tool.get("function"), dict)
        and tool["function"].get("name") == required_tool
    ]
    return narrowed or tools


def _single_exposed_submit_tool(exposed_tool_names: set[str]) -> str | None:
    if not exposed_tool_names.issubset(SUBMIT_FOCUS_ALLOWED_TOOLS):
        return None
    submit_names = sorted(name for name in exposed_tool_names if name in SUBMIT_TOOLS)
    if len(submit_names) == 1:
        return submit_names[0]
    return None


def _submit_focus_summary(state: Any, required_tool: str) -> dict[str, Any]:
    ledger = state.evidence_ledger
    return {
        "instruction": (
            "The current milestone has enough evidence. The next assistant message "
            "must call required_next_tool. Do not call exploration, probe, or "
            "inspection tools unless required_next_tool is one of those tools."
        ),
        "required_next_tool": required_tool,
        "nlq": state.nlq,
        "db_id": state.db_id,
        "record_id": state.record_id,
        "mode": state.mode,
        "terminal_only": state.terminal_only,
        "terminal_reason": state.terminal_reason,
        "stale_milestones": sorted(state.stale_milestones),
        "environment": state.environment,
        "intent": state.intent,
        "query_plan": state.query_plan,
        "blocking_debts": [_jsonable(debt) for debt in ledger.blocking_debts()],
        "evidence_summary": ledger.summary(),
        "recent_evidence": _recent_evidence(ledger),
    }


def _recent_evidence(ledger: Any) -> list[dict[str, Any]]:
    records = list(getattr(ledger, "records", {}).values())[-RECENT_EVIDENCE_RECORD_LIMIT:]
    return [
        {
            "evidence_id": getattr(record, "evidence_id", None),
            "source_tool": getattr(record, "source_tool", None),
            "summary": _compact_evidence_summary(getattr(record, "summary", None)),
        }
        for record in records
    ]


def _compact_evidence_summary(summary: Any) -> Any:
    if not isinstance(summary, dict):
        return summary
    compact: dict[str, Any] = {}
    for key in (
        "tool",
        "db_id",
        "collection",
        "path",
        "sample_count",
        "path_count",
        "returned_path_count",
        "omitted_path_count",
        "result_count",
        "count",
        "limit",
        "ok",
        "error_type",
    ):
        if key in summary:
            compact[key] = summary[key]
    for key in ("top_level_keys", "array_paths", "object_paths"):
        value = summary.get(key)
        if isinstance(value, list):
            compact[key] = value[:12]
    type_counts = summary.get("top_level_type_counts")
    if isinstance(type_counts, dict):
        compact["top_level_type_counts"] = {
            str(key): value for key, value in list(type_counts.items())[:12]
        }
    values = summary.get("values") or summary.get("value_samples")
    if isinstance(values, list):
        compact["value_samples"] = values[:12]
    if not compact:
        compact["summary_keys"] = sorted(str(key) for key in summary)[:20]
    return compact


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_json"):
        return value.to_json()
    if isinstance(value, dict):
        return value
    return str(value)


def _model_name(ctx: Any | None) -> str | None:
    settings = getattr(ctx, "settings", None)
    llm_settings = getattr(settings, "llm", None)
    if llm_settings is None:
        return None
    model_for = getattr(llm_settings, "model_for", None)
    if callable(model_for):
        return str(model_for("smart_eg"))
    model = getattr(llm_settings, "model", None)
    return str(model) if model is not None else None


def _tool_arguments(call: dict[str, Any]) -> Any:
    import json

    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = function.get("arguments") or call.get("arguments") or {}
    if isinstance(raw, str):
        try:
            return json.loads(raw or "{}")
        except json.JSONDecodeError:
            return raw
    return raw


def _messages_for_turn(
    history: SmartEGHistory,
    state: Any,
    *,
    required_next_tool: str | None,
) -> list[dict[str, Any]]:
    if required_next_tool is None:
        return history.build_messages()
    base = history.build_messages()
    system = [message for message in base[:1] if message.get("role") == "system"]
    return [
        *system,
        {
            "role": "user",
            "content": "Submit-ready context:\n"
            + json_dumps(_submit_focus_summary(state, required_next_tool)),
        },
    ]


def _assistant_message(response: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.get("content"),
        "tool_calls": list(response.get("tool_calls") or []),
    }


def _record_usage(response: dict[str, Any], state: SmartEGState, observer: SmartEGObserver) -> None:
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    cost = response.get("cost") if isinstance(response.get("cost"), dict) else {}
    total_tokens = int(usage.get("total_tokens") or 0)
    cost_usd = float(cost.get("cost_usd") or response.get("cost_usd") or 0.0)
    state.counters.tokens += total_tokens
    state.counters.cost_usd += cost_usd
    observer.record_cost(
        {
            "provider": response.get("provider") or "unknown",
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": total_tokens,
            "cost_usd": cost_usd,
            "cost_source": (
                cost.get("cost_source")
                or cost.get("source")
                or response.get("cost_source")
                or "unavailable"
            ),
        }
    )


def _record_observation_evidence(
    state: SmartEGState,
    observer: SmartEGObserver,
    observation: ToolObservation,
    observation_ref: str,
) -> None:
    for evidence_id in observation.evidence_ids:
        record = state.evidence_ledger.records.get(evidence_id)
        if record is None:
            continue
        record.observation_ref = observation_ref
        observer.record_evidence(record.to_json())


def _counts_against_tool_budget(observation: ToolObservation) -> bool:
    reason = observation.llm_visible_content.get("reason")
    return reason not in {
        "environment_ready_to_submit",
        "intent_ready_to_submit",
        "planning_ready_to_submit",
        "execution_ready_to_submit",
        "terminal_only",
    }


def _budget_failure(
    state: SmartEGState,
    observer: SmartEGObserver,
    reason: str,
) -> SmartEGFailure:
    state.refresh_debt_queue()
    return SmartEGFailure(
        result_type="solver_failure",
        db_id=state.db_id,
        record_id=state.record_id,
        nlq=state.nlq,
        error_code="TOOL_BUDGET_EXHAUSTED",
        message=f"SMART-EG stopped at runtime limit: {reason}",
        last_candidate_ref=None,
        unresolved_debts=[debt.debt_id for debt in state.debt_queue],
        evidence_ledger_ref=observer.evidence_ref(),
        execution_trace_ref=observer.execution_trace_ref(),
        agent_session_ref=observer.agent_ref(),
    )


def default_budgets() -> SmartEGBudgets:
    return SmartEGBudgets()


def _system_prompt_for_policy(policy: SmartEGPolicy) -> str:
    if policy.value_grounding:
        return SYSTEM_PROMPT
    return SYSTEM_PROMPT.replace(
        "  `list_collections`, `sample_documents`, `discover_paths`, `profile_path`,\n"
        "  `profile_path_values`, `search_values`, `inspect_array_shape`,\n",
        "  `list_collections`, `sample_documents`, `discover_paths`, `profile_path`,\n"
        "  `inspect_array_shape`,\n",
    ).replace(
        "- Use typed value grounding before constants, enums, dates, ObjectId filters, regexes,\n"
        "  or comparisons. Do not invent values that were not observed or profiled.",
        "- Value-grounding probes are disabled for this run. Do not call value-grounding "
        "tools; rely on non-value structural evidence and typed execution feedback.",
    )


def _tool_schemas(
    *,
    terminal_only: bool = False,
    policy: SmartEGPolicy | None = None,
) -> list[dict[str, Any]]:
    schemas = _base_tool_schemas(terminal_only=terminal_only)
    if policy is not None:
        names = _policy_tool_schema_names(policy)
        schemas = [
            tool
            for tool in schemas
            if isinstance(tool.get("function"), dict)
            and tool["function"].get("name") in names
        ]
    return _document_runtime_tool_schemas(schemas)


def _policy_tool_schema_names(policy: SmartEGPolicy) -> set[str]:
    names = {tool["function"]["name"] for tool in _base_tool_schemas()}
    if not policy.value_grounding:
        names.difference_update({"profile_path_values", "search_values"})
    if not policy.relationship_probe:
        names.discard("profile_relationship_candidates")
    if not policy.enable_counterexamples:
        names.discard("mine_counterexamples")
    if not policy.prefix_execution:
        names.difference_update(
            {"render_pipeline_prefix", "execute_pipeline_prefix", "check_prefix_checkpoint"}
        )
    if not policy.revisit:
        names.discard("request_revisit")
    return names


def _document_runtime_tool_schemas(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    documented = deepcopy(tools)
    for tool in documented:
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name in _RUNTIME_TOOL_DESCRIPTION_OVERRIDES:
            function["description"] = _RUNTIME_TOOL_DESCRIPTION_OVERRIDES[name]
        parameters = function.get("parameters")
        if not isinstance(parameters, dict):
            continue
        properties = parameters.get("properties")
        if not isinstance(properties, dict):
            continue
        evidence_refs = properties.get("evidence_refs")
        if isinstance(evidence_refs, dict):
            evidence_refs["description"] = (
                "Evidence ids returned by prior observation tools. Cite typed "
                "path/value/relationship/probe evidence that supports this milestone."
            )
        pipeline = properties.get("pipeline")
        if isinstance(pipeline, dict):
            pipeline["description"] = (
                "MongoDB aggregation pipeline stages. Use MongoDB idioms and keep "
                "the pipeline read-only."
            )
        mql = properties.get("MQL")
        if isinstance(mql, dict):
            mql["description"] = (
                "Full MongoDB MQL aggregate expression. Use only when it matches "
                "collection plus pipeline."
            )
        lower_mql = properties.get("mql")
        if isinstance(lower_mql, dict):
            lower_mql["description"] = (
                mql.get("description")
                if isinstance(mql, dict)
                else "Full MongoDB MQL aggregate expression."
            )
    return documented


def _policy_from_options(
    options: dict[str, Any],
    *,
    max_tool_turns: int | None,
    max_revisits: int | None,
    cost_budget_usd: float | None,
) -> SmartEGPolicy:
    return SmartEGPolicy(
        max_tool_turns=int(
            max_tool_turns
            if max_tool_turns is not None
            else options.get("max_tool_turns", 48)
        ),
        max_revisits=int(
            max_revisits if max_revisits is not None else options.get("max_revisits", 4)
        ),
        cost_budget_usd=(
            float(cost_budget_usd)
            if cost_budget_usd is not None
            else (
                float(options["cost_budget_usd"])
                if options.get("cost_budget_usd") is not None
                else None
            )
        ),
        evidence_gate=bool(options.get("use_evidence_gate", True)),
        counterexample_gate=bool(options.get("use_counterexample", True)),
        value_grounding=bool(options.get("use_value_grounding", True)),
        relationship_probe=bool(options.get("use_relationship_probe", True)),
        prefix_execution=bool(options.get("use_prefix_execution", True)),
        revisit=bool(options.get("use_revisit", True)),
        probe_scheduler=bool(options.get("use_probe_scheduler", True)),
    )
