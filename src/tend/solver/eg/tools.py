"""Provider-native tool exposure and deterministic gates for SMART-EG."""
from __future__ import annotations

import json
from typing import Any

from ..per_stage import CheckpointSpec, render_prefix_mql, run_per_stage_check
from .contracts import (
    GateViolation,
    Milestone,
    QueryCandidate,
    SmartEGFailure,
    SmartEGPrediction,
    SmartEGState,
    SubmitGateResult,
    ToolObservation,
    downstream_milestones,
    mode_for_milestone,
)
from .counterexamples import mine_counterexamples
from .evidence import EvidenceDebt
from .execution import check_ast_filter, parse_or_render_mql, run_final_sanity_execution
from .mongo_tools import SmartEGMongoTools
from .observability import SmartEGObserver
from .policy import SmartEGPolicy
from .safety import parse_path, stable_hash, value_proof, value_token

ENVIRONMENT_TOOLS = {
    "list_collections",
    "sample_documents",
    "discover_paths",
    "profile_path",
    "profile_path_values",
    "search_values",
    "inspect_array_shape",
    "inspect_dynamic_keys",
    "profile_relationship_candidates",
    "run_readonly_probe",
}
EVIDENCE_TOOLS = {
    "add_evidence_claim",
    "link_evidence",
    "inspect_evidence_ledger",
    "inspect_evidence_debt",
    "mine_counterexamples",
}
EXECUTION_TOOLS = {
    "render_pipeline",
    "render_pipeline_prefix",
    "execute_pipeline_prefix",
    "check_prefix_checkpoint",
    "check_ast_filter",
    "run_final_sanity_execution",
}
PREFIX_EXECUTION_TOOLS = {
    "render_pipeline_prefix",
    "execute_pipeline_prefix",
    "check_prefix_checkpoint",
}
TERMINAL_TOOLS = {"submit_final_mql", "abandon_with_failure"}
STAGE_CONTROL_TOOLS = {
    "submit_environment_model",
    "submit_intent_hypothesis",
    "submit_query_plan",
    "request_revisit",
    "request_mode_shift",
}
FAILURE_CODES = {
    "INSUFFICIENT_EVIDENCE",
    "TOOL_BUDGET_EXHAUSTED",
    "PROVIDER_FAILURE",
    "BOUNDARY_REJECTED",
    "EXECUTION_UNRESOLVED",
    "NO_VALID_QUERY_FOUND",
}


class SmartEGToolAPI:
    def __init__(
        self,
        policy: SmartEGPolicy,
        *,
        observer: SmartEGObserver | None = None,
        db_handle: Any = None,
        executor: Any = None,
    ) -> None:
        self.policy = policy
        self.observer = observer
        self.db_handle = db_handle
        self.executor = executor

    def tools_for_state(self, state: SmartEGState) -> list[dict[str, Any]]:
        environment_tools = _environment_tool_names(self.policy)
        evidence_tools = _evidence_tool_names(self.policy)
        execution_tools = _execution_tool_names(self.policy)
        control_tools = _stage_control_tool_names(self.policy)
        if state.terminal_only:
            return [_schema(name) for name in sorted(_terminal_tool_names_for_state(state, self.policy))]
        if state.mode == "environment":
            if _environment_ready_for_submit(state, self.policy):
                names = {
                    "submit_environment_model",
                    "inspect_evidence_ledger",
                    "inspect_evidence_debt",
                    "abandon_with_failure",
                }
            else:
                names = environment_tools | evidence_tools | {
                    "submit_environment_model",
                    "request_mode_shift",
                    "abandon_with_failure",
                }
        elif state.mode == "intent":
            if _intent_ready_for_submit(state, self.policy):
                names = {
                    "submit_intent_hypothesis",
                    "inspect_evidence_ledger",
                    "inspect_evidence_debt",
                    "abandon_with_failure",
                }
            else:
                names = environment_tools | evidence_tools | {
                    "submit_intent_hypothesis",
                    "request_mode_shift",
                    "abandon_with_failure",
                } | (control_tools & {"request_revisit"})
        elif state.mode == "planning":
            if _planning_ready_for_submit(state, self.policy):
                names = {
                    "submit_query_plan",
                    "inspect_evidence_ledger",
                    "inspect_evidence_debt",
                    "abandon_with_failure",
                }
            else:
                names = environment_tools | evidence_tools | {
                    "render_pipeline",
                    "check_ast_filter",
                    "submit_query_plan",
                    "abandon_with_failure",
                } | (control_tools & {"request_revisit"})
        else:
            if _execution_ready_for_submit(state, self.policy):
                names = {
                    "submit_final_mql",
                    "inspect_evidence_ledger",
                    "inspect_evidence_debt",
                    "abandon_with_failure",
                }
            else:
                names = environment_tools | evidence_tools | execution_tools | {
                    "submit_final_mql",
                    "request_mode_shift",
                    "abandon_with_failure",
                } | (control_tools & {"request_revisit"})
        return [_schema(name) for name in sorted(names)]

    def tool_choice_for_state(self, state: SmartEGState) -> dict[str, Any] | str | None:
        if state.terminal_only:
            if _terminal_has_repair_tools(state, self.policy):
                return None
            if _ready_for_mode_submit(state, self.policy):
                return {"type": "function", "function": {"name": _terminal_submit_tool_for_mode(state.mode)}}
            return {"type": "function", "function": {"name": "abandon_with_failure"}}
        if state.mode == "environment" and _environment_ready_for_submit(state, self.policy):
            return {"type": "function", "function": {"name": "submit_environment_model"}}
        if _intent_ready_for_submit(state, self.policy):
            return {"type": "function", "function": {"name": "submit_intent_hypothesis"}}
        if _planning_ready_for_submit(state, self.policy):
            return {"type": "function", "function": {"name": "submit_query_plan"}}
        if _execution_ready_for_submit(state, self.policy):
            return {"type": "function", "function": {"name": "submit_final_mql"}}
        if not self.policy.force_tool_choice:
            return None
        return "required"

    def execute(
        self,
        tool_call: dict[str, Any],
        state: SmartEGState,
        *,
        exposed_tool_names: set[str] | None = None,
    ) -> ToolObservation:
        function = tool_call.get("function") or {}
        name = str(function.get("name") or tool_call.get("name") or "")
        args = _parse_args(function.get("arguments"))
        call_id = str(tool_call.get("id") or "")
        exposed = (
            {tool["function"]["name"] for tool in self.tools_for_state(state)}
            if exposed_tool_names is None
            else set(exposed_tool_names)
        )
        known_tools = _known_tool_names()
        if name not in exposed:
            if (
                state.mode == "environment"
                and _environment_ready_for_submit(state, self.policy)
                and name in known_tools
                and name in ENVIRONMENT_TOOLS
            ):
                return _observation(
                    name,
                    call_id,
                    False,
                    {
                        "reason": "environment_ready_to_submit",
                        "required_tool": "submit_environment_model",
                        "message": (
                            "The environment model has enough bounded evidence. "
                            "Do not run more environment probes; call submit_environment_model next."
                        ),
                    },
                )
            if state.terminal_only and name in known_tools:
                allowed = _terminal_tool_names_for_state(state, self.policy)
                return _observation(
                    name,
                    call_id,
                    False,
                    {
                        "reason": "terminal_only",
                        "allowed_tools": sorted(allowed),
                        "message": (
                            "The runtime is in terminal-only mode; call the current stage submit "
                            "tool or abandon_with_failure."
                        ),
                    },
                )
            if state.mode == "intent" and _intent_ready_for_submit(state, self.policy) and name in known_tools:
                return _observation(
                    name,
                    call_id,
                    False,
                    {
                        "reason": "intent_ready_to_submit",
                        "required_tool": "submit_intent_hypothesis",
                        "message": (
                            "Intent has enough bounded evidence and no blocking debt. "
                            "Do not run more probes; call submit_intent_hypothesis next."
                        ),
                    },
                )
            if state.mode == "planning" and _planning_ready_for_submit(state, self.policy) and name in known_tools:
                return _observation(
                    name,
                    call_id,
                    False,
                    {
                        "reason": "planning_ready_to_submit",
                        "required_tool": "submit_query_plan",
                        "message": (
                            "Planning has enough bounded evidence and no blocking debt. "
                            "Do not run more probes; call submit_query_plan next."
                        ),
                    },
                )
            if state.mode == "execution" and _execution_ready_for_submit(state, self.policy) and name in known_tools:
                return _observation(
                    name,
                    call_id,
                    False,
                    {
                        "reason": "execution_ready_to_submit",
                        "required_tool": "submit_final_mql",
                        "message": (
                            "Execution has an accepted query plan and no blocking debt. "
                            "Do not run more probes; call submit_final_mql next."
                        ),
                    },
                )
            state.counters.protocol_violations += 1
            if self.observer:
                self.observer.record_error(
                    {"error_code": "PROTOCOL_INVALID", "message": "tool not exposed", "tool": name}
                )
            return _observation(name or "unknown_tool", call_id, False, {"reason": "tool_not_exposed"})

        if name == "list_collections":
            return self._list_collections(call_id, state)
        if name in ENVIRONMENT_TOOLS and name not in _environment_tool_names(self.policy):
            return _observation(name, call_id, False, {"reason": "tool_not_exposed"})
        if name in ENVIRONMENT_TOOLS:
            return self._mongo_observation(name, call_id, args, state)
        if name == "add_evidence_claim":
            return self._add_evidence_claim(call_id, args, state)
        if name == "link_evidence":
            claim_id = str(args.get("claim_id") or "")
            evidence_id = str(args.get("evidence_id") or "")
            linked = state.evidence_ledger.link_evidence(claim_id, evidence_id)
            state.refresh_debt_queue()
            if not linked:
                return _observation(
                    name,
                    call_id,
                    False,
                    {
                        "reason": "invalid_evidence_ref",
                        "claim_id": claim_id,
                        "evidence_id": evidence_id,
                        "debt_count": len(state.debt_queue),
                    },
                )
            return _observation(name, call_id, True, {"debt_count": len(state.debt_queue)})
        if name == "inspect_evidence_ledger":
            return _observation(name, call_id, True, state.evidence_ledger.summary())
        if name == "inspect_evidence_debt":
            state.refresh_debt_queue()
            return _observation(name, call_id, True, {"debts": [d.to_json() for d in state.debt_queue]})
        if name == "mine_counterexamples" and not self.policy.enable_counterexamples:
            return _observation(name, call_id, False, {"reason": "tool_not_exposed"})
        if name == "mine_counterexamples":
            hits = mine_counterexamples(
                plan=args.get("plan") or state.query_plan,
                final_candidate=args.get("final_candidate"),
                ledger=state.evidence_ledger,
            )
            return _observation(name, call_id, True, {"hits": [h.to_json() for h in hits]})
        if name == "request_revisit" and not self.policy.revisit:
            return _observation(name, call_id, False, {"reason": "tool_not_exposed"})
        if name == "request_revisit":
            return self._request_revisit(call_id, args, state)
        if name == "request_mode_shift":
            return self._request_mode_shift(call_id, args, state)
        if name == "submit_environment_model":
            return self._submit_environment(call_id, args.get("model") or args, state)
        if name == "submit_intent_hypothesis":
            return self._submit_intent(call_id, args.get("intent") or args, state)
        if name == "submit_query_plan":
            return self._submit_plan(call_id, args.get("plan") or args, state)
        if name == "submit_final_mql":
            return self._submit_final(call_id, args, state)
        if name == "abandon_with_failure":
            return self._abandon(call_id, args, state)
        if name == "render_pipeline":
            return self._render_pipeline(call_id, args)
        if name in PREFIX_EXECUTION_TOOLS:
            if not self.policy.prefix_execution:
                return _observation(name, call_id, False, {"reason": "tool_not_exposed"})
            return self._prefix_tool(name, call_id, args, state)
        if name == "check_ast_filter":
            try:
                _, _, mql = parse_or_render_mql(
                    collection=args.get("collection"),
                    pipeline=args.get("pipeline"),
                    mql=args.get("MQL") or args.get("mql"),
                )
                result = check_ast_filter(mql)
            except Exception as exc:  # noqa: BLE001 - tool feedback, not solver failure
                return _observation(
                    name,
                    call_id,
                    False,
                    {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
                )
            return _observation(name, call_id, bool(result["ok"]), result)
        if name == "run_final_sanity_execution":
            collection = ""
            pipeline: list[dict[str, Any]] = []
            mql = ""
            try:
                collection, pipeline, mql = parse_or_render_mql(
                    collection=args.get("collection"),
                    pipeline=args.get("pipeline"),
                    mql=args.get("MQL") or args.get("mql"),
                )
                result = run_final_sanity_execution(
                    executor=self.executor,
                    db_id=state.db_id,
                    mql=mql,
                )
            except Exception as exc:  # noqa: BLE001 - tool feedback, not solver failure
                result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
            state.execution_trace.final_sanity_runs.append(result)
            if self.observer:
                self.observer.record_execution_trace({"event": "final_sanity", **result})
            evidence_ids: list[str] = []
            if result.get("ok"):
                summary = {
                    "tool": "run_final_sanity_execution",
                    "db_id": state.db_id,
                    "collection": collection or result.get("collection"),
                    "stage_count": len(pipeline) if pipeline else result.get("stage_count"),
                    "mql_hash": _mql_hash(mql),
                    **result,
                }
                record = state.evidence_ledger.add_record(
                    source_tool="run_final_sanity_execution",
                    tool_call_id=call_id,
                    observation_ref="",
                    summary=summary,
                    supports_claims=[],
                    redaction={"raw_rows": False, "sample_preview": "bounded"},
                    binding=_pipeline_binding(
                        collection=collection or str(result.get("collection") or ""),
                        pipeline=pipeline,
                        milestone="final",
                        mql=mql,
                    ),
                )
                result = {**summary, "evidence_id": record.evidence_id}
                evidence_ids.append(record.evidence_id)
            return _observation(name, call_id, bool(result["ok"]), result, evidence_ids)
        return _observation(
            name or "unknown_tool",
            call_id,
            False,
            {"reason": "unknown_tool", "implemented": False},
        )

    def _list_collections(self, call_id: str, state: SmartEGState) -> ToolObservation:
        raw: Any = []
        error: dict[str, Any] | None = None
        if self.db_handle is not None:
            try:
                if hasattr(self.db_handle, "list_collections"):
                    try:
                        raw = self.db_handle.list_collections(state.db_id)
                    except TypeError:
                        try:
                            raw = self.db_handle.list_collections({})
                        except TypeError:
                            raw = self.db_handle.list_collections()
                elif hasattr(self.db_handle, "list_collection_names"):
                    raw = self.db_handle.list_collection_names()
            except Exception as exc:  # noqa: BLE001 - expose bounded tool feedback
                error = {"error_type": type(exc).__name__, "message": str(exc)[:300]}
                raw = []
        if error is not None:
            if self.observer:
                self.observer.record_error(
                    {
                        "error_code": "TOOL_EXECUTION_FAILED",
                        "tool": "list_collections",
                        **error,
                    }
                )
            return _observation(
                "list_collections",
                call_id,
                False,
                {"reason": "tool_execution_failed", "error": error},
            )
        if isinstance(raw, dict):
            raw = raw.get("collections") or []
        collections = [
            str(item.get("collection") if isinstance(item, dict) else item)
            for item in raw
            if item
        ]
        summary: dict[str, Any] = {"collections": collections}
        record = state.evidence_ledger.add_record(
            source_tool="list_collections",
            tool_call_id=call_id,
            observation_ref="",
            summary=summary,
            supports_claims=[],
            redaction={"raw_rows": False},
            binding={"milestone": "environment"},
        )
        return _observation(
            "list_collections",
            call_id,
            True,
            {**summary, "evidence_id": record.evidence_id},
            [record.evidence_id],
        )

    def _mongo_observation(
        self,
        name: str,
        call_id: str,
        args: dict[str, Any],
        state: SmartEGState,
    ) -> ToolObservation:
        if self.db_handle is None:
            return _observation(name, call_id, False, {"reason": "mongo_unavailable"})
        try:
            tools = SmartEGMongoTools(self.db_handle, state.db_id)
            summary = _dispatch_mongo_tool(tools, name, args)
        except Exception as exc:  # noqa: BLE001 - tool errors are feedback to the agent
            if self.observer:
                self.observer.record_error(
                    {
                        "error_code": "TOOL_EXECUTION_FAILED",
                        "tool": name,
                        "message": str(exc)[:500],
                        "error_type": type(exc).__name__,
                    }
                )
            return _observation(
                name,
                call_id,
                False,
                {"reason": "tool_execution_failed", "message": str(exc)[:500]},
            )
        record = state.evidence_ledger.add_record(
            source_tool=name,
            tool_call_id=call_id,
            observation_ref="",
            summary=summary,
            supports_claims=[],
            redaction=summary.get("redaction", {"raw_rows": False}),
            binding=_evidence_binding(
                source_tool=name,
                args=args,
                summary=summary,
                milestone=_milestone_for_mode(state.mode),
            ),
        )
        return _observation(
            name,
            call_id,
            True,
            {"observation": summary, "evidence_id": record.evidence_id},
            [record.evidence_id],
        )

    def _add_evidence_claim(
        self,
        call_id: str,
        args: dict[str, Any],
        state: SmartEGState,
    ) -> ToolObservation:
        claim = state.evidence_ledger.add_claim(
            claim_type=str(args.get("claim_type") or "field_grounding"),
            statement=str(args.get("statement") or ""),
            required_evidence=[str(item) for item in args.get("required_evidence") or []],
            evidence_refs=[str(item) for item in args.get("evidence_refs") or []],
            used_by=[str(item) for item in args.get("used_by") or [state.mode]],
            status=args.get("status"),
        )
        for ref in list(claim.evidence_refs):
            state.evidence_ledger.link_evidence(claim.claim_id, ref)
        state.refresh_debt_queue()
        return _observation("add_evidence_claim", call_id, True, {"claim": claim.to_json()})

    def _submit_environment(self, call_id: str, model: Any, state: SmartEGState) -> ToolObservation:
        violations: list[GateViolation] = []
        debts: list[EvidenceDebt] = []
        refs = _refs(model)
        if not isinstance(model, dict) or not model.get("candidate_collections"):
            violations.append(GateViolation("contract_invalid", "environment needs candidate_collections"))
        if self.policy.evidence_gate:
            binding = {"milestone": "environment"}
            ref_violations, ref_debts = _evidence_ref_gate(
                state,
                refs,
                "environment",
                claim_type="evidence_refs_missing",
                missing_evidence=["list_collections"],
                binding=binding,
            )
            violations.extend(ref_violations)
            debts.extend(ref_debts)
        gate = _gate("submit_environment_model", "environment", not violations, violations, debts)
        if gate.accepted:
            state.environment = dict(model)
            state.mode = "intent"
            _resolve_debts(state, "environment", refs=refs, binding={"milestone": "environment"})
        return self._submit_observation("submit_environment_model", call_id, gate, state)

    def _submit_intent(self, call_id: str, intent: Any, state: SmartEGState) -> ToolObservation:
        violations: list[GateViolation] = []
        debts: list[EvidenceDebt] = []
        refs = _refs(intent)
        if state.environment is None or "environment" in state.stale_milestones:
            violations.append(GateViolation("stale_environment", "intent requires accepted environment"))
        if not isinstance(intent, dict) or not intent.get("task_kind"):
            violations.append(GateViolation("contract_invalid", "intent needs task_kind"))
        intent_binding: dict[str, Any] = {"milestone": "intent"}
        if isinstance(intent, dict):
            target_collection = str(intent.get("target_collection") or "").strip()
            if not target_collection:
                violations.append(
                    GateViolation("contract_invalid", "intent needs a meaningful target_collection")
                )
            else:
                intent_binding["collection"] = target_collection
                accepted_collections = _accepted_collections(state)
                if accepted_collections and target_collection not in accepted_collections:
                    violations.append(
                        GateViolation(
                            "target_collection_unaccepted",
                            "intent target_collection must match accepted environment/evidence collections",
                            {
                                "target_collection": target_collection,
                                "accepted_collections": sorted(accepted_collections),
                            },
                        )
                    )
            if not _has_meaningful_intent_contract(intent):
                violations.append(
                    GateViolation(
                        "contract_invalid",
                        "intent needs at least one target field, filter, aggregation, or output contract",
                    )
                )
        if self.policy.evidence_gate:
            ref_violations, ref_debts = _evidence_ref_gate(
                state,
                refs,
                "intent",
                claim_type="intent_evidence_missing",
                missing_evidence=["profile_path_values"],
                binding=intent_binding,
            )
            violations.extend(ref_violations)
            debts.extend(ref_debts)
        gate = _gate("submit_intent_hypothesis", "intent", not violations, violations, debts)
        if gate.accepted:
            state.intent = dict(intent)
            state.mode = "planning"
            _resolve_debts(state, "intent", refs=refs, binding=intent_binding)
        return self._submit_observation("submit_intent_hypothesis", call_id, gate, state)

    def _submit_plan(self, call_id: str, plan: Any, state: SmartEGState) -> ToolObservation:
        violations: list[GateViolation] = []
        debts: list[EvidenceDebt] = []
        challenged: list[str] = []
        refs = _refs(plan)
        if state.intent is None or "intent" in state.stale_milestones:
            violations.append(GateViolation("stale_intent", "plan requires accepted intent"))
        if not isinstance(plan, dict) or not plan.get("collection") or not plan.get("stages"):
            violations.append(GateViolation("contract_invalid", "plan needs collection and stages"))
        plan_binding = (
            _pipeline_binding(
                collection=str(plan.get("collection") or ""),
                pipeline=list(plan.get("stages") or []),
                milestone="plan",
            )
            if isinstance(plan, dict)
            else {"milestone": "plan"}
        )
        if self.policy.evidence_gate:
            ref_violations, ref_debts = _evidence_ref_gate(
                state,
                refs,
                "plan",
                claim_type="plan_evidence_missing",
                missing_evidence=["discover_paths"],
                binding=plan_binding,
            )
            violations.extend(ref_violations)
            debts.extend(ref_debts)
        if self.policy.enable_counterexamples and isinstance(plan, dict):
            for hit in mine_counterexamples(plan=plan, ledger=state.evidence_ledger):
                challenged.extend(hit.challenged_claims)
                debts.append(state.evidence_ledger.ensure_debt(
                    milestone="plan",
                    claim_type=hit.code,
                    missing_evidence=hit.suggested_tools,
                    binding=plan_binding,
                ))
                violations.append(GateViolation(hit.code, hit.message, hit.context))
        if self.policy.value_grounding and isinstance(plan, dict):
            grounding_violations, grounding_debts = _value_grounding_gate(
                state,
                refs,
                plan.get("stages") or [],
                "plan",
            )
            violations.extend(grounding_violations)
            debts.extend(grounding_debts)
        if isinstance(plan, dict):
            output_violations, output_debts = _output_contract_gate(
                state,
                refs,
                plan.get("stages") or [],
                "plan",
            )
            violations.extend(output_violations)
            debts.extend(output_debts)
            field_violations, field_debts = _field_path_contract_gate(
                state,
                refs,
                plan.get("stages") or [],
                "plan",
            )
            violations.extend(field_violations)
            debts.extend(field_debts)
        gate = _gate("submit_query_plan", "plan", not violations, violations, debts, challenged, candidate_id="plan")
        if gate.accepted:
            state.query_plan = dict(plan)
            state.mode = "execution"
            _resolve_debts(state, "plan", refs=refs, binding=plan_binding)
            _resolve_milestone_debts(state, "plan")
        return self._submit_observation("submit_query_plan", call_id, gate, state)

    def _submit_final(self, call_id: str, args: dict[str, Any], state: SmartEGState) -> ToolObservation:
        violations: list[GateViolation] = []
        debts: list[EvidenceDebt] = []
        refs = _refs(args)
        candidate_id = str(args.get("candidate_id") or "cand-final")
        collection = ""
        pipeline: list[dict[str, Any]] = []
        mql = ""
        final_binding: dict[str, Any] = {"milestone": "final", "candidate_id": candidate_id}
        if state.mode != "execution":
            violations.append(GateViolation("wrong_mode", "final submit requires execution mode"))
        missing_milestones = [
            name
            for name, value in (
                ("environment", state.environment),
                ("intent", state.intent),
                ("plan", state.query_plan),
            )
            if value is None
        ]
        if missing_milestones:
            violations.append(
                GateViolation(
                    "missing_milestone",
                    "final requires accepted environment, intent, and query_plan",
                    {"missing": missing_milestones},
                )
            )
        stale = sorted({"environment", "intent", "plan", "final"} & state.stale_milestones)
        if stale:
            violations.append(
                GateViolation("stale_milestone", "final requires fresh milestones", {"stale": stale})
            )
        try:
            collection, pipeline, mql = parse_or_render_mql(
                collection=args.get("collection"),
                pipeline=args.get("pipeline"),
                mql=args.get("MQL") or args.get("mql"),
            )
            final_binding = _pipeline_binding(
                collection=collection,
                pipeline=pipeline,
                milestone="final",
                mql=mql,
                candidate_id=candidate_id,
            )
            ast = check_ast_filter(mql)
            if not ast["ok"]:
                violations.append(GateViolation("boundary_rejected", "disallowed operators", ast))
            if self.policy.evidence_gate:
                blocking_debts = state.evidence_ledger.blocking_debts()
                if blocking_debts:
                    violations.append(
                        GateViolation(
                            "blocking_evidence_debt",
                            "final requires no blocking evidence debts",
                            {"debt_ids": [debt.debt_id for debt in blocking_debts]},
                        )
                    )
                missing_refs = [ref for ref in refs if ref not in state.evidence_ledger.records]
                if missing_refs:
                    violations.append(
                        GateViolation(
                            "invalid_evidence_refs",
                            "final submit cited evidence refs that do not exist",
                            {"missing_refs": missing_refs},
                        )
                    )
                elif _cites_final_execution_evidence(state, refs):
                    execution_violations, execution_debts = _final_execution_evidence_gate(
                        state,
                        refs,
                        final_binding,
                    )
                    violations.extend(execution_violations)
                    debts.extend(execution_debts)
            if self.policy.value_grounding:
                grounding_violations, grounding_debts = _value_grounding_gate(
                    state,
                    refs,
                    pipeline,
                    "final",
                )
                violations.extend(grounding_violations)
                debts.extend(grounding_debts)
            output_violations, output_debts = _output_contract_gate(
                state,
                refs,
                pipeline,
                "final",
            )
            violations.extend(output_violations)
            debts.extend(output_debts)
            field_violations, field_debts = _field_path_contract_gate(
                state,
                refs,
                pipeline,
                "final",
            )
            violations.extend(field_violations)
            debts.extend(field_debts)
            if self.policy.enable_final_sanity_execution and not violations:
                sanity = run_final_sanity_execution(executor=self.executor, db_id=state.db_id, mql=mql)
                state.execution_trace.final_sanity_runs.append(sanity)
                if self.observer:
                    self.observer.record_execution_trace({"event": "final_sanity", **sanity})
                if not sanity.get("ok"):
                    violations.append(GateViolation("execution_unresolved", "final sanity failed", sanity))
        except Exception as exc:  # noqa: BLE001
            violations.append(GateViolation("boundary_rejected", str(exc), {"error_type": type(exc).__name__}))
        gate = _gate(
            "submit_final_mql",
            "final",
            not violations,
            violations,
            debts,
            candidate_id=candidate_id,
            accepted_action="finalized",
        )
        if gate.accepted:
            _resolve_debts(state, "final", refs=refs, binding=final_binding)
            state.candidates.append(QueryCandidate(candidate_id, collection, pipeline, mql, refs))
            state.best_candidate_id = candidate_id
            state.terminal = True
            state.terminal_reason = "submit_final_mql"
            state.result = SmartEGPrediction(
                result_type="solver_prediction",
                db_id=state.db_id,
                record_id=state.record_id,
                nlq=state.nlq,
                collection=collection,
                pipeline=pipeline,
                MQL=mql,
                disclosure={"solver": "smart-eg", "policy": self.policy.to_json()},
                environment_model_ref=self._state_ref("environment"),
                intent_ref=self._state_ref("intent"),
                query_plan_ref=self._state_ref("query_plan"),
                execution_trace_ref="execution_trace.jsonl",
                evidence_ledger_ref="evidence_ledger.jsonl",
                agent_session_ref=self.observer.agent_ref() if self.observer else "",
                submit_gate_refs=list(state.submit_gate_refs),
            )
        return self._submit_observation("submit_final_mql", call_id, gate, state)

    def _abandon(self, call_id: str, args: dict[str, Any], state: SmartEGState) -> ToolObservation:
        code = str(args.get("error_code") or "NO_VALID_QUERY_FOUND")
        if code not in FAILURE_CODES:
            code = "NO_VALID_QUERY_FOUND"
        state.refresh_debt_queue()
        state.terminal = True
        state.terminal_reason = "abandon_with_failure"
        state.result = SmartEGFailure(
            result_type="solver_failure",
            db_id=state.db_id,
            record_id=state.record_id,
            nlq=state.nlq,
            error_code=code,  # type: ignore[arg-type]
            message=str(args.get("message") or "SMART-EG abandoned with failure."),
            last_candidate_ref=None,
            unresolved_debts=[debt.debt_id for debt in state.debt_queue],
            evidence_ledger_ref="evidence_ledger.jsonl",
            execution_trace_ref="execution_trace.jsonl",
            agent_session_ref=self.observer.agent_ref() if self.observer else "",
        )
        return _observation("abandon_with_failure", call_id, True, {"accepted": True, "error_code": code})

    def _request_revisit(self, call_id: str, args: dict[str, Any], state: SmartEGState) -> ToolObservation:
        target = _milestone(args.get("target_milestone"))
        reason = " ".join(str(args.get("reason") or "").lower().split())
        claims = sorted(str(item) for item in args.get("challenged_claims") or [])
        debts = sorted(str(item) for item in args.get("debt_ids") or [])
        signature = "|".join([target, reason, ",".join(claims), ",".join(debts)])
        if signature in state.revisit_signatures:
            return _observation("request_revisit", call_id, False, {"accepted": False, "reason": "repeated_revisit_signature"})
        if state.counters.revisits >= state.budgets.max_revisits:
            state.terminal_only = True
            return _observation("request_revisit", call_id, False, {"accepted": False, "reason": "revisit_budget_exhausted"})
        state.revisit_signatures.add(signature)
        state.counters.revisits += 1
        state.evidence_ledger.challenge_claims(claims)
        state.stale_milestones.update(downstream_milestones(target))  # type: ignore[arg-type]
        state.mode = mode_for_milestone(target)  # type: ignore[arg-type]
        state.refresh_debt_queue()
        if self.observer:
            self.observer.agent_event("revisit_requested", {"target_milestone": target, "debt_ids": debts})
        return _observation("request_revisit", call_id, True, {"accepted": True, "stale_milestones": sorted(state.stale_milestones)})

    def _request_mode_shift(self, call_id: str, args: dict[str, Any], state: SmartEGState) -> ToolObservation:
        target = str(args.get("target_mode") or args.get("mode") or state.mode)
        if target not in {"environment", "intent", "planning", "execution", "repair"}:
            return _observation("request_mode_shift", call_id, False, {"accepted": False, "reason": "unknown_mode"})
        if _mode_rank(target) < _mode_rank(state.mode):
            if not self.policy.revisit:
                return _observation(
                    "request_mode_shift",
                    call_id,
                    False,
                    {"accepted": False, "reason": "no_revisit"},
                )
            if state.counters.revisits >= state.budgets.max_revisits:
                state.terminal_only = True
                return _observation(
                    "request_mode_shift",
                    call_id,
                    False,
                    {"accepted": False, "reason": "revisit_budget_exhausted"},
                )
            state.counters.revisits += 1
            milestone = _milestone_for_mode(target)
            state.stale_milestones.update(downstream_milestones(milestone))
        state.mode = target  # type: ignore[assignment]
        state.refresh_debt_queue()
        return _observation("request_mode_shift", call_id, True, {"accepted": True, "mode": target})

    def _render_pipeline(self, call_id: str, args: dict[str, Any]) -> ToolObservation:
        try:
            collection, pipeline, mql = parse_or_render_mql(
                collection=args.get("collection"),
                pipeline=args.get("pipeline"),
                mql=args.get("MQL") or args.get("mql"),
            )
        except Exception as exc:  # noqa: BLE001
            return _observation("render_pipeline", call_id, False, {"error": str(exc)})
        return _observation("render_pipeline", call_id, True, {"collection": collection, "pipeline": pipeline, "MQL": mql})

    def _prefix_tool(
        self,
        name: str,
        call_id: str,
        args: dict[str, Any],
        state: SmartEGState,
    ) -> ToolObservation:
        try:
            collection, pipeline, _mql = parse_or_render_mql(
                collection=args.get("collection"),
                pipeline=args.get("pipeline"),
                mql=args.get("MQL") or args.get("mql"),
            )
            prefix_length = _prefix_length(args, len(pipeline))
            prefix_pipeline = pipeline[:prefix_length]
            prefix_mql = render_prefix_mql(collection, prefix_pipeline)
        except Exception as exc:  # noqa: BLE001 - tool feedback, not solver failure
            return _observation(
                name,
                call_id,
                False,
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            )
        render_payload = {
            "ok": True,
            "collection": collection,
            "stage_count": len(pipeline),
            "prefix_length": prefix_length,
            "pipeline": prefix_pipeline,
            "MQL": prefix_mql,
            "mql_hash": _mql_hash(prefix_mql),
        }
        if name == "render_pipeline_prefix":
            return _observation(name, call_id, True, render_payload)
        if self.executor is None or not hasattr(self.executor, "execute_prefix"):
            return _observation(
                name,
                call_id,
                False,
                {
                    "ok": False,
                    "reason": "unsupported_prefix_executor",
                    "message": "prefix execution requires an executor with execute_prefix",
                },
            )
        checkpoint = CheckpointSpec(
            target_fields=tuple(str(item) for item in args.get("target_fields") or ()),
            required_fields_by_stage={
                int(key): tuple(str(item) for item in value)
                for key, value in dict(args.get("required_fields_by_stage") or {}).items()
                if isinstance(value, list)
            },
            collapse_to_zero=bool(args.get("collapse_to_zero", False)),
        )
        try:
            result = run_per_stage_check(
                db_id=state.db_id,
                mql=prefix_mql,
                executor=self.executor,
                checkpoint=checkpoint if name == "check_prefix_checkpoint" else None,
            )
        except Exception as exc:  # noqa: BLE001
            return _observation(
                name,
                call_id,
                False,
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            )
        payload = {
            **render_payload,
            "ok": result.ok,
            "prefixes_executed": result.prefixes_executed,
            "feedback": result.feedback.to_log_context() if result.feedback else None,
        }
        if name == "execute_pipeline_prefix":
            state.execution_trace.prefix_runs.append(payload)
        else:
            state.execution_trace.checkpoint_results.append(payload)
        evidence_ids: list[str] = []
        if result.ok:
            record = state.evidence_ledger.add_record(
                source_tool=name,
                tool_call_id=call_id,
                observation_ref="",
                summary={"tool": name, "db_id": state.db_id, **payload},
                supports_claims=[],
                redaction={"raw_rows": False},
                binding=_pipeline_binding(
                    collection=collection,
                    pipeline=prefix_pipeline,
                    milestone=_milestone_for_mode(state.mode),
                    mql=prefix_mql,
                ),
            )
            payload["evidence_id"] = record.evidence_id
            evidence_ids.append(record.evidence_id)
        return _observation(name, call_id, bool(result.ok), payload, evidence_ids)

    def _submit_observation(self, name: str, call_id: str, gate: SubmitGateResult, state: SmartEGState) -> ToolObservation:
        if not gate.accepted:
            state.counters.submit_rejections += 1
            state.last_submit_rejection_evidence_count = len(state.evidence_ledger.records)
        else:
            state.last_submit_rejection_evidence_count = 0
        state.refresh_debt_queue()
        payload = gate.to_json()
        gate_ref = self.observer.record_submit_gate(payload) if self.observer else None
        if gate_ref:
            state.submit_gate_refs.append(gate_ref)
            if isinstance(state.result, SmartEGPrediction):
                state.result.submit_gate_refs = list(state.submit_gate_refs)
        payload["gate_ref"] = gate_ref
        if self.observer:
            self.observer.agent_event("submit_attempt", {"submit_tool": name, "accepted": gate.accepted})
        return _observation(name, call_id, gate.accepted, payload, gate_ref=gate_ref)

    def _state_ref(self, label: str) -> str:
        if not self.observer:
            return label
        return f"{self.observer.agent_jsonl_ref()}#{label}"


def tool_schemas(*, terminal_only: bool = False) -> list[dict[str, Any]]:
    if terminal_only:
        return [_schema(name) for name in ["submit_final_mql", "abandon_with_failure"]]
    return [
        _schema(name)
        for name in sorted(
            ENVIRONMENT_TOOLS
            | EVIDENCE_TOOLS
            | EXECUTION_TOOLS
            | TERMINAL_TOOLS
            | STAGE_CONTROL_TOOLS
        )
    ]


def _terminal_tool_names_for_state(state: SmartEGState, policy: SmartEGPolicy) -> set[str]:
    names = {"abandon_with_failure"}
    if _ready_for_mode_submit(state, policy):
        names.add(_terminal_submit_tool_for_mode(state.mode))
    debts = state.evidence_ledger.blocking_debts()
    if debts:
        names.update({"inspect_evidence_debt", "inspect_evidence_ledger"})
        for debt in debts:
            names.update(tool for tool in debt.suggested_tools if tool in _known_tool_names(policy))
    return names


def _terminal_has_repair_tools(state: SmartEGState, policy: SmartEGPolicy) -> bool:
    return bool(
        _terminal_tool_names_for_state(state, policy)
        - {_terminal_submit_tool_for_mode(state.mode), "abandon_with_failure"}
    )


def _ready_for_mode_submit(state: SmartEGState, policy: SmartEGPolicy) -> bool:
    if state.mode == "environment":
        return _environment_ready_for_submit(state, policy)
    if state.mode == "intent":
        return _intent_ready_for_submit(state, policy)
    if state.mode == "planning":
        return _planning_ready_for_submit(state, policy)
    if state.mode == "execution":
        return _execution_ready_for_submit(state, policy)
    return False


def _terminal_submit_tool_for_mode(mode: str) -> str:
    if mode == "environment":
        return "submit_environment_model"
    if mode == "intent":
        return "submit_intent_hypothesis"
    if mode == "planning":
        return "submit_query_plan"
    return "submit_final_mql"


def _schema(name: str) -> dict[str, Any]:
    description, properties, required = _schema_definition(name)
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": True,
            },
        },
    }


def _schema_definition(name: str) -> tuple[str, dict[str, Any], list[str]]:
    collection = {"type": "string", "description": "MongoDB collection name."}
    path = {"type": "string", "description": "Dot path; use [] for arrays and * for dynamic keys."}
    limit = {"type": "integer", "minimum": 1, "description": "Bounded sample/probe limit."}
    evidence_refs = {
        "type": "array",
        "items": {"type": "string"},
        "description": "Evidence ids returned by prior observation tools.",
    }
    if name == "sample_documents":
        return (
            "Return a compact redacted shape summary for sampled documents. It never returns raw rows.",
            {"collection": collection, "limit": limit},
            ["collection"],
        )
    if name == "discover_paths":
        return (
            "Discover bounded document paths and value type counts for one collection.",
            {"collection": collection, "limit": limit},
            ["collection"],
        )
    if name == "profile_path":
        return (
            "Profile presence, missing count, value count, and type counts for one path.",
            {"collection": collection, "path": path, "limit": limit},
            ["collection", "path"],
        )
    if name == "profile_path_values":
        return (
            "Profile hashed value buckets for one path to ground constants without exposing raw rows.",
            {
                "collection": collection,
                "path": path,
                "limit": limit,
                "value_limit": {"type": "integer", "minimum": 1},
            },
            ["collection", "path"],
        )
    if name == "search_values":
        return (
            "Search sampled scalar values for a user-mentioned term and return redacted path matches.",
            {
                "collection": collection,
                "query": {"type": "string"},
                "limit": limit,
                "value_limit": {"type": "integer", "minimum": 1},
            },
            ["collection", "query"],
        )
    if name == "inspect_array_shape":
        return (
            "Inspect array lengths, element types, and object subpaths at a path.",
            {"collection": collection, "path": path, "limit": limit},
            ["collection", "path"],
        )
    if name == "inspect_dynamic_keys":
        return (
            "Inspect dynamic object keys at a path with hashed key samples and value type counts.",
            {
                "collection": collection,
                "path": path,
                "limit": limit,
                "key_limit": {"type": "integer", "minimum": 1},
            },
            ["collection", "path"],
        )
    if name == "profile_relationship_candidates":
        return (
            "Find sampled _id and *_id relationship candidates across collections.",
            {"limit": limit},
            [],
        )
    if name == "run_readonly_probe":
        return (
            (
                "Run a bounded read-only aggregate probe. Provide either MQL/mql or "
                "collection plus pipeline. Disabled operators are rejected."
            ),
            {
                "collection": collection,
                "pipeline": {"type": "array", "items": {"type": "object"}},
                "MQL": {"type": "string"},
                "mql": {"type": "string"},
                "limit": limit,
            },
            [],
        )
    if name == "submit_environment_model":
        return (
            "Submit the accepted environment model after exploration. This advances to intent mode.",
            {
                "candidate_collections": {"type": "array", "items": {"type": "string"}},
                "relevant_paths": {"type": "object"},
                "relationship_hypotheses": {"type": "array"},
                "evidence_refs": evidence_refs,
                "notes": {"type": "string"},
            },
            ["candidate_collections", "evidence_refs"],
        )
    if name == "submit_intent_hypothesis":
        return (
            "Submit the grounded NLQ intent after accepted environment evidence.",
            {
                "task_kind": {"type": "string"},
                "target_collection": {"type": "string"},
                "target_fields": {"type": "array", "items": {"type": "string"}},
                "filters": {"type": "array"},
                "aggregations": {"type": "array"},
                "evidence_refs": evidence_refs,
            },
            ["task_kind", "evidence_refs"],
        )
    if name == "submit_query_plan":
        return (
            "Submit a MongoDB aggregation plan after accepted intent evidence.",
            {
                "collection": collection,
                "stages": {"type": "array", "items": {"type": "object"}},
                "plan_summary": {"type": "string"},
                "evidence_refs": evidence_refs,
            },
            ["collection", "stages", "evidence_refs"],
        )
    if name == "submit_final_mql":
        return (
            "Submit the final MQL. This is the only successful solver exit.",
            {
                "collection": collection,
                "pipeline": {"type": "array", "items": {"type": "object"}},
                "MQL": {"type": "string"},
                "mql": {"type": "string"},
                "candidate_id": {"type": "string"},
                "evidence_refs": evidence_refs,
            },
            [],
        )
    if name == "abandon_with_failure":
        return (
            "Terminate with a structured normal failure when no valid query can be produced.",
            {"error_code": {"type": "string"}, "message": {"type": "string"}},
            [],
        )
    if name == "add_evidence_claim":
        return (
            "Record a claim that must be linked to evidence ids before gated submission.",
            {
                "claim_type": {"type": "string"},
                "statement": {"type": "string"},
                "required_evidence": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": evidence_refs,
                "used_by": {"type": "array", "items": {"type": "string"}},
            },
            ["statement"],
        )
    if name == "link_evidence":
        return (
            "Link an existing evidence id to a claim id.",
            {"claim_id": {"type": "string"}, "evidence_id": {"type": "string"}},
            ["claim_id", "evidence_id"],
        )
    if name in {"render_pipeline", "render_pipeline_prefix", "execute_pipeline_prefix", "check_prefix_checkpoint", "check_ast_filter", "run_final_sanity_execution"}:
        return (
            f"Execution/checkpoint tool: {name}. Provide collection/pipeline/MQL as applicable.",
            {
                "collection": collection,
                "pipeline": {"type": "array", "items": {"type": "object"}},
                "MQL": {"type": "string"},
                "mql": {"type": "string"},
                "prefix_length": {"type": "integer", "minimum": 1},
            },
            [],
        )
    return (
        f"SMART-EG tool: {name}. Use this only when exposed in the current mode.",
        {},
        [],
    )


def _parse_args(raw: Any) -> dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {"_raw_arguments": str(raw)}
    return dict(parsed) if isinstance(parsed, dict) else {"value": parsed}


def _dispatch_mongo_tool(
    tools: SmartEGMongoTools,
    name: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    if name == "sample_documents":
        return tools.sample_documents(args)
    if name == "discover_paths":
        return tools.discover_paths(
            str(args.get("collection") or ""),
            limit=args.get("limit"),
        )
    if name == "profile_path":
        return tools.profile_path(args)
    if name == "profile_path_values":
        return tools.profile_path_values(args)
    if name == "search_values":
        return tools.search_values(
            str(args.get("collection") or ""),
            str(args.get("query") or ""),
            limit=args.get("limit"),
            value_limit=args.get("value_limit"),
        )
    if name == "inspect_array_shape":
        return tools.inspect_array_shape(
            str(args.get("collection") or ""),
            str(args.get("path") or ""),
            limit=args.get("limit"),
        )
    if name == "inspect_dynamic_keys":
        return tools.inspect_dynamic_keys(
            str(args.get("collection") or ""),
            str(args.get("path") or ""),
            limit=args.get("limit"),
            key_limit=args.get("key_limit"),
        )
    if name == "profile_relationship_candidates":
        return tools.profile_relationship_candidates(limit=args.get("limit"))
    if name == "run_readonly_probe":
        return tools.run_readonly_probe(args)
    raise ValueError(f"unknown environment tool: {name}")


def _environment_tool_names(policy: SmartEGPolicy) -> set[str]:
    names = set(ENVIRONMENT_TOOLS)
    if not policy.relationship_probe:
        names.discard("profile_relationship_candidates")
    if not policy.value_grounding:
        names.difference_update({"profile_path_values", "search_values"})
    return names


def _evidence_tool_names(policy: SmartEGPolicy) -> set[str]:
    names = set(EVIDENCE_TOOLS)
    if not policy.enable_counterexamples:
        names.discard("mine_counterexamples")
    return names


def _execution_tool_names(policy: SmartEGPolicy) -> set[str]:
    names = set(EXECUTION_TOOLS)
    if not policy.prefix_execution:
        names.difference_update(PREFIX_EXECUTION_TOOLS)
    return names


def _stage_control_tool_names(policy: SmartEGPolicy) -> set[str]:
    names = set(STAGE_CONTROL_TOOLS)
    if not policy.revisit:
        names.discard("request_revisit")
    return names


def _known_tool_names(policy: SmartEGPolicy | None = None) -> set[str]:
    if policy is None:
        return ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | EXECUTION_TOOLS | TERMINAL_TOOLS | STAGE_CONTROL_TOOLS
    return (
        _environment_tool_names(policy)
        | _evidence_tool_names(policy)
        | _execution_tool_names(policy)
        | TERMINAL_TOOLS
        | _stage_control_tool_names(policy)
    )


_MILESTONE_RELEVANT_SOURCES: dict[str, set[str]] = {
    "environment": {
        "list_collections",
        "sample_documents",
        "discover_paths",
        "profile_path",
        "profile_path_values",
        "inspect_array_shape",
        "inspect_dynamic_keys",
        "profile_relationship_candidates",
    },
    "intent": {
        "sample_documents",
        "discover_paths",
        "profile_path",
        "profile_path_values",
        "search_values",
        "inspect_array_shape",
        "inspect_dynamic_keys",
        "profile_relationship_candidates",
    },
    "plan": {
        "sample_documents",
        "discover_paths",
        "profile_path",
        "profile_path_values",
        "search_values",
        "inspect_array_shape",
        "inspect_dynamic_keys",
        "profile_relationship_candidates",
        "run_readonly_probe",
        "check_ast_filter",
    },
    "final": {
        "run_readonly_probe",
        "run_final_sanity_execution",
        "check_ast_filter",
    },
}


def _evidence_ref_gate(
    state: SmartEGState,
    refs: list[str],
    milestone: Milestone,
    *,
    claim_type: str,
    missing_evidence: list[str],
    binding: dict[str, Any] | None = None,
) -> tuple[list[GateViolation], list[EvidenceDebt]]:
    if not refs:
        debt = state.evidence_ledger.ensure_debt(
            milestone=milestone,
            claim_type=claim_type,
            missing_evidence=missing_evidence,
            binding=binding,
        )
        return [
            GateViolation(
                "insufficient_evidence",
                f"{milestone} evidence refs are missing",
                {"missing_evidence": missing_evidence},
            )
        ], [debt]
    missing_refs = [ref for ref in refs if ref not in state.evidence_ledger.records]
    if missing_refs:
        debt = state.evidence_ledger.ensure_debt(
            milestone=milestone,
            claim_type=claim_type,
            missing_evidence=missing_evidence,
            binding=binding,
        )
        return [
            GateViolation(
                "insufficient_evidence",
                f"{milestone} evidence refs do not exist",
                {"missing_refs": missing_refs},
            )
        ], [debt]
    if not _has_relevant_evidence_refs(state, refs, milestone):
        debt = state.evidence_ledger.ensure_debt(
            milestone=milestone,
            claim_type=claim_type,
            missing_evidence=missing_evidence,
            binding=binding,
        )
        return [
            GateViolation(
                "irrelevant_evidence_refs",
                f"{milestone} evidence refs do not support this milestone",
                {
                    "evidence_refs": refs,
                    "observed_sources": sorted(_evidence_sources_for_refs(state, refs)),
                    "accepted_sources": sorted(_MILESTONE_RELEVANT_SOURCES[milestone]),
                },
            )
        ], [debt]
    return [], []


def _final_execution_evidence_gate(
    state: SmartEGState,
    refs: list[str],
    binding: dict[str, Any],
) -> tuple[list[GateViolation], list[EvidenceDebt]]:
    target_hash = str(binding.get("mql_hash") or "")
    final_refs = [
        ref
        for ref in refs
        if (
            ref in state.evidence_ledger.records
            and state.evidence_ledger.records[ref].source_tool == "run_final_sanity_execution"
        )
    ]
    if not final_refs:
        debt = state.evidence_ledger.ensure_debt(
            milestone="final",
            claim_type="final_execution_missing",
            missing_evidence=["run_final_sanity_execution"],
            suggested_tools=["run_final_sanity_execution"],
            binding=binding,
        )
        return [
            GateViolation(
                "final_execution_evidence_missing",
                "final submit must cite run_final_sanity_execution evidence",
                {"required_mql_hash": target_hash},
            )
        ], [debt]
    mismatched: list[dict[str, str]] = []
    for ref in final_refs:
        record = state.evidence_ledger.records[ref]
        observed_hash = str(record.binding.get("mql_hash") or record.summary.get("mql_hash") or "")
        if observed_hash == target_hash:
            return [], []
        mismatched.append({"evidence_id": ref, "mql_hash": observed_hash})
    debt = state.evidence_ledger.ensure_debt(
        milestone="final",
        claim_type="final_execution_mql_mismatch",
        missing_evidence=[f"mql_hash:{target_hash}"],
        suggested_tools=["run_final_sanity_execution"],
        binding=binding,
    )
    return [
        GateViolation(
            "final_execution_mql_mismatch",
            "cited final execution evidence does not match submitted final MQL",
            {"required_mql_hash": target_hash, "cited": mismatched},
        )
    ], [debt]


def _cites_final_execution_evidence(state: SmartEGState, refs: list[str]) -> bool:
    return any(
        ref in state.evidence_ledger.records
        and state.evidence_ledger.records[ref].source_tool == "run_final_sanity_execution"
        for ref in refs
    )


def _has_relevant_evidence_refs(
    state: SmartEGState,
    refs: list[str],
    milestone: Milestone,
) -> bool:
    return bool(_evidence_sources_for_refs(state, refs) & _MILESTONE_RELEVANT_SOURCES[milestone])


def _has_relevant_evidence_source(state: SmartEGState, milestone: Milestone) -> bool:
    refs = list(state.evidence_ledger.records)
    return _has_relevant_evidence_refs(state, refs, milestone) if refs else False


def _evidence_sources_for_refs(state: SmartEGState, refs: list[str]) -> set[str]:
    sources: set[str] = set()
    for ref in refs:
        record = state.evidence_ledger.records.get(ref)
        if record is None:
            continue
        sources.add(record.source_tool)
        tool = record.summary.get("tool")
        if isinstance(tool, str):
            sources.add(tool)
    return sources


def _environment_ready_for_submit(state: SmartEGState, policy: SmartEGPolicy) -> bool:
    sources = {record.source_tool for record in state.evidence_ledger.records.values()}
    shape_sources = {
        "sample_documents",
        "discover_paths",
        "profile_path",
        "profile_path_values",
        "inspect_array_shape",
        "inspect_dynamic_keys",
        "profile_relationship_candidates",
    }
    if not policy.relationship_probe:
        shape_sources.discard("profile_relationship_candidates")
    return "list_collections" in sources and bool(sources & shape_sources)


def _intent_ready_for_submit(state: SmartEGState, policy: SmartEGPolicy) -> bool:
    del policy
    if state.mode != "intent":
        return False
    if state.environment is None or "environment" in state.stale_milestones:
        return False
    if state.evidence_ledger.blocking_debts(milestone="intent"):
        return False
    sources = {record.source_tool for record in state.evidence_ledger.records.values()}
    intent_sources = {
        "discover_paths",
        "profile_path",
        "profile_path_values",
        "search_values",
    }
    return bool(sources & intent_sources)


def _planning_ready_for_submit(state: SmartEGState, policy: SmartEGPolicy) -> bool:
    if state.mode != "planning":
        return False
    if state.intent is None or "intent" in state.stale_milestones:
        return False
    if state.evidence_ledger.blocking_debts(milestone="plan"):
        if state.counters.submit_rejections == 0:
            return False
        if len(state.evidence_ledger.records) <= state.last_submit_rejection_evidence_count:
            return False
    sources = {record.source_tool for record in state.evidence_ledger.records.values()}
    plan_sources = {
        "discover_paths",
        "profile_path",
        "profile_path_values",
        "inspect_array_shape",
        "inspect_dynamic_keys",
        "profile_relationship_candidates",
        "run_readonly_probe",
        "check_ast_filter",
    }
    if not policy.relationship_probe:
        plan_sources.discard("profile_relationship_candidates")
    return bool(sources & plan_sources)


def _execution_ready_for_submit(state: SmartEGState, policy: SmartEGPolicy) -> bool:
    del policy
    if state.mode != "execution":
        return False
    if state.query_plan is None or {"environment", "intent", "plan"} & state.stale_milestones:
        return False
    if state.evidence_ledger.blocking_debts():
        return False
    return True


def _refs(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return [str(item) for item in payload.get("evidence_refs") or []]


_VALUE_COMPARISON_OPERATORS = {
    "$eq",
    "$ne",
    "$gt",
    "$gte",
    "$lt",
    "$lte",
    "$in",
    "$nin",
    "$regex",
    "$regexMatch",
}
_RANGE_COMPARISON_OPERATORS = {"$gt", "$gte", "$lt", "$lte"}
_PRESENCE_VALUE_OPERATORS = {"$exists"}
_STRUCTURAL_STRING_KEYS = {
    "as",
    "from",
    "localField",
    "foreignField",
    "path",
    "input",
    "format",
    "timezone",
    "includeArrayIndex",
}


def _value_grounding_gate(
    state: SmartEGState,
    refs: list[str],
    pipeline: list[Any],
    milestone: Milestone,
) -> tuple[list[GateViolation], list[EvidenceDebt]]:
    constants = _pipeline_value_constants(pipeline)
    if not constants:
        return [], []
    grounded = _grounded_value_markers(state, refs)
    ungrounded = [
        constant
        for constant in constants
        if not any(marker in grounded for marker in constant["markers"])
    ]
    if not ungrounded:
        return [], []
    debt = state.evidence_ledger.ensure_debt(
        milestone=milestone,
        claim_type="value_grounding",
        missing_evidence=[
            str(constant["missing_marker"])
            for constant in ungrounded[:5]
        ],
        suggested_tools=["profile_path_values", "search_values", "run_readonly_probe"],
        binding=_pipeline_binding(collection="", pipeline=pipeline, milestone=milestone),
    )
    violation = GateViolation(
        "ungrounded_value_constant",
        "Pipeline uses value constants not present in bounded literal/token evidence.",
        {
            "constants": [constant["display"] for constant in ungrounded],
            "missing_evidence": [
                str(constant["missing_marker"])
                for constant in ungrounded[:20]
            ],
            "grounded_value_marker_count": len(grounded),
            "grounded_value_markers": sorted(grounded)[:20],
        },
    )
    return [violation], [debt]


def _grounded_value_markers(state: SmartEGState, refs: list[str]) -> set[str]:
    markers: set[str] = set()
    for ref in refs:
        record = state.evidence_ledger.records.get(ref)
        if record is None:
            continue
        _collect_value_markers(record.summary, markers)
    return markers


def _collect_value_markers(payload: Any, out: set[str]) -> None:
    if isinstance(payload, dict):
        literal = payload.get("literal")
        if isinstance(literal, str):
            out.add(f"literal:{literal}")
        token = payload.get("token")
        if isinstance(token, str):
            out.add(f"token:{token}")
        for value in payload.values():
            _collect_value_markers(value, out)
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_value_markers(item, out)


def _pipeline_value_constants(pipeline: list[Any]) -> list[dict[str, Any]]:
    constants: dict[str, dict[str, Any]] = {}

    def add_constant(value: Any, *, operator: str | None = None) -> None:
        if _is_structural_query_value(value, operator=operator):
            return
        marker = _value_constant_marker(value)
        if marker is None:
            return
        constants.setdefault(marker["missing_marker"], marker)

    def visit(
        value: Any,
        *,
        in_match: bool = False,
        in_compare: bool = False,
        operator: str | None = None,
    ) -> None:
        if isinstance(value, str):
            if (in_match or in_compare) and _is_groundable_string_constant(value):
                add_constant(value, operator=operator)
            return
        if (
            (in_match or in_compare)
            and (value is None or isinstance(value, (int, float, bool)))
        ):
            add_constant(value, operator=operator)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, in_match=in_match, in_compare=in_compare, operator=operator)
            return
        if not isinstance(value, dict):
            return
        for raw_key, child in value.items():
            key = str(raw_key)
            if key in _STRUCTURAL_STRING_KEYS and isinstance(child, str):
                continue
            if key == "$literal":
                continue
            if key == "$match":
                visit(child, in_match=True, operator=key)
                continue
            child_is_comparison = key in _VALUE_COMPARISON_OPERATORS
            child_operator = (
                key
                if child_is_comparison or key in _PRESENCE_VALUE_OPERATORS
                else operator
            )
            visit(
                child,
                in_match=in_match,
                in_compare=in_compare or child_is_comparison,
                operator=child_operator,
            )

    visit(pipeline)
    return [
        constants[key]
        for key in sorted(constants)
    ]


def _is_groundable_string_constant(value: str) -> bool:
    return not value.startswith("$")


def _is_structural_query_value(value: Any, *, operator: str | None) -> bool:
    if value is None:
        return True
    if operator in _PRESENCE_VALUE_OPERATORS and isinstance(value, bool):
        return True
    if (
        operator in _RANGE_COMPARISON_OPERATORS
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return True
    return False


def _value_constant_marker(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        proof = value_proof(value)
        token_marker = f"token:{value_token(value)}"
        if proof.get("string_format"):
            return {
                "display": _display_value_constant(value),
                "markers": [token_marker],
                "missing_marker": token_marker,
            }
        literal_marker = f"literal:{value}"
        return {
            "display": value,
            "markers": [literal_marker, token_marker],
            "missing_marker": literal_marker,
        }
    if value is None or isinstance(value, (int, float, bool)):
        token_marker = f"token:{value_token(value)}"
        return {
            "display": _display_value_constant(value),
            "markers": [token_marker],
            "missing_marker": token_marker,
        }
    return None


def _display_value_constant(value: Any) -> str:
    if isinstance(value, str):
        proof = value_proof(value)
        if proof.get("string_format"):
            return f"{proof['string_format']}:{value_token(value)}"
        return value
    return f"{type(value).__name__}:{value_token(value)}"


def _output_contract_gate(
    state: SmartEGState,
    refs: list[str],
    pipeline: list[Any],
    milestone: Milestone,
) -> tuple[list[GateViolation], list[EvidenceDebt]]:
    raw_outputs = _raw_complex_projection_outputs(state, refs, pipeline)
    if not raw_outputs:
        return [], []
    debt = state.evidence_ledger.ensure_debt(
        milestone=milestone,
        claim_type="output_contract",
        missing_evidence=[f"scalarize:{item['output']}" for item in raw_outputs[:5]],
        suggested_tools=["profile_path", "run_readonly_probe"],
        binding=_pipeline_binding(
            collection="",
            pipeline=pipeline,
            milestone=milestone,
            paths=[item["source_path"] for item in raw_outputs if item.get("source_path")],
        ),
    )
    violation = GateViolation(
        "raw_complex_output",
        (
            "Final projection exposes object/array context fields. Project scalar identifiers, "
            "counts, shares, or named scalar context fields instead of raw nested values."
        ),
        {"raw_outputs": raw_outputs},
    )
    return [violation], [debt]


def _raw_complex_projection_outputs(
    state: SmartEGState,
    refs: list[str],
    pipeline: list[Any],
) -> list[dict[str, str]]:
    project = _last_project(pipeline)
    if project is None:
        return []
    complex_paths = _complex_evidence_paths(state, refs)
    alias_sources = _alias_sources_before_last_project(pipeline)
    raw: list[dict[str, str]] = []
    for output, expression in project.items():
        source_info = _direct_projection_source(str(output), expression)
        if source_info is None:
            continue
        source, is_passthrough = source_info
        if is_passthrough and source in alias_sources:
            source = alias_sources[source][0]
        if source in complex_paths or (is_passthrough and str(output) in complex_paths):
            raw.append({"output": str(output), "source_path": source})
    return raw


def _last_project(pipeline: list[Any]) -> dict[str, Any] | None:
    index = _last_project_index(pipeline)
    if index is None:
        return None
    project = pipeline[index].get("$project") if isinstance(pipeline[index], dict) else None
    return project if isinstance(project, dict) else None


def _last_project_index(pipeline: list[Any]) -> int | None:
    for index in range(len(pipeline) - 1, -1, -1):
        stage = pipeline[index]
        if not isinstance(stage, dict):
            continue
        project = stage.get("$project")
        if isinstance(project, dict):
            return index
    return None


def _direct_projection_source(output: str, expression: Any) -> tuple[str, bool] | None:
    if expression in (1, True):
        return output, True
    if isinstance(expression, str) and expression.startswith("$"):
        return expression.lstrip("$"), False
    return None


def _field_path_contract_gate(
    state: SmartEGState,
    refs: list[str],
    pipeline: list[Any],
    milestone: Milestone,
) -> tuple[list[GateViolation], list[EvidenceDebt]]:
    unknown = _unknown_pipeline_field_paths(state, refs, pipeline)
    if not unknown:
        return [], []
    debt = state.evidence_ledger.ensure_debt(
        milestone=milestone,
        claim_type="field_path_contract",
        missing_evidence=[f"field_path:{item['path']}" for item in unknown[:5]],
        suggested_tools=["profile_path", "discover_paths", "run_readonly_probe"],
        binding=_pipeline_binding(
            collection="",
            pipeline=pipeline,
            milestone=milestone,
            paths=[item["resolved_path"] for item in unknown if item.get("resolved_path")],
        ),
    )
    violation = GateViolation(
        "unknown_field_path",
        "Pipeline references nested field paths not supported by observed scalar/object evidence.",
        {"paths": [item["path"] for item in unknown], "resolved_paths": unknown},
    )
    return [violation], [debt]


def _unknown_pipeline_field_paths(
    state: SmartEGState,
    refs: list[str],
    pipeline: list[Any],
) -> list[dict[str, str]]:
    scalar_paths, complex_paths, all_paths = _evidence_path_sets(state, refs)
    missing_paths = _missing_profile_paths(state, refs)
    if not all_paths and not missing_paths:
        return []
    alias_sources = _alias_sources_before_last_project(pipeline)
    generated_group_paths = _generated_group_id_paths(pipeline)
    unknown: list[dict[str, str]] = []
    for ref in _pipeline_field_refs(pipeline):
        if _is_generated_group_ref(ref, generated_group_paths):
            continue
        resolved = _resolve_alias_ref(ref, alias_sources)
        if resolved is None:
            continue
        has_path_evidence = _has_evidence_path(resolved, all_paths)
        if not has_path_evidence and _is_refuted_by_missing_profile(resolved, missing_paths):
            unknown.append({"path": ref, "resolved_path": resolved})
            continue
        if _extends_observed_scalar_path(resolved, scalar_paths):
            unknown.append({"path": ref, "resolved_path": resolved})
            continue
        if _under_observed_complex_path(resolved, complex_paths) and not has_path_evidence:
            unknown.append({"path": ref, "resolved_path": resolved})
    deduped: dict[str, dict[str, str]] = {}
    for item in unknown:
        deduped.setdefault(item["path"], item)
    return list(deduped.values())


def _pipeline_field_refs(pipeline: list[Any]) -> list[str]:
    refs: list[str] = []

    def visit(value: Any, *, key: str | None = None) -> None:
        if isinstance(value, str):
            if key in _STRUCTURAL_STRING_KEYS:
                return
            if value.startswith("$") and not value.startswith("$$"):
                refs.append(value.lstrip("$"))
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for raw_key, child in value.items():
            visit(child, key=str(raw_key))

    visit(pipeline)
    return refs


def _generated_group_id_paths(pipeline: list[Any]) -> set[str]:
    paths: set[str] = set()
    for stage in pipeline:
        if not isinstance(stage, dict):
            continue
        group = stage.get("$group")
        if not isinstance(group, dict):
            continue
        group_id = group.get("_id")
        if isinstance(group_id, dict):
            for key in group_id:
                paths.add(f"_id.{key}")
    return paths


def _is_generated_group_ref(ref: str, generated_group_paths: set[str]) -> bool:
    return any(ref == path or ref.startswith(f"{path}.") for path in generated_group_paths)


def _alias_sources_before_last_project(pipeline: list[Any]) -> dict[str, tuple[str, str]]:
    end = _last_project_index(pipeline)
    if end is None:
        end = len(pipeline)
    aliases: dict[str, tuple[str, str]] = {}
    for stage in pipeline[:end]:
        if not isinstance(stage, dict):
            continue
        for operator in ("$addFields", "$set"):
            assignments = stage.get(operator)
            if isinstance(assignments, dict):
                _update_alias_sources(aliases, assignments)
        project = stage.get("$project")
        if isinstance(project, dict):
            for output, expression in project.items():
                name = str(output)
                source = _expression_source(expression)
                if source is not None:
                    aliases[name] = source
                elif expression in (0, False):
                    aliases.pop(name, None)
                elif expression not in (1, True):
                    aliases.pop(name, None)
    return aliases


def _update_alias_sources(
    aliases: dict[str, tuple[str, str]],
    assignments: dict[Any, Any],
) -> None:
    for output, expression in assignments.items():
        name = str(output)
        source = _expression_source(expression)
        if source is None:
            aliases.pop(name, None)
        else:
            aliases[name] = source


def _expression_source(expression: Any) -> tuple[str, str] | None:
    if isinstance(expression, str) and expression.startswith("$") and not expression.startswith("$$"):
        return expression.lstrip("$"), "direct"
    if isinstance(expression, dict) and len(expression) == 1:
        value = expression.get("$objectToArray")
        if isinstance(value, str) and value.startswith("$"):
            return value.lstrip("$"), "object_to_array"
    return None


def _resolve_alias_ref(ref: str, aliases: dict[str, tuple[str, str]]) -> str | None:
    parts = ref.split(".")
    if not parts:
        return ref
    alias = aliases.get(parts[0])
    if alias is None:
        return ref
    source, transform = alias
    rest = parts[1:]
    if transform == "object_to_array":
        if not rest:
            return source
        if rest[0] == "k":
            return None
        if rest[0] == "v":
            if len(rest) == 1:
                return f"{source}.*"
            return ".".join([source, "*[]", *rest[1:]])
    if not rest:
        return source
    return ".".join([source, *rest])


def _complex_evidence_paths(state: SmartEGState, refs: list[str]) -> set[str]:
    return _evidence_path_sets(state, refs)[1]


def _evidence_path_sets(
    state: SmartEGState,
    refs: list[str],
) -> tuple[set[str], set[str], set[str]]:
    scalar_paths: set[str] = set()
    complex_paths: set[str] = set()
    all_paths: set[str] = set()
    paths: set[str] = set()
    records = [
        state.evidence_ledger.records[ref]
        for ref in refs
        if ref in state.evidence_ledger.records
    ]
    for record in records:
        _collect_type_paths(record.summary, scalar_paths, complex_paths, all_paths)
    for path in list(scalar_paths):
        scalar_paths.update(_path_variants(path))
    for path in list(complex_paths):
        complex_paths.update(_path_variants(path))
    for path in list(all_paths):
        all_paths.update(_path_variants(path))
    return scalar_paths, complex_paths, all_paths


def _missing_profile_paths(state: SmartEGState, refs: list[str]) -> set[str]:
    paths: set[str] = set()
    for ref in refs:
        record = state.evidence_ledger.records.get(ref)
        if record is None:
            continue
        summary = record.summary
        if not isinstance(summary, dict):
            continue
        if record.source_tool != "profile_path" and summary.get("tool") != "profile_path":
            continue
        path = summary.get("path")
        if not isinstance(path, str) or not path:
            continue
        if _has_observed_type_counts(summary.get("type_counts")):
            continue
        if not _profile_summary_is_missing_only(summary):
            continue
        paths.add(path)
        paths.update(_path_variants(path))
    return paths


def _profile_summary_is_missing_only(summary: dict[str, Any]) -> bool:
    counters = ("present_count", "exists_count", "value_count")
    if not any(key in summary for key in counters):
        return False
    for key in counters:
        if key not in summary:
            continue
        try:
            if int(summary.get(key) or 0) > 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _collect_complex_paths(payload: Any, out: set[str]) -> None:
    _collect_type_paths(payload, set(), out, set())


def _collect_type_paths(
    payload: Any,
    scalar_paths: set[str],
    complex_paths: set[str],
    all_paths: set[str],
) -> None:
    if isinstance(payload, dict):
        top_level_type_counts = payload.get("top_level_type_counts")
        if isinstance(top_level_type_counts, dict):
            for item_path, item_type_counts in top_level_type_counts.items():
                if not isinstance(item_path, str) or not isinstance(item_type_counts, dict):
                    continue
                all_paths.add(item_path)
                if _is_complex_type_counts(item_type_counts):
                    complex_paths.add(item_path)
                elif _is_scalar_type_counts(item_type_counts):
                    scalar_paths.add(item_path)
        path = payload.get("path")
        type_counts = payload.get("type_counts")
        if (
            isinstance(path, str)
            and isinstance(type_counts, dict)
            and _has_observed_type_counts(type_counts)
        ):
            all_paths.add(path)
            if _is_complex_type_counts(type_counts):
                complex_paths.add(path)
            elif _is_scalar_type_counts(type_counts):
                scalar_paths.add(path)
        paths = payload.get("paths")
        if isinstance(paths, dict):
            for item_path, info in paths.items():
                if not isinstance(item_path, str) or not isinstance(info, dict):
                    continue
                item_type_counts = info.get("type_counts")
                if not isinstance(item_type_counts, dict) or not _has_observed_type_counts(
                    item_type_counts
                ):
                    continue
                all_paths.add(item_path)
                if _is_complex_type_counts(item_type_counts):
                    complex_paths.add(item_path)
                elif _is_scalar_type_counts(item_type_counts):
                    scalar_paths.add(item_path)
        for value in payload.values():
            _collect_type_paths(value, scalar_paths, complex_paths, all_paths)
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_type_paths(item, scalar_paths, complex_paths, all_paths)


def _is_complex_type_counts(type_counts: Any) -> bool:
    return isinstance(type_counts, dict) and (
        int(type_counts.get("object", 0) or 0) > 0
        or int(type_counts.get("array", 0) or 0) > 0
    )


def _has_observed_type_counts(type_counts: Any) -> bool:
    if not isinstance(type_counts, dict):
        return False
    return any(int(count or 0) > 0 for count in type_counts.values())


def _is_scalar_type_counts(type_counts: Any) -> bool:
    if not isinstance(type_counts, dict):
        return False
    scalar_count = 0
    for kind, count in type_counts.items():
        if kind in {"object", "array", "null"}:
            continue
        scalar_count += int(count or 0)
    return scalar_count > 0


def _path_variants(path: str) -> set[str]:
    parts = list(parse_path(path))
    variants = {_format_parsed_path(parts)}
    for index, part in enumerate(parts[:-1]):
        if part == "[]" or parts[index + 1] != "[]" or index == 0:
            continue
        wildcarded = list(parts)
        wildcarded[index] = "*"
        variants.add(_format_parsed_path(wildcarded))
    return variants


def _format_parsed_path(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if part == "[]":
            out += "[]"
        else:
            out = part if not out else f"{out}.{part}"
    return out


def _extends_observed_scalar_path(path: str, scalar_paths: set[str]) -> bool:
    return any(
        len(parse_path(path)) > len(parse_path(scalar_path))
        and _path_prefix_matches(scalar_path, path)
        for scalar_path in scalar_paths
    )


def _under_observed_complex_path(path: str, complex_paths: set[str]) -> bool:
    return any(_path_prefix_matches(complex_path, path) for complex_path in complex_paths)


def _has_evidence_path(path: str, all_paths: set[str]) -> bool:
    return any(
        _path_matches(evidence_path, path) or _path_prefix_matches(path, evidence_path)
        for evidence_path in all_paths
    )


def _is_refuted_by_missing_profile(path: str, missing_paths: set[str]) -> bool:
    return any(_path_prefix_matches(missing_path, path) for missing_path in missing_paths)


def _path_prefix_matches(prefix: str, path: str) -> bool:
    prefix_parts = parse_path(prefix)
    path_parts = parse_path(path)
    if len(prefix_parts) > len(path_parts):
        return False
    return _path_parts_match(prefix_parts, path_parts[: len(prefix_parts)])


def _path_matches(pattern: str, path: str) -> bool:
    pattern_parts = parse_path(pattern)
    path_parts = parse_path(path)
    if len(pattern_parts) != len(path_parts):
        return False
    return _path_parts_match(pattern_parts, path_parts)


def _path_parts_match(pattern_parts: tuple[str, ...], path_parts: tuple[str, ...]) -> bool:
    for expected, actual in zip(pattern_parts, path_parts, strict=True):
        if expected == "*":
            if actual == "[]":
                return False
            continue
        if expected != actual:
            return False
    return True


def _mql_hash(mql: str) -> str:
    return stable_hash(mql)


def _prefix_length(args: dict[str, Any], stage_count: int) -> int:
    raw = args.get("prefix_length")
    if raw is None:
        return stage_count
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("prefix_length must be an integer") from exc
    if value <= 0:
        raise ValueError("prefix_length must be positive")
    if value > stage_count:
        raise ValueError("prefix_length exceeds pipeline stage count")
    return value


def _milestone_for_mode(mode: str) -> Milestone:
    if mode == "environment":
        return "environment"
    if mode == "intent":
        return "intent"
    if mode == "planning":
        return "plan"
    return "final"


def _mode_rank(mode: str) -> int:
    return {
        "environment": 0,
        "intent": 1,
        "planning": 2,
        "execution": 3,
        "repair": 4,
    }.get(mode, 0)


def _pipeline_binding(
    *,
    collection: str,
    pipeline: list[Any],
    milestone: Milestone | str,
    mql: str | None = None,
    candidate_id: str | None = None,
    paths: list[str] | None = None,
) -> dict[str, Any]:
    binding: dict[str, Any] = {"milestone": str(milestone)}
    if collection:
        binding["collection"] = collection
    operators = sorted(_pipeline_operators(pipeline))
    if operators:
        binding["operator"] = operators
    path_set = set(paths or [])
    path_set.update(_pipeline_field_refs(pipeline))
    path_set.update(_pipeline_match_field_paths(pipeline))
    if path_set:
        binding["paths"] = sorted(path_set)
    if mql:
        binding["mql_hash"] = _mql_hash(mql)
    if candidate_id:
        binding["candidate_id"] = candidate_id
    return binding


def _evidence_binding(
    *,
    source_tool: str,
    args: dict[str, Any],
    summary: dict[str, Any],
    milestone: Milestone,
) -> dict[str, Any]:
    binding: dict[str, Any] = {"milestone": milestone}
    collection = summary.get("collection") or args.get("collection")
    if collection:
        binding["collection"] = str(collection)
    path = summary.get("path") or args.get("path")
    if path:
        binding["paths"] = [str(path)]
    if summary.get("mql_hash"):
        binding["mql_hash"] = str(summary["mql_hash"])
    if summary.get("candidate_id") or args.get("candidate_id"):
        binding["candidate_id"] = str(summary.get("candidate_id") or args.get("candidate_id"))
    mql = args.get("MQL") or args.get("mql")
    pipeline = args.get("pipeline")
    if mql or (collection and isinstance(pipeline, list)):
        try:
            parsed_collection, parsed_pipeline, rendered = parse_or_render_mql(
                collection=str(collection) if collection else None,
                pipeline=pipeline if isinstance(pipeline, list) else None,
                mql=str(mql) if mql else None,
            )
            binding.update(
                _pipeline_binding(
                    collection=parsed_collection,
                    pipeline=parsed_pipeline,
                    milestone=milestone,
                    mql=rendered,
                    candidate_id=binding.get("candidate_id"),
                )
            )
        except Exception:  # noqa: BLE001 - best-effort binding only
            pass
    if source_tool == "profile_relationship_candidates":
        binding.setdefault("relationship_pair", summary.get("relationship_pair", "candidate_scan"))
    return binding


def _pipeline_operators(pipeline: list[Any]) -> set[str]:
    operators: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(key, str) and key.startswith("$"):
                    operators.add(key)
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(pipeline)
    return operators


def _pipeline_match_field_paths(pipeline: list[Any]) -> set[str]:
    paths: set[str] = set()

    def visit_match(value: Any, prefix: str = "") -> None:
        if not isinstance(value, dict):
            return
        for raw_key, child in value.items():
            key = str(raw_key)
            if key.startswith("$"):
                if isinstance(child, list):
                    for item in child:
                        visit_match(item, prefix)
                elif isinstance(child, dict):
                    visit_match(child, prefix)
                continue
            path = key if not prefix else f"{prefix}.{key}"
            paths.add(path)
            if isinstance(child, dict) and not any(str(k).startswith("$") for k in child):
                visit_match(child, path)

    for stage in pipeline:
        if isinstance(stage, dict) and isinstance(stage.get("$match"), dict):
            visit_match(stage["$match"])
    return paths


def _accepted_collections(state: SmartEGState) -> set[str]:
    collections: set[str] = set()
    if isinstance(state.environment, dict):
        for item in state.environment.get("candidate_collections") or []:
            if item:
                collections.add(str(item))
    for record in state.evidence_ledger.records.values():
        summary = record.summary
        if record.source_tool == "list_collections":
            for item in summary.get("collections") or []:
                if isinstance(item, dict) and item.get("collection"):
                    collections.add(str(item["collection"]))
                elif item:
                    collections.add(str(item))
        collection = summary.get("collection") or record.binding.get("collection")
        if collection:
            collections.add(str(collection))
    return collections


def _has_meaningful_intent_contract(intent: dict[str, Any]) -> bool:
    for key in (
        "target_fields",
        "filters",
        "aggregations",
        "output_contract",
        "output_fields",
        "group_by",
    ):
        value = intent.get(key)
        if isinstance(value, (list, tuple, set, dict)) and len(value) > 0:
            return True
        if isinstance(value, str) and value.strip():
            return True
    return False


def _resolve_debts(
    state: SmartEGState,
    milestone: str,
    *,
    refs: list[str],
    binding: dict[str, Any],
) -> None:
    records = [
        state.evidence_ledger.records[ref]
        for ref in refs
        if ref in state.evidence_ledger.records
    ]
    sources = {record.source_tool for record in records}
    markers = _markers_for_records(records)
    for debt in state.evidence_ledger.debts.values():
        if debt.resolved or debt.milestone != milestone:
            continue
        if debt.binding and not _binding_compatible(debt.binding, binding):
            continue
        missing = set(debt.missing_evidence)
        if not missing:
            if records:
                debt.resolved = True
            continue
        if missing.issubset(sources) or missing.issubset(markers):
            if not debt.binding or any(_binding_compatible(debt.binding, _record_binding(record)) for record in records):
                debt.resolved = True
    state.refresh_debt_queue()


def _resolve_milestone_debts(state: SmartEGState, milestone: str) -> None:
    for debt in state.evidence_ledger.debts.values():
        if debt.milestone == milestone:
            debt.resolved = True
    state.refresh_debt_queue()


def _record_binding(record: Any) -> dict[str, Any]:
    binding = getattr(record, "binding", {}) or {}
    if binding:
        return dict(binding)
    summary = getattr(record, "summary", {}) or {}
    return {
        key: value
        for key, value in summary.items()
        if key in {"collection", "paths", "operator", "relationship_pair", "mql_hash", "candidate_id", "milestone"}
    }


def _binding_compatible(required: dict[str, Any], observed: dict[str, Any]) -> bool:
    matched = 0
    for key, expected in required.items():
        actual = observed.get(key)
        if actual in (None, "", [], {}, ()):
            continue
        if key in {"paths", "operator"}:
            expected_set = set(_as_list(expected))
            actual_set = set(_as_list(actual))
            if expected_set and not expected_set.issubset(actual_set):
                return False
            matched += 1
            continue
        if key == "relationship_pair":
            expected_set = set(_as_list(expected))
            actual_set = set(_as_list(actual))
            if expected_set and actual_set:
                if expected_set != actual_set:
                    return False
            elif str(expected) != str(actual):
                return False
            matched += 1
            continue
        if str(expected) != str(actual):
            return False
        matched += 1
    return matched > 0


def _markers_for_records(records: list[Any]) -> set[str]:
    markers: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            tool = value.get("tool")
            if isinstance(tool, str):
                markers.add(tool)
            collection = value.get("collection")
            if isinstance(collection, str):
                markers.add(f"collection:{collection}")
            path = value.get("path")
            if isinstance(path, str):
                markers.add(f"field_path:{path}")
                markers.add(f"path:{path}")
            for path_item in _as_list(value.get("paths")):
                markers.add(f"field_path:{path_item}")
                markers.add(f"path:{path_item}")
            for key in ("literal", "token", "mql_hash", "candidate_id"):
                item = value.get(key)
                if isinstance(item, str):
                    markers.add(f"{key}:{item}" if key not in {"literal", "token"} else f"{key}:{item}")
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for record in records:
        markers.add(str(getattr(record, "source_tool", "")))
        visit(getattr(record, "summary", {}))
        visit(getattr(record, "binding", {}))
    return markers


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return sorted(str(key) for key in value)
    if isinstance(value, (list, tuple, set, frozenset)):
        return sorted(str(item) for item in value if item)
    return [str(value)]


def _gate(
    submit_tool: str,
    milestone: str,
    accepted: bool,
    violations: list[GateViolation],
    debts: list[EvidenceDebt],
    challenged: list[str] | None = None,
    *,
    candidate_id: str | None = None,
    accepted_action: str = "continue",
) -> SubmitGateResult:
    return SubmitGateResult(
        submit_tool=submit_tool,
        accepted=accepted,
        milestone=milestone,  # type: ignore[arg-type]
        candidate_id=candidate_id,
        violations=violations,
        new_debts=debts,
        challenged_claims=list(challenged or []),
        required_next_action=accepted_action if accepted else "continue",  # type: ignore[arg-type]
    )

def _observation(
    name: str,
    tool_call_id: str,
    ok: bool,
    result: dict[str, Any],
    evidence_ids: list[str] | None = None,
    gate_ref: str | None = None,
) -> ToolObservation:
    return ToolObservation(
        name=name,
        tool_call_id=tool_call_id,
        ok=ok,
        result=result,
        evidence_ids=list(evidence_ids or []),
        gate_ref=gate_ref,
        llm_visible_content={"ok": ok, "tool": name, **result},
    )


def _milestone(raw: Any) -> str:
    value = str(raw or "environment")
    if value == "planning":
        return "plan"
    if value not in {"environment", "intent", "plan", "final"}:
        return "environment"
    return value
