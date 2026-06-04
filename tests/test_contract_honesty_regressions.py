from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tend.ablations import ABLATION_IDS
from tend.ablations.strategies import resolve_ablations
from tend.ablations.workflow import (
    _failure_from_solver_payload,
    _prediction_from_solver_payload,
    _runtime_options,
)
from tend.agents import AgentContext
from tend.baselines.boundary import sanitize_public_record, sanitize_public_schema
from tend.baselines.strategies import BaselinePromptContext, _plan_messages
from tend.config import Settings
from tend.evaluation import EVALUATION_METRICS, evaluate_predictions
from tend.execution.ast_check import parse_pipeline
from tend.errors import SourceError
from tend.llm import LLMClient
from tend.observability import setup_logging
from tend.solver.eg import SmartEGPolicy
from tend.solver.eg.contracts import SmartEGState
from tend.solver.eg.tools import PREFIX_EXECUTION_TOOLS, SmartEGToolAPI
from tend.workflow import Workflow


def _settings(tmp_path: Path, run_id: str = "contract-honesty-test") -> Settings:
    return Settings.from_env(
        overrides={
            "TEND_LLM_STUB": "1",
            "TEND_RUN_DIR": str(tmp_path / "run"),
        },
        run_id=run_id,
        require_bird=False,
    )


def _workflow(tmp_path: Path) -> tuple[Workflow, Any]:
    settings = _settings(tmp_path)
    log = setup_logging(settings.run_dir, console=False)
    ctx = AgentContext(settings=settings, llm=LLMClient(settings, log), log=log)
    return Workflow(ctx), log


def _tool_call(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": f"call-{name}",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments or {}),
        },
    }


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def test_first_turn_final_submit_and_prefix_tools_have_truthful_outcomes() -> None:
    api = SmartEGToolAPI(SmartEGPolicy())
    first_turn = SmartEGState(nlq="list accounts", db_id="financial", record_id=1001)

    first_turn_tools = {
        tool["function"]["name"] for tool in api.tools_for_state(first_turn)
    }
    assert "submit_final_mql" not in first_turn_tools

    final_observation = api.execute(
        _tool_call(
            "submit_final_mql",
            {
                "collection": "account",
                "pipeline": [{"$limit": 1}],
                "MQL": 'db.account.aggregate([{"$limit":1}])',
            },
        ),
        first_turn,
        exposed_tool_names=first_turn_tools,
    )

    assert final_observation.ok is False
    assert final_observation.result == {"reason": "tool_not_exposed"}
    assert final_observation.llm_visible_content["ok"] is False
    assert first_turn.result is None

    execution_state = SmartEGState(
        nlq="list accounts",
        db_id="financial",
        record_id=1001,
        mode="execution",
    )
    execution_tools = {
        tool["function"]["name"] for tool in api.tools_for_state(execution_state)
    }
    assert PREFIX_EXECUTION_TOOLS <= execution_tools

    render_observation = api.execute(
        _tool_call(
            "render_pipeline_prefix",
            {
                "collection": "account",
                "pipeline": [{"$limit": 1}],
            },
        ),
        execution_state,
        exposed_tool_names=execution_tools,
    )
    assert render_observation.ok is True
    assert render_observation.llm_visible_content["ok"] is True
    assert render_observation.result["prefix_length"] == 1
    assert render_observation.result["MQL"] == 'db.account.aggregate([{"$limit":1}])'

    for tool_name in sorted(PREFIX_EXECUTION_TOOLS - {"render_pipeline_prefix"}):
        observation = api.execute(
            _tool_call(
                tool_name,
                {
                    "collection": "account",
                    "pipeline": [{"$limit": 1}],
                },
            ),
            execution_state,
            exposed_tool_names=execution_tools,
        )
        assert observation.ok is False, tool_name
        assert observation.llm_visible_content["ok"] is False, tool_name
        assert observation.result["reason"] == "unsupported_prefix_executor", tool_name


def test_sanitized_baseline_prompt_excludes_private_schema_and_gold_metadata() -> None:
    raw_record = {
        "db_id": "financial",
        "record_id": 1001,
        "nl_queries": {
            "canonical": "List account identifiers.",
            "paraphrase": "SECRET_PARAPHRASE_SHOULD_NOT_LEAK",
        },
        "MQL": "SECRET_GOLD_MQL_SHOULD_NOT_LEAK",
        "canonical_form_set": {"must": ["SECRET_CANONICAL_SHOULD_NOT_LEAK"]},
        "shape_policy": "SECRET_SHAPE_POLICY_SHOULD_NOT_LEAK",
        "native_verification": {"trace": "SECRET_NATIVE_VERIFY_SHOULD_NOT_LEAK"},
        "construction_model_id": "qps-construction-only",
        "qps_trace": "SECRET_QPS_TRACE_SHOULD_NOT_LEAK",
    }
    raw_schema = {
        "db_id": "financial",
        "structure_audit": {"private": "SECRET_STRUCTURE_AUDIT_SHOULD_NOT_LEAK"},
        "structure_gate": {"private": "SECRET_STRUCTURE_GATE_SHOULD_NOT_LEAK"},
        "collections": {
            "account": {
                "document_count": 2,
                "fields": {"account_id": "INT", "district_id": "INT"},
                "__variants": [
                    {
                        "discriminator": {"loan": "present"},
                        "fields": {"loan": "OBJECT"},
                        "coverage": "SECRET_COVERAGE_SHOULD_NOT_LEAK",
                        "source_signal": "SECRET_SOURCE_SIGNAL_SHOULD_NOT_LEAK",
                    }
                ],
                "source_tables": ["account"],
                "canonical_form_set": "SECRET_SCHEMA_CANONICAL_SHOULD_NOT_LEAK",
                "MQL": "SECRET_SCHEMA_GOLD_MQL_SHOULD_NOT_LEAK",
                "native_metadata": {"builder": "SECRET_NATIVE_META_SHOULD_NOT_LEAK"},
                "native_verification": {"ok": True},
                "dynamic_key_samples": {"loan": ["SECRET_DYNAMIC_SAMPLE"]},
                "presence_state_counts": {"loan": {"present": 1}},
                "collection_counts": {"account": 2},
            }
        },
    }

    sanitized_record = sanitize_public_record(raw_record)
    sanitized_schema = sanitize_public_schema(raw_schema)
    ctx = BaselinePromptContext(
        record=sanitized_record.value,
        schema=sanitized_schema.value,
        witness_digest={"collections": {"account": {"sample_count": 2}}},
        schema_summary=sanitized_schema.value,
        nlq=sanitized_record.value["nl_queries"]["canonical"],
    )
    prompt_text = "\n".join(
        str(message.get("content", "")) for message in _plan_messages(ctx, {})
    )
    combined = _json_text(
        {
            "record": sanitized_record.value,
            "schema": sanitized_schema.value,
            "prompt": prompt_text,
        }
    )

    assert sanitized_schema.value["public_schema_version"] == "baseline_public_schema_v1"
    assert sanitized_schema.value["collections"]["account"]["variants"] == [
        {
            "discriminator": {"loan": "present"},
            "fields": {"loan": "OBJECT"},
        }
    ]
    assert "structure_audit" in sanitized_schema.stripped_fields
    assert "structure_gate" in sanitized_schema.stripped_fields
    assert "MQL" in sanitized_record.stripped_fields
    assert "canonical_form_set" in sanitized_record.stripped_fields

    forbidden_fragments = [
        "SECRET_",
        "structure_audit",
        "structure_gate",
        "canonical_form_set",
        "shape_policy",
        "native_verification",
        "native_metadata",
        "construction_model_id",
        "qps_trace",
        "source_tables",
        "dynamic_key_samples",
        "presence_state_counts",
        "collection_counts",
        "coverage",
        "source_signal",
        "SECRET_GOLD_MQL_SHOULD_NOT_LEAK",
        "SECRET_SCHEMA_GOLD_MQL_SHOULD_NOT_LEAK",
    ]
    for fragment in forbidden_fragments:
        assert fragment not in combined


def test_registered_ablations_are_behavior_toggles_or_labelled_budget_profiles() -> None:
    assert "smart_eg_no_probe_scheduler" not in ABLATION_IDS
    with pytest.raises(SourceError, match="smart_eg_no_probe_scheduler"):
        resolve_ablations("smart_eg_no_probe_scheduler")

    behavior_flags = {
        "use_evidence_gate",
        "use_counterexample",
        "use_value_grounding",
        "use_relationship_probe",
        "use_prefix_execution",
        "use_revisit",
    }
    specs = resolve_ablations("all")

    assert {spec.id for spec in specs} == set(ABLATION_IDS)
    for spec in specs:
        options = spec.to_runtime_options()
        assert options["ablation_id"] == spec.id
        assert options["solver_variant"] == spec.id

        if spec.id.startswith("smart_eg_budget_"):
            profile = spec.id.removeprefix("smart_eg_budget_")
            assert options["budget_profile"] == profile
            assert spec.title == f"{profile.title()} budget profile"
            assert "budget profile" in spec.description
            assert options["cost_budget_usd_source"] == "provider_cost_usd_if_available"
            assert (
                options["cost_budget_usd_unpriced_behavior"]
                == "advisory_when_unpriced"
            )
            continue

        disabled = {key for key in behavior_flags if options[key] is False}
        if spec.id == "smart_eg_full":
            assert not disabled
            assert options["budget_profile"] == "full"
        else:
            assert len(disabled) == 1, spec.id
            assert spec.limitations


class _ExactGoldExecutor:
    def available(self) -> bool:
        return True

    def load_witness(self, db_id: str, collections: dict[str, list[dict[str, Any]]]) -> None:
        assert db_id == "financial"
        assert collections["account"]

    def norm_exec(self, db_id: str, mql: str) -> list[dict[str, Any]]:
        assert db_id == "financial"
        parse_pipeline(mql)
        return [{"account_id": 1}]


def test_typed_runtime_failure_rows_are_failed_and_make_report_partial(
    tmp_path: Path,
) -> None:
    dataset_dir = tmp_path / "release"
    (dataset_dir / "mongodb_data").mkdir(parents=True)
    record = {
        "record_id": 1001,
        "db_id": "financial",
        "domain_id": "financial",
        "difficulty_tier": "L3",
        "join_depth": 1,
        "aggregation_depth": "simple",
        "schema_pattern": "native_document",
        "schema_flex": "medium",
        "functional_sql_solvable": True,
        "structural_sql_solvable": True,
        "sql_infeasibility_class": "none",
        "MQL": "db.account.aggregate([])",
        "canonical_form_set": {},
    }
    (dataset_dir / "test.json").write_text(json.dumps([record]), encoding="utf-8")
    (dataset_dir / "mongodb_data" / "financial.json").write_text(
        json.dumps({"account": [{"account_id": 1}]}),
        encoding="utf-8",
    )
    predictions_path = tmp_path / "ablation_predictions.jsonl"
    predictions_path.write_text(
        json.dumps(
            {
                "result_type": "ablation_failure",
                "status": "failed",
                "ablation_id": "smart_eg_no_revisit",
                "session_id": "solve-ablation-0-smart_eg_no_revisit-financial-1001",
                "batch_index": 0,
                "work_item_id": "ablation:0:smart_eg_no_revisit:financial:1001",
                "record_id": 1001,
                "db_id": "financial",
                "error_code": "EXECUTION_UNRESOLVED",
                "message": "solver reported a typed runtime failure",
                "transcript_refs": ["llm/ablation.md"],
                "diagnostics_refs": ["llm/ablation.diagnostics.json"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    log = setup_logging(tmp_path / "eval-run", console=False)
    try:
        output = evaluate_predictions(
            dataset_dir=dataset_dir,
            predictions_path=predictions_path,
            out_dir=tmp_path / "eval",
            experiment_kind="ablation",
            run_id="eval-contract-honesty",
            logger=log,
            progress=None,
            executor=_ExactGoldExecutor(),
            max_workers=1,
        )
    finally:
        log.close()

    rows = [
        json.loads(line)
        for line in output.paths.per_record_jsonl.read_text(encoding="utf-8").splitlines()
    ]

    assert output.status == "partial"
    assert output.report["status"] == "partial"
    assert output.report["diagnostics"]["record_failed"] == 1
    assert output.report["diagnostics"]["ablation_failure"] == 1
    assert output.report["diagnostics"]["EXECUTION_UNRESOLVED"] == 1
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["metrics"] == dict.fromkeys(EVALUATION_METRICS, 0)
    assert rows[0]["diagnostics"] == {
        "error_code": "EXECUTION_UNRESOLVED",
        "failure_type": "ablation_failure",
        "message": "solver reported a typed runtime failure",
    }
    assert rows[0]["prediction_ref"] == {
        "line": 1,
        "work_item_id": "ablation:0:smart_eg_no_revisit:financial:1001",
        "batch_index": 0,
        "result_type": "ablation_failure",
        "transcript_refs": ["llm/ablation.md"],
        "diagnostics_refs": ["llm/ablation.diagnostics.json"],
    }


def test_ablation_prediction_and_failure_rows_keep_traceability_refs(
    tmp_path: Path,
) -> None:
    wf, log = _workflow(tmp_path)
    spec = resolve_ablations("smart_eg_budget_low")[0]
    options = _runtime_options(
        spec,
        max_tool_turns=48,
        max_revisits=2,
        cost_budget_usd=1.0,
        batch_index=3,
        db_id="financial",
        record_id=1001,
    )
    prediction_payload = {
        "result_type": "solver_prediction",
        "record_id": 1001,
        "db_id": "financial",
        "MQL": "db.account.aggregate([])",
        "attempts": 4,
        "environment_model_ref": "agent/session.jsonl#environment",
        "intent_ref": "agent/session.jsonl#intent",
        "query_plan_ref": "agent/session.jsonl#plan",
        "execution_trace_ref": "execution_trace.jsonl",
        "evidence_ledger_ref": "evidence_ledger.jsonl",
        "agent_session_ref": "agent/session.md",
        "submit_gate_refs": ["submit_gates.jsonl#1"],
        "disclosure": {"budget_profile": "low"},
    }
    failure_payload = {
        "result_type": "solver_failure",
        "record_id": 1001,
        "db_id": "financial",
        "error_code": "EXECUTION_UNRESOLVED",
        "message": "final execution could not be resolved",
        "attempts": 2,
        "last_candidate_ref": "execution_trace.jsonl#candidate-1",
        "unresolved_debts": ["debt-final"],
        "evidence_ledger_ref": "evidence_ledger.jsonl",
        "execution_trace_ref": "execution_trace.jsonl",
        "agent_session_ref": "agent/session.md",
        "disclosure": {"budget_profile": "low"},
    }

    try:
        prediction = _prediction_from_solver_payload(
            wf,
            spec,
            options,
            prediction_payload,
            local_data=None,
            transcript_refs=["llm/prediction.md"],
            diagnostics_refs=["llm/prediction.diagnostics.json"],
        ).to_json()
        failure = _failure_from_solver_payload(
            wf,
            spec,
            options,
            failure_payload,
            local_data=None,
            transcript_refs=["llm/failure.md"],
            diagnostics_refs=["llm/failure.diagnostics.json"],
        ).to_json()
    finally:
        log.close()

    for row in (prediction, failure):
        assert row["session_id"]
        assert row["session_id"] == options["session_id"]
        assert row["ablation_id"] == "smart_eg_budget_low"
        assert row["batch_index"] == 3
        assert row["work_item_id"] == "ablation:3:smart_eg_budget_low:financial:1001"
        assert row["record_id"] == 1001
        assert row["db_id"] == "financial"
        assert row["disclosure"]["budget_profile"] == "low"
        assert row["disclosure"]["options"]["budget_profile"] == "low"
        assert row["disclosure"]["cost_budget_usd_source"] == (
            "provider_cost_usd_if_available"
        )
        assert row["disclosure"]["cost_budget_usd_unpriced_behavior"] == (
            "advisory_when_unpriced"
        )
        assert row["evidence_ledger_ref"] == "evidence_ledger.jsonl"
        assert row["execution_trace_ref"] == "execution_trace.jsonl"
        assert row["agent_session_ref"] == "agent/session.md"

    assert prediction["result_type"] == "ablation_prediction"
    assert prediction["status"] == "ok"
    assert prediction["transcript_refs"] == ["llm/prediction.md"]
    assert prediction["diagnostics_refs"] == ["llm/prediction.diagnostics.json"]
    assert prediction["submit_gate_refs"] == ["submit_gates.jsonl#1"]

    assert failure["result_type"] == "ablation_failure"
    assert failure["status"] == "failed"
    assert failure["transcript_refs"] == ["llm/failure.md"]
    assert failure["diagnostics_refs"] == ["llm/failure.diagnostics.json"]
    assert failure["last_candidate_ref"] == "execution_trace.jsonl#candidate-1"
    assert failure["unresolved_debts"] == ["debt-final"]
