"""Provider-native tool exposure and deterministic gates for SMART-EG."""
from __future__ import annotations

import json
from typing import Any

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
from .safety import parse_path

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
    "check_environment_model",
    "check_intent_hypothesis",
    "check_query_plan",
    "check_final_candidate",
}
EXECUTION_TOOLS = {
    "render_pipeline",
    "render_pipeline_prefix",
    "execute_pipeline_prefix",
    "check_prefix_checkpoint",
    "check_ast_filter",
    "run_final_sanity_execution",
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
        if state.terminal_only:
            return [_schema(name) for name in sorted(_terminal_tool_names_for_state(state))]
        if state.mode == "environment":
            if _environment_ready_for_submit(state):
                names = {
                    "submit_environment_model",
                    "inspect_evidence_ledger",
                    "inspect_evidence_debt",
                    "abandon_with_failure",
                }
            else:
                names = ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | {
                    "submit_environment_model",
                    "request_mode_shift",
                    "abandon_with_failure",
                }
        elif state.mode == "intent":
            if _intent_ready_for_submit(state):
                names = {
                    "submit_intent_hypothesis",
                    "inspect_evidence_ledger",
                    "inspect_evidence_debt",
                    "abandon_with_failure",
                }
            else:
                names = ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | {
                    "submit_intent_hypothesis",
                    "request_revisit",
                    "request_mode_shift",
                    "abandon_with_failure",
                }
        elif state.mode == "planning":
            if _planning_ready_for_submit(state):
                names = {
                    "submit_query_plan",
                    "inspect_evidence_ledger",
                    "inspect_evidence_debt",
                    "abandon_with_failure",
                }
            else:
                names = ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | {
                    "render_pipeline",
                    "check_ast_filter",
                    "submit_query_plan",
                    "request_revisit",
                    "abandon_with_failure",
                }
        else:
            if _execution_ready_for_submit(state):
                names = {
                    "submit_final_mql",
                    "inspect_evidence_ledger",
                    "inspect_evidence_debt",
                    "abandon_with_failure",
                }
            else:
                names = ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | EXECUTION_TOOLS | {
                    "submit_final_mql",
                    "request_revisit",
                    "request_mode_shift",
                    "abandon_with_failure",
                }
        return [_schema(name) for name in sorted(names)]

    def tool_choice_for_state(self, state: SmartEGState) -> dict[str, Any] | str | None:
        if state.terminal_only:
            if _terminal_has_repair_tools(state):
                return None
            return {"type": "function", "function": {"name": _terminal_submit_tool_for_mode(state.mode)}}
        if state.mode == "environment" and _environment_ready_for_submit(state):
            return {"type": "function", "function": {"name": "submit_environment_model"}}
        if _intent_ready_for_submit(state):
            return {"type": "function", "function": {"name": "submit_intent_hypothesis"}}
        if _planning_ready_for_submit(state):
            return {"type": "function", "function": {"name": "submit_query_plan"}}
        if _execution_ready_for_submit(state):
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
        exposed = exposed_tool_names or {tool["function"]["name"] for tool in self.tools_for_state(state)}
        if name not in exposed and name not in TERMINAL_TOOLS:
            if state.mode == "environment" and _environment_ready_for_submit(state) and name in ENVIRONMENT_TOOLS:
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
            if state.terminal_only and name in _known_tool_names():
                allowed = _terminal_tool_names_for_state(state)
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
            if state.mode == "intent" and _intent_ready_for_submit(state) and name in _known_tool_names():
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
            if state.mode == "planning" and _planning_ready_for_submit(state) and name in _known_tool_names():
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
            if state.mode == "execution" and _execution_ready_for_submit(state) and name in _known_tool_names():
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
        if name in ENVIRONMENT_TOOLS:
            return self._mongo_observation(name, call_id, args, state)
        if name == "add_evidence_claim":
            return self._add_evidence_claim(call_id, args, state)
        if name == "link_evidence":
            state.evidence_ledger.link_evidence(str(args.get("claim_id")), str(args.get("evidence_id")))
            state.refresh_debt_queue()
            return _observation(name, call_id, True, {"debt_count": len(state.debt_queue)})
        if name == "inspect_evidence_ledger":
            return _observation(name, call_id, True, state.evidence_ledger.summary())
        if name == "inspect_evidence_debt":
            state.refresh_debt_queue()
            return _observation(name, call_id, True, {"debts": [d.to_json() for d in state.debt_queue]})
        if name == "mine_counterexamples":
            hits = mine_counterexamples(
                plan=args.get("plan") or state.query_plan,
                final_candidate=args.get("final_candidate"),
                ledger=state.evidence_ledger,
            )
            return _observation(name, call_id, True, {"hits": [h.to_json() for h in hits]})
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
            try:
                _, _, mql = parse_or_render_mql(
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
            return _observation(name, call_id, bool(result["ok"]), result)
        return _observation(name, call_id, True, {"bounded": True, "implemented": False})

    def _list_collections(self, call_id: str, state: SmartEGState) -> ToolObservation:
        raw: Any = []
        error: dict[str, Any] | None = None
        if self.db_handle is not None:
            try:
                if hasattr(self.db_handle, "list_collections"):
                    try:
                        raw = self.db_handle.list_collections({})
                    except TypeError:
                        try:
                            raw = self.db_handle.list_collections(state.db_id)
                        except TypeError:
                            raw = self.db_handle.list_collections()
                elif hasattr(self.db_handle, "list_collection_names"):
                    raw = self.db_handle.list_collection_names()
            except Exception as exc:  # noqa: BLE001 - expose bounded tool feedback
                error = {"error_type": type(exc).__name__, "message": str(exc)[:300]}
                raw = []
        if isinstance(raw, dict):
            raw = raw.get("collections") or []
        collections = [
            str(item.get("collection") if isinstance(item, dict) else item)
            for item in raw
            if item
        ]
        summary: dict[str, Any] = {"collections": collections}
        if error is not None:
            summary["error"] = error
        record = state.evidence_ledger.add_record(
            source_tool="list_collections",
            tool_call_id=call_id,
            observation_ref="",
            summary=summary,
            supports_claims=[],
            redaction={"raw_rows": False},
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
        if not refs or not state.evidence_ledger.has_evidence_refs(refs):
            debts.append(state.evidence_ledger.ensure_debt(
                milestone="environment",
                claim_type="evidence_refs_missing",
                missing_evidence=["list_collections"],
            ))
            violations.append(GateViolation("insufficient_evidence", "environment evidence refs are missing"))
        gate = _gate("submit_environment_model", "environment", not violations, violations, debts)
        if gate.accepted:
            state.environment = dict(model)
            state.mode = "intent"
            _resolve_debts(state, "environment")
        return self._submit_observation("submit_environment_model", call_id, gate, state)

    def _submit_intent(self, call_id: str, intent: Any, state: SmartEGState) -> ToolObservation:
        violations: list[GateViolation] = []
        debts: list[EvidenceDebt] = []
        refs = _refs(intent)
        if state.environment is None or "environment" in state.stale_milestones:
            violations.append(GateViolation("stale_environment", "intent requires accepted environment"))
        if not isinstance(intent, dict) or not intent.get("task_kind"):
            violations.append(GateViolation("contract_invalid", "intent needs task_kind"))
        if not refs or not state.evidence_ledger.has_evidence_refs(refs):
            debts.append(state.evidence_ledger.ensure_debt(
                milestone="intent",
                claim_type="intent_evidence_missing",
                missing_evidence=["profile_path_values"],
            ))
            violations.append(GateViolation("insufficient_evidence", "intent evidence refs are missing"))
        gate = _gate("submit_intent_hypothesis", "intent", not violations, violations, debts)
        if gate.accepted:
            state.intent = dict(intent)
            state.mode = "planning"
            _resolve_debts(state, "intent")
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
        if not refs or not state.evidence_ledger.has_evidence_refs(refs):
            debts.append(state.evidence_ledger.ensure_debt(
                milestone="plan",
                claim_type="plan_evidence_missing",
                missing_evidence=["discover_paths"],
            ))
            violations.append(GateViolation("insufficient_evidence", "plan evidence refs are missing"))
        if self.policy.enable_counterexamples and isinstance(plan, dict):
            for hit in mine_counterexamples(plan=plan, ledger=state.evidence_ledger):
                challenged.extend(hit.challenged_claims)
                debts.append(state.evidence_ledger.ensure_debt(
                    milestone="plan",
                    claim_type=hit.code,
                    missing_evidence=hit.suggested_tools,
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
            _resolve_debts(state, "plan")
        return self._submit_observation("submit_query_plan", call_id, gate, state)

    def _submit_final(self, call_id: str, args: dict[str, Any], state: SmartEGState) -> ToolObservation:
        violations: list[GateViolation] = []
        debts: list[EvidenceDebt] = []
        refs = _refs(args)
        has_prior_milestones = any([state.environment, state.intent, state.query_plan])
        if has_prior_milestones and (
            state.query_plan is None or {"environment", "intent", "plan"} & state.stale_milestones
        ):
            violations.append(GateViolation("stale_milestone", "final requires fresh milestones"))
        if has_prior_milestones and (not refs or not state.evidence_ledger.has_evidence_refs(refs)):
            debts.append(state.evidence_ledger.ensure_debt(
                milestone="final",
                claim_type="final_evidence_missing",
                missing_evidence=["run_final_sanity_execution"],
            ))
            violations.append(GateViolation("insufficient_evidence", "final evidence refs are missing"))
        collection = ""
        pipeline: list[dict[str, Any]] = []
        mql = ""
        try:
            collection, pipeline, mql = parse_or_render_mql(
                collection=args.get("collection"),
                pipeline=args.get("pipeline"),
                mql=args.get("MQL") or args.get("mql"),
            )
            ast = check_ast_filter(mql)
            if not ast["ok"]:
                violations.append(GateViolation("boundary_rejected", "disallowed operators", ast))
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
        candidate_id = str(args.get("candidate_id") or "cand-final")
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
        state.mode = target  # type: ignore[assignment]
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

    def _submit_observation(self, name: str, call_id: str, gate: SubmitGateResult, state: SmartEGState) -> ToolObservation:
        if not gate.accepted:
            state.counters.submit_rejections += 1
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
        return _observation(name, call_id, True, payload, gate_ref=gate_ref)

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


def _terminal_tool_names_for_state(state: SmartEGState) -> set[str]:
    names = {_terminal_submit_tool_for_mode(state.mode), "abandon_with_failure"}
    debts = state.evidence_ledger.blocking_debts()
    if debts:
        names.update({"inspect_evidence_debt", "inspect_evidence_ledger"})
        for debt in debts:
            names.update(tool for tool in debt.suggested_tools if tool in _known_tool_names())
    return names


def _terminal_has_repair_tools(state: SmartEGState) -> bool:
    return bool(_terminal_tool_names_for_state(state) - {_terminal_submit_tool_for_mode(state.mode), "abandon_with_failure"})


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


def _environment_ready_for_submit(state: SmartEGState) -> bool:
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
    return "list_collections" in sources and bool(sources & shape_sources)


def _intent_ready_for_submit(state: SmartEGState) -> bool:
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


def _planning_ready_for_submit(state: SmartEGState) -> bool:
    if state.mode != "planning":
        return False
    if state.intent is None or "intent" in state.stale_milestones:
        return False
    if state.evidence_ledger.blocking_debts(milestone="plan"):
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
    return bool(sources & plan_sources)


def _execution_ready_for_submit(state: SmartEGState) -> bool:
    if state.mode != "execution":
        return False
    if state.query_plan is None or {"environment", "intent", "plan"} & state.stale_milestones:
        return False
    return not state.evidence_ledger.blocking_debts(milestone="final")


def _known_tool_names() -> set[str]:
    return ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | EXECUTION_TOOLS | TERMINAL_TOOLS | STAGE_CONTROL_TOOLS


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
    constants = _pipeline_string_constants(pipeline)
    if not constants:
        return [], []
    grounded = _grounded_literals(state, refs)
    ungrounded = [constant for constant in constants if constant not in grounded]
    if not ungrounded:
        return [], []
    debt = state.evidence_ledger.ensure_debt(
        milestone=milestone,
        claim_type="value_grounding",
        missing_evidence=[f"literal:{constant}" for constant in ungrounded[:5]],
        suggested_tools=["profile_path_values", "search_values", "run_readonly_probe"],
    )
    violation = GateViolation(
        "ungrounded_value_constant",
        "Pipeline uses string value constants not present in bounded literal evidence.",
        {
            "constants": ungrounded,
            "grounded_literal_count": len(grounded),
            "grounded_literals": sorted(grounded)[:20],
        },
    )
    return [violation], [debt]


def _grounded_literals(state: SmartEGState, refs: list[str]) -> set[str]:
    literals: set[str] = set()
    for ref in refs:
        record = state.evidence_ledger.records.get(ref)
        if record is None:
            continue
        _collect_literals(record.summary, literals)
    return literals


def _collect_literals(payload: Any, out: set[str]) -> None:
    if isinstance(payload, dict):
        literal = payload.get("literal")
        if isinstance(literal, str):
            out.add(literal)
        for value in payload.values():
            _collect_literals(value, out)
        return
    if isinstance(payload, list):
        for item in payload:
            _collect_literals(item, out)


def _pipeline_string_constants(pipeline: list[Any]) -> list[str]:
    constants: list[str] = []

    def visit(value: Any, *, in_match: bool = False, in_compare: bool = False) -> None:
        if isinstance(value, str):
            if (in_match or in_compare) and _is_groundable_string_constant(value):
                constants.append(value)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, in_match=in_match, in_compare=in_compare)
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
                visit(child, in_match=True)
                continue
            visit(
                child,
                in_match=in_match,
                in_compare=in_compare or key in _VALUE_COMPARISON_OPERATORS,
            )

    visit(pipeline)
    return sorted(set(constants))


def _is_groundable_string_constant(value: str) -> bool:
    return not value.startswith("$")


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
    if not all_paths:
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
        if _extends_observed_scalar_path(resolved, scalar_paths):
            unknown.append({"path": ref, "resolved_path": resolved})
            continue
        if _under_observed_complex_path(resolved, complex_paths) and not _has_evidence_path(
            resolved,
            all_paths,
        ):
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


def _collect_complex_paths(payload: Any, out: set[str]) -> None:
    _collect_type_paths(payload, set(), out, set())


def _collect_type_paths(
    payload: Any,
    scalar_paths: set[str],
    complex_paths: set[str],
    all_paths: set[str],
) -> None:
    if isinstance(payload, dict):
        path = payload.get("path")
        type_counts = payload.get("type_counts")
        if isinstance(path, str) and isinstance(type_counts, dict):
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
                if not isinstance(item_type_counts, dict):
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


def _resolve_debts(state: SmartEGState, milestone: str) -> None:
    for debt in state.evidence_ledger.debts.values():
        if debt.milestone == milestone:
            debt.resolved = True


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
