"""Provider-native tool exposure and deterministic gates for SMART-EG."""
from __future__ import annotations

import json
from typing import Any

from .contracts import (
    GateViolation,
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
            names = ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | {
                "submit_intent_hypothesis",
                "request_revisit",
                "request_mode_shift",
                "abandon_with_failure",
            }
        elif state.mode == "planning":
            names = ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | {
                "render_pipeline",
                "check_ast_filter",
                "submit_query_plan",
                "request_revisit",
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
            return {"type": "function", "function": {"name": _terminal_submit_tool_for_mode(state.mode)}}
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
        observation_ref = self.observer.agent_event(
            "tool_observation",
            {"tool": "list_collections"},
        ) if self.observer else ""
        record = state.evidence_ledger.add_record(
            source_tool="list_collections",
            tool_call_id=call_id,
            observation_ref=observation_ref,
            summary=summary,
            supports_claims=[],
            redaction={"raw_rows": False},
        )
        if self.observer:
            self.observer.record_evidence(record.to_json())
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
        observation_ref = self.observer.agent_event(
            "tool_observation",
            {"tool": name},
        ) if self.observer else ""
        record = state.evidence_ledger.add_record(
            source_tool=name,
            tool_call_id=call_id,
            observation_ref=observation_ref,
            summary=summary,
            supports_claims=[],
            redaction=summary.get("redaction", {"raw_rows": False}),
        )
        if self.observer:
            self.observer.record_evidence(record.to_json())
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
    return {_terminal_submit_tool_for_mode(state.mode), "abandon_with_failure"}


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


def _known_tool_names() -> set[str]:
    return ENVIRONMENT_TOOLS | EVIDENCE_TOOLS | EXECUTION_TOOLS | TERMINAL_TOOLS | STAGE_CONTROL_TOOLS


def _refs(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    return [str(item) for item in payload.get("evidence_refs") or []]


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
