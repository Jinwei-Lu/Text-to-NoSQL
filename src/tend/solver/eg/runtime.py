"""SMART-EG provider-native tool-call loop."""
from __future__ import annotations

import inspect
from typing import Any

from .contracts import SmartEGBudgets, SmartEGFailure, SmartEGState
from .history import SmartEGHistory
from .observability import SmartEGObserver
from .policy import SmartEGConvergenceChecker, SmartEGPolicy
from .tools import SmartEGToolAPI, tool_schemas
from ..inputs import _canonical_nlq

SYSTEM_PROMPT = (
    "You are SMART-EG, a provider-native ReAct solver for NLQ plus an interactive "
    "MongoDB database. Use only exposed tool calls, not natural-language final answers. "
    "Proceed in stages: first explore the database with list_collections and compact "
    "Mongo tools, then call submit_environment_model with candidate_collections and "
    "evidence_refs, then submit_intent_hypothesis, then submit_query_plan, then validate "
    "and finish with submit_final_mql. Successful completion is only submit_final_mql. "
    "Normal failure is only abandon_with_failure. read_documents and run_query are not "
    "available tools; use sample_documents, discover_paths, profile_path_values, "
    "run_readonly_probe, and the submit_* tools instead. Keep exploration bounded: prefer "
    "one or two targeted tool calls per turn, use evidence_id values returned by tools in "
    "evidence_refs, and stop exploring once the current stage has enough evidence."
)


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
    observer = SmartEGObserver(run_dir, session_id=session_id)
    state = SmartEGState(
        nlq=nlq,
        db_id=db_id,
        record_id=record_id,
        budgets=policy.budgets,
        session_id=observer.session_id,
    )
    history = SmartEGHistory(system_prompt=SYSTEM_PROMPT)
    history.add_user(f"NLQ: {nlq}\nDB: {db_id}\nRecord: {record_id}")
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

            exposed_tools = api.tools_for_state(state)
            exposed_tool_names = {tool["function"]["name"] for tool in exposed_tools}
            tool_choice = api.tool_choice_for_state(state)
            observer.agent_event(
                "turn_start",
                {
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
            observer.agent_event(
                "llm_request",
                {
                    "mode": state.mode,
                    "tools": [tool["function"]["name"] for tool in exposed_tools],
                    "tool_choice": tool_choice,
                },
            )
            try:
                response = await _complete_with_tools(
                    llm,
                    messages=history.build_messages(state.summary()),
                    tools=exposed_tools,
                    tool_choice=tool_choice,
                    agent="smart_eg",
                    stream=policy.stream,
                    first_token_timeout_s=policy.first_token_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 - return typed provider failure
                observer.record_error(
                    {
                        "error_code": "PROVIDER_FAILURE",
                        "message": str(exc)[:500],
                        "error_type": type(exc).__name__,
                    }
                )
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
                history.add_user(
                    "Protocol violation: call one exposed SMART-EG tool. "
                    "Natural-language answers cannot submit or fail the solver."
                )
                continue

            for call in tool_calls:
                observer.agent_event(
                    "tool_call",
                    {
                        "tool_call_id": call.get("id"),
                        "tool": (call.get("function") or {}).get("name"),
                    },
                )
                observation = api.execute(call, state, exposed_tool_names=exposed_tool_names)
                state.counters.tool_turns += 1
                history.add_tool_result(
                    observation.tool_call_id,
                    observation.name,
                    observation.llm_visible_content,
                )
                observer.agent_event(
                    "tool_observation",
                    {
                        "tool_call_id": observation.tool_call_id,
                        "tool": observation.name,
                        "ok": observation.ok,
                        "gate_ref": observation.gate_ref,
                    },
                )
                if state.terminal:
                    break

            if len(history.messages) > policy.budgets.history_max_messages:
                history.compact(
                    max_messages=policy.budgets.history_max_messages,
                    state_summary=state.summary(),
                )

        if state.result is None:
            state.result = _budget_failure(state, observer, "terminal_without_result")
        observer.record_execution_trace(
            {"event": "final_state", "state": state.summary(), "result_type": state.result.result_type}
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
        return {
            "role": "assistant",
            "content": getattr(result, "content", None),
            "tool_calls": [call_to_json(call) for call in result.tool_calls],
            "usage": getattr(result, "usage", {}),
            "cost": getattr(result, "cost", {}),
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
            "arguments": json_dumps(getattr(call, "arguments", {})),
        },
    }


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)


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
            "cost_source": cost.get("cost_source") or response.get("cost_source") or "unavailable",
        }
    )


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


def _tool_schemas(*, terminal_only: bool = False) -> list[dict[str, Any]]:
    return tool_schemas(terminal_only=terminal_only)


def _policy_from_options(
    options: dict[str, Any],
    *,
    max_tool_turns: int | None,
    max_revisits: int | None,
    cost_budget_usd: float | None,
) -> SmartEGPolicy:
    return SmartEGPolicy(
        max_tool_turns=int(max_tool_turns if max_tool_turns is not None else options.get("max_tool_turns", 48)),
        max_revisits=int(max_revisits if max_revisits is not None else options.get("max_revisits", 4)),
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
