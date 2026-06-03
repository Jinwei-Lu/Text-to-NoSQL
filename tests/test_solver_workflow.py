from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tend.agents import AgentContext
from tend.config import Settings
from tend.errors import ExecutionError
from tend.execution.ast_check import parse_pipeline, scan_disabled
from tend.execution.mongo import MongoExecutor
from tend.llm import LLMClient
from tend.observability import setup_logging
from tend.solver.contracts import PhysicalPlan
from tend.solver.per_stage import (
    CheckpointCode,
    CheckpointSpec,
    PrefixExecutionRequest,
    _has_path,
    run_per_stage_check,
)
from tend.solver.guards import SolverBoundary
import tend.solver.workflow as solver_workflow
from tend.solver.workflow import (
    _MongoPrefixExecutor,
    _NoopPrefixExecutor,
    SolverFailure,
    load_solver_release_inputs,
    smart_solve_record,
)
from tend.stubs import stub_fn
from tend.workflow import Workflow


@pytest.fixture(scope="module")
def stub_settings() -> Settings:
    return Settings.from_env(overrides={"TEND_LLM_STUB": "1"}, run_id="solver-workflow-test")


@pytest.fixture()
def logger(tmp_path: Path):
    log = setup_logging(tmp_path / "run", console=False)
    try:
        yield log
    finally:
        log.close()


def test_solver_boundary_removes_gold_fields(stub_settings: Settings, logger) -> None:
    boundary = SolverBoundary.from_settings(stub_settings, logger=logger)
    record = {
        "record_id": 1001,
        "db_id": "financial",
        "nl_queries": {"canonical": "attach score"},
        "MQL": "db.x.aggregate([])",
        "canonical_form_set": {},
        "shape_policy": "preserve",
        "agent_design_rationale_ref": "audit/x",
    }

    safe = boundary.sanitize_test_record(record)

    assert safe == {
        "record_id": 1001,
        "db_id": "financial",
        "nl_queries": {"canonical": "attach score"},
    }


def test_load_solver_release_inputs_selects_nlq_tracks(stub_settings: Settings) -> None:
    dataset_dir = stub_settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    raw_record = json.loads((dataset_dir / "test.json").read_text(encoding="utf-8"))[0]
    canonical = raw_record["nl_queries"]["canonical"]
    colloquial = raw_record["nl_queries"]["colloquial"]

    default_record, _, _ = load_solver_release_inputs(
        dataset_dir,
        db_id="financial",
        record_id=1001,
        limit=1,
    )[0]
    canonical_record, _, _ = load_solver_release_inputs(
        dataset_dir,
        db_id="financial",
        record_id=1001,
        limit=1,
        nlq_track="canonical",
    )[0]
    colloquial_record, _, _ = load_solver_release_inputs(
        dataset_dir,
        db_id="financial",
        record_id=1001,
        limit=1,
        nlq_track="colloquial",
    )[0]

    assert default_record["nl_queries"] == raw_record["nl_queries"]
    assert canonical_record["nl_queries"] == {"canonical": canonical}
    assert colloquial_record["nl_queries"] == {"canonical": colloquial}
    assert canonical_record["nlq_track"] == "canonical"
    assert colloquial_record["nlq_track"] == "colloquial"


def test_smart_solver_stub_end_to_end(stub_settings: Settings, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    log = setup_logging(run_dir, console=False)
    client = LLMClient(stub_settings, log)
    client.set_stub(stub_fn)
    mongo = MongoExecutor(stub_settings, log)
    ctx = AgentContext(settings=stub_settings, llm=client, log=log, mongo=mongo)
    wf = Workflow(ctx)
    (record, schema, data) = load_solver_release_inputs(
        stub_settings.paths.repo_root / "tests" / "fixtures" / "smoke_release",
        db_id="financial",
        record_id=1001,
        limit=1,
    )[0]

    pred = asyncio.run(smart_solve_record(wf, record, schema, local_data=data))
    log.close()
    mongo.close()

    assert pred.db_id == "financial"
    assert pred.record_id == 1001
    assert pred.disclosure.backbone == "deepseek-v4-flash"
    assert pred.disclosure.no_training is True
    assert pred.disclosure.uses_train_json is False
    assert pred.logical_spec["shape_policy"] == "preserve"
    assert pred.physical_plan["variant_handling"]
    coll, pipeline = parse_pipeline(pred.MQL)
    assert coll == "account"
    assert len(pipeline) == 3
    assert scan_disabled(pred.MQL) == []
    assert (run_dir / "llm" / "smart_intent").is_dir()
    assert (run_dir / "llm" / "smart_plan").is_dir()

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert any(e["event"] == "smart_solver_start" for e in events)
    assert any(e["event"] == "solver_per_stage_prefix" for e in events)


def test_smart_solver_exhaustion_returns_typed_failure_without_dummy_mql(
    stub_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    log = setup_logging(run_dir, console=False)
    client = LLMClient(stub_settings, log)
    ctx = AgentContext(settings=stub_settings, llm=client, log=log)
    wf = Workflow(ctx)
    realization_calls: list[dict] = []

    async def fake_comprehend_shapes(_wf, _ctx, _nlq, _schema):
        return {
            "collections": {
                "account": {
                    "variants": [{"id": "default", "discriminator": {}}],
                    "field_locus": {},
                }
            },
            "coverage_gaps": [],
            "shape_flex_signature": [],
        }

    async def fake_agent(agent_id, inputs, *, ctx=None):
        if agent_id == "smart_intent":
            return {
                "entity": "account",
                "per": "account",
                "shape_policy": "preserve",
                "target_fields": ["score"],
                "output": {"target_fields": ["score"]},
            }
        if agent_id == "smart_plan":
            return {
                "collection": "account",
                "stages": [
                    {
                        "op": "$project",
                        "note": "drop target field",
                        "stage": {"$project": {"_id": 1}},
                    }
                ],
                "variant_handling": [],
            }
        raise AssertionError(agent_id)

    def fake_realize(_ctx, _boundary, **kwargs):
        realization_calls.append(kwargs)
        return {
            "ok": False,
            "mql": None,
            "feedback": {
                "error_code": "TARGET_FIELD_MISSING",
                "stage_index": 1,
                "failing_variant": "default",
                "suspect_field": "score",
                "message": "required target field is absent",
            },
        }

    monkeypatch.setattr(solver_workflow, "comprehend_shapes", fake_comprehend_shapes)
    monkeypatch.setattr(solver_workflow, "realize_plan_per_stage", fake_realize)
    wf.agent = fake_agent  # type: ignore[method-assign]

    try:
        result = asyncio.run(
            smart_solve_record(
                wf,
                {
                    "record_id": 7,
                    "db_id": "financial",
                    "nl_queries": {"canonical": "attach score"},
                },
                {"collections": {"account": {"_id": "INT"}}},
                local_data={"account": [{"_id": 1}]},
                r_max=1,
            )
        )
    finally:
        log.close()

    assert isinstance(result, SolverFailure)
    assert result.error_code == "SOLVER_EXHAUSTED"
    assert len(result.feedback) == 2
    assert [entry["attempt"] for entry in result.feedback] == [0, 1]
    assert len(realization_calls) == 2
    payload = result.to_json()
    assert "MQL" not in payload
    assert json.dumps(payload, default=str) != "[]"

    anomalies = [
        json.loads(line) for line in (run_dir / "anomalies.jsonl").read_text().splitlines()
    ]
    assert [entry["anomaly"] for entry in anomalies] == ["solver_exhausted"]


def test_required_fields_by_stage_treats_star_as_preserve_all_not_literal_field() -> None:
    stages = [
        {"$match": {"status": "active"}},
        {"$limit": 25},
    ]

    assert solver_workflow._required_fields_by_stage(stages, ["*"]) == {}


def test_required_fields_by_stage_tracks_each_target_field_materialization_independently() -> None:
    stages = [
        {"$addFields": {"segment": "$identity.segment"}},
        {"$project": {"segment": 1, "month_entries": {"$objectToArray": "$metrics"}}},
        {"$addFields": {"month": "$month_entries.k"}},
        {"$group": {"_id": {"segment": "$segment", "month": "$month"}}},
        {"$project": {"segment": "$_id.segment", "month": "$_id.month", "high_count": 1}},
    ]

    assert solver_workflow._required_fields_by_stage(
        stages,
        ["segment", "month", "high_count"],
    ) == {
        1: ("segment",),
        2: ("segment",),
        3: ("segment", "month"),
        4: ("segment", "month"),
        5: ("segment", "month", "high_count"),
    }


def test_required_fields_by_stage_for_reduce_checks_targets_only_at_final_output() -> None:
    stages = [
        {"$addFields": {"segment": "$identity.segment"}},
        {"$project": {"segment": 1, "month": "$month_entry.k"}},
        {"$group": {"_id": {"segment": "$segment", "month": "$month"}, "count": {"$sum": 1}}},
        {"$project": {"segment": "$_id.segment", "month": "$_id.month", "count": 1}},
    ]

    assert solver_workflow._required_fields_by_stage(
        stages,
        ["segment", "month", "count"],
        shape_policy="reduce",
    ) == {1: (), 2: (), 3: (), 4: ("segment", "month", "count")}


def test_realize_plan_per_stage_allows_reduce_queries_to_select_schema_variants(
    stub_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = setup_logging(tmp_path / "run", console=False)
    ctx = type(
        "Ctx",
        (),
        {
            "settings": stub_settings,
            "mongo": None,
            "log": log,
        },
    )()
    boundary = SolverBoundary.from_settings(stub_settings, logger=log)
    captured: dict[str, bool] = {}

    def fake_run_per_stage_check(**kwargs):
        captured["collapse_to_zero"] = kwargs["checkpoint"].collapse_to_zero
        return type(
            "Result",
            (),
            {
                "ok": True,
                "final_mql": kwargs["mql"],
                "feedback": None,
            },
        )()

    monkeypatch.setattr("tend.solver.per_stage.run_per_stage_check", fake_run_per_stage_check)
    try:
        result = solver_workflow.realize_plan_per_stage(
            ctx,
            boundary,
            db_id="superhero",
            plan=PhysicalPlan.from_json(
                {
                    "collection": "hero_dossiers",
                    "stages": [
                        {
                            "op": "$match",
                            "stage": {"$match": {"heroes_by_alignment": {"$exists": True}}},
                        }
                    ],
                }
            ),
            target_fields=[],
            shape_policy="reduce",
        )
    finally:
        log.close()

    assert result["ok"] is True
    assert captured == {"collapse_to_zero": False}


def test_solver_native_task_context_keeps_public_hints_without_gold_or_template_leakage() -> None:
    context = solver_workflow._solver_native_task_context(
        {
            "schema_flex": "dynamic_key",
            "schema_feature": "orders.by_month",
            "native_query_pattern": "orders.monthly_rollup",
            "MQL": "db.orders.aggregate([])",
            "canonical_form_set": {},
            "shape_policy": "reduce",
            "native_metadata": {
                "feature_id": "orders.by_month",
                "feature_type": "dynamic_key_object",
                "feature_field": "metrics.by_month",
                "query_pattern": "orders.monthly_rollup",
                "target_shape_policy": "reduce",
                "required_native_constructs": ["$objectToArray", "$group"],
                "mongo_native_constructs": ["$objectToArray", "$group", "$limit"],
                "anti_sql_transfer": {"level": "strong"},
                "compiler": "surgical_dataset_patch",
                "surgical_template_id": "orders.monthly_rollup",
                "surgical_patch_index": 4,
            },
        }
    )

    assert context["schema_flex"] == "dynamic_key"
    assert context["feature_field"] == "metrics.by_month"
    assert context["target_shape_policy"] == "reduce"
    assert context["required_native_constructs"] == ["$objectToArray", "$group"]
    serialized = json.dumps(context, ensure_ascii=False)
    assert "db.orders.aggregate" not in serialized
    assert "canonical_form_set" not in serialized
    assert "surgical_dataset_patch" not in serialized
    assert "surgical_template_id" not in serialized
    assert "surgical_patch_index" not in serialized


def test_native_context_focuses_solver_prompt_inputs_to_feature_collection() -> None:
    shape_model = {
        "collections": {
            "orders": {"variants": [{"id": "dynamic"}]},
            "customers": {"variants": [{"id": "root"}]},
        },
        "coverage_gaps": ["kept"],
    }
    witness_digest = {
        "orders": {"sample_count": 1},
        "customers": {"sample_count": 1},
    }
    native_context = {
        "feature_id": "orders.by_month",
        "feature_field": "metrics.by_month",
    }

    focused_shape = solver_workflow._focus_native_collection_shape_model(
        shape_model,
        native_context,
    )
    focused_witness = solver_workflow._focus_native_collection_witness_digest(
        witness_digest,
        native_context,
    )

    assert focused_shape["collections"] == {"orders": {"variants": [{"id": "dynamic"}]}}
    assert focused_shape["coverage_gaps"] == ["kept"]
    assert focused_witness == {"orders": {"sample_count": 1}}


def test_native_query_pattern_overrides_feature_id_when_focusing_solver_inputs() -> None:
    shape_model = {
        "collections": {
            "counterparty_flow_profiles": {
                "dynamic_key_paths": ["flows_by_symbol"],
                "dynamic_key_samples": {"flows_by_symbol": ["UVER"]},
                "field_locus": {"flows_by_symbol": []},
            },
            "district_market_contexts": {
                "dynamic_key_paths": [
                    "accounts_by_frequency",
                    "clients_by_gender",
                    "flows_by_symbol",
                ],
                "dynamic_key_samples": {
                    "accounts_by_frequency": ["POPLATEK_MESICNE"],
                    "clients_by_gender": ["F", "M"],
                    "flows_by_symbol": ["UVER"],
                },
                "array_paths": [
                    "accounts_by_frequency.*[]",
                    "clients_by_gender.*[]",
                    "flows_by_symbol.*[]",
                ],
                "field_locus": {
                    "accounts_by_frequency": [],
                    "clients_by_gender": [],
                    "flows_by_symbol": [],
                },
            },
        },
        "coverage_gaps": [],
    }
    witness_digest = {
        "counterparty_flow_profiles": {"sample_count": 1},
        "district_market_contexts": {"sample_count": 1},
    }
    native_context = {
        "feature_id": "counterparty_flow_profiles.bank_symbol_flow_matrix",
        "feature_field": "flows_by_symbol",
        "query_pattern": "financial.district_frequency_gender_loan_mix",
    }

    focused_shape = solver_workflow._focus_native_collection_shape_model(
        shape_model,
        native_context,
    )
    focused_witness = solver_workflow._focus_native_collection_witness_digest(
        witness_digest,
        native_context,
    )

    assert list(focused_shape["collections"]) == ["district_market_contexts"]
    collection = focused_shape["collections"]["district_market_contexts"]
    assert collection["dynamic_key_paths"] == ["accounts_by_frequency", "clients_by_gender"]
    assert "flows_by_symbol" not in collection["field_locus"]
    assert focused_witness == {"district_market_contexts": {"sample_count": 1}}


def test_native_query_pattern_normalizes_misleading_feature_field_for_agents() -> None:
    native_context = {
        "feature_id": "district_market_contexts.account_market_segments",
        "feature_field": "accounts_by_frequency",
        "query_pattern": "financial.loan_schedule",
        "target_shape_policy": "reduce",
    }

    agent_context = solver_workflow._solver_agent_native_task_context(native_context)

    assert agent_context["root_collection"] == "account_ledgers"
    assert agent_context["feature_field"] == "loan.repayment_schedule.by_due_month"
    assert agent_context["source_feature_field"] == "accounts_by_frequency"
    assert agent_context["relevant_paths"] == [
        "loan.repayment_schedule.by_due_month",
        "loan.contract",
        "district_context",
        "identity.service_plan",
    ]


def test_native_query_pattern_prunes_witness_digest_to_relevant_paths() -> None:
    witness_digest = {
        "account_ledgers": {
            "sample_count": 1,
            "sample_documents": [
                {
                    "_id": "acct-1",
                    "accounts_by_frequency": {"POPLATEK_MESICNE": [{"noise": True}]},
                    "cashflow": {"activity_by_month": {"1994-01": {"huge": "ignored"}}},
                    "identity": {
                        "account_id": 1,
                        "service_plan": {"frequency_key": "POPLATEK_MESICNE"},
                    },
                    "district_context": {
                        "region": "south Bohemia",
                        "avg_salary": 8968,
                    },
                    "loan": {
                        "contract": {"status_bucket": "completed_good"},
                        "repayment_schedule": {
                            "by_due_month": {
                                "1996-01": {
                                    "scheduled_amount": 100,
                                    "observed_payment_total": 90,
                                }
                            }
                        },
                        "observed_loan_flows": {"transactions_by_month": {"noise": []}},
                    },
                }
            ],
            "string_values_in_sample": {
                "district_context.region": ["south Bohemia"],
                "loan.contract.status_bucket": ["completed_good"],
            },
        },
        "district_market_contexts": {"sample_count": 1, "sample_documents": [{"noise": True}]},
    }
    native_context = {
        "feature_field": "accounts_by_frequency",
        "query_pattern": "financial.loan_schedule",
    }

    focused = solver_workflow._focus_native_collection_witness_digest(
        witness_digest,
        native_context,
    )

    assert list(focused) == ["account_ledgers"]
    sample = focused["account_ledgers"]["sample_documents"][0]
    assert sample == {
        "_id": "acct-1",
        "loan": {
            "repayment_schedule": {
                "by_due_month": {
                    "1996-01": {
                        "scheduled_amount": 100,
                        "observed_payment_total": 90,
                    }
                }
            },
            "contract": {"status_bucket": "completed_good"},
        },
        "district_context": {"region": "south Bohemia", "avg_salary": 8968},
        "identity": {"service_plan": {"frequency_key": "POPLATEK_MESICNE"}},
    }
    assert "accounts_by_frequency" not in sample
    assert "cashflow" not in sample


def test_shape_model_projects_db_structure_audit_into_sparse_native_collections() -> None:
    from tend.solver.agents import _collection_shape

    schema = {
        "collections": {
            "district_market_contexts": {
                "document_count": 77,
                "root_entity": "district banking market context",
                "source_tables": ["district", "account", "client", "loan"],
            }
        },
        "structure_audit": {
            "collection_counts": {"district_market_contexts": 77},
            "dynamic_key_paths": [
                {
                    "path": "accounts_by_frequency",
                    "sample_keys": ["POPLATEK_MESICNE", "POPLATEK_TYDNE"],
                    "document_count": 77,
                    "value_kinds": ["array"],
                },
                {
                    "path": "clients_by_gender",
                    "sample_keys": ["F", "M"],
                    "document_count": 77,
                    "value_kinds": ["array"],
                },
            ],
            "nested_array_paths": ["accounts_by_frequency.*[]", "clients_by_gender.*[]"],
            "dynamic_array_object_paths": ["accounts_by_frequency.*[]"],
            "array_object_dynamic_paths": [],
            "presence_state_counts": {"missing": 4, "present": 73},
        },
    }

    collections = solver_workflow._schema_collections(schema)
    fragment = _collection_shape(
        "district_market_contexts",
        collections["district_market_contexts"],
    )
    shape = fragment["collections"]["district_market_contexts"]

    assert "accounts_by_frequency" in shape["dynamic_key_paths"]
    assert "clients_by_gender" in shape["dynamic_key_paths"]
    assert shape["dynamic_key_samples"]["clients_by_gender"] == ["F", "M"]
    assert "accounts_by_frequency.*[]" in shape["array_paths"]
    assert "accounts_by_frequency.*[]" in shape["dynamic_array_object_paths"]
    assert shape["presence_state_counts"] == {"missing": 4, "present": 73}
    assert shape["doc_count"] == 77
    assert "document_count" not in shape["field_locus"]
    assert "accounts_by_frequency" in shape["field_locus"]


def test_smart_solver_passes_public_native_task_context_to_intent_and_planner(
    stub_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_dir = tmp_path / "run"
    log = setup_logging(run_dir, console=False)
    client = LLMClient(stub_settings, log)
    ctx = AgentContext(settings=stub_settings, llm=client, log=log)
    wf = Workflow(ctx)
    captured: dict[str, dict] = {}

    async def fake_comprehend_shapes(_wf, _ctx, _nlq, _schema):
        return {
            "collections": {
                "orders": {"variants": [], "field_locus": {}},
                "customers": {"variants": [], "field_locus": {}},
            }
        }

    async def fake_agent(agent_id, inputs, *, ctx=None):
        captured[agent_id] = dict(inputs)
        if agent_id == "smart_intent":
            return {
                "entity": "orders",
                "per": "month",
                "shape_policy": "reduce",
                "target_fields": ["month", "total"],
                "clause_coverage": ["monthly rollup"],
            }
        if agent_id == "smart_plan":
            return {
                "collection": "orders",
                "stages": [
                    {
                        "op": "$project",
                        "note": "emit test projection",
                        "stage": {"$project": {"_id": 0}},
                    }
                ],
                "variant_handling": [{"variant": "*", "strategy": "bounded"}],
            }
        raise AssertionError(agent_id)

    def fake_realize(_ctx, _boundary, **_kwargs):
        return {"ok": True, "mql": "db.orders.aggregate([])", "feedback": None}

    monkeypatch.setattr(solver_workflow, "comprehend_shapes", fake_comprehend_shapes)
    monkeypatch.setattr(solver_workflow, "realize_plan_per_stage", fake_realize)
    wf.agent = fake_agent  # type: ignore[method-assign]

    try:
        result = asyncio.run(
            smart_solve_record(
                wf,
                {
                    "record_id": 42,
                    "db_id": "shop",
                    "nl_queries": {"canonical": "Summarize monthly orders."},
                    "native_metadata": {
                        "feature_id": "orders.by_month",
                        "feature_type": "dynamic_key_object",
                        "feature_field": "metrics.by_month",
                        "query_pattern": "orders.monthly_rollup",
                        "target_shape_policy": "reduce",
                        "compiler": "surgical_dataset_patch",
                    },
                },
                {"collections": {"orders": {"fields": ["metrics"]}}},
                local_data={"orders": [{"_id": 1}], "customers": [{"_id": "c1"}]},
                r_max=0,
            )
        )
    finally:
        log.close()

    assert not isinstance(result, SolverFailure)
    assert captured["smart_intent"]["native_task_context"]["feature_field"] == "metrics.by_month"
    assert captured["smart_plan"]["native_task_context"]["feature_field"] == "metrics.by_month"
    assert captured["smart_intent"]["shape_model"]["collections"].keys() == {"orders"}
    assert captured["smart_plan"]["shape_model"]["collections"].keys() == {"orders"}
    assert captured["smart_plan"]["witness_digest"].keys() == {"orders"}
    assert "compiler" not in captured["smart_plan"]["native_task_context"]


def test_build_witness_digest_compacts_large_nested_documents() -> None:
    digest = solver_workflow.build_witness_digest(
        {
            "large_docs": [
                {
                    "_id": "doc:1",
                    "payload": {
                        "long_text": "x" * 500,
                        "items": [{"name": f"item-{i}", "value": i} for i in range(20)],
                    },
                }
            ]
        },
        witness_k=1,
    )

    preview = digest["large_docs"]["sample_documents"][0]
    assert preview["payload"]["long_text"].endswith("...")
    assert len(preview["payload"]["long_text"]) < 200
    assert len(preview["payload"]["items"]) < 20
    assert preview["payload"]["items"][-1]["__truncated_items__"] == 12


def test_mongo_prefix_executor_stratifies_shape_variants_for_feedback() -> None:
    class FakeMongo:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def norm_exec(self, _db_id: str, mql: str) -> list[dict]:
            self.calls.append(mql)
            _collection, pipeline = parse_pipeline(mql)
            selector = pipeline[0]["$match"]
            if selector == {"loan": {"$exists": True}}:
                return [{"_id": 1, "score": 10}]
            if selector == {"loan": {"$exists": False}}:
                return [{"_id": 2}]
            raise AssertionError(selector)

    fake_mongo = FakeMongo()
    executor = _MongoPrefixExecutor(
        fake_mongo,
        shape_model={
            "collections": {
                "account": {
                    "variants": [
                        {"id": "loan-present", "discriminator": {"loan": "present"}},
                        {"id": "loan-missing", "discriminator": {"loan": "missing"}},
                    ]
                }
            }
        },
        local_data={"account": [{"_id": 1, "loan": {"amount": 10}}, {"_id": 2}]},
    )

    result = run_per_stage_check(
        db_id="financial",
        mql='db.account.aggregate([{"$addFields":{"score":"$loan.amount"}}])',
        executor=executor,
        checkpoint=CheckpointSpec(required_fields_by_stage={1: ("score",)}),
    )

    assert result.ok is False
    assert result.feedback is not None
    assert result.feedback.error_code == CheckpointCode.TARGET_FIELD_MISSING
    assert result.feedback.failing_variant == "loan-missing"
    assert result.feedback.suspect_field == "score"
    assert len(fake_mongo.calls) == 2
    variants = result.feedback.context["variants"]
    assert [variant["variant"] for variant in variants] == ["loan-present", "loan-missing"]


def test_mongo_prefix_executor_bounds_prefix_input_before_generated_pipeline() -> None:
    class FakeMongo:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def norm_exec(self, _db_id: str, mql: str) -> list[dict]:
            self.calls.append(mql)
            _collection, pipeline = parse_pipeline(mql)
            assert "$limit" in pipeline[0]
            assert pipeline[1] == {"$facet": {"all_docs": [{"$project": {"_id": 1}}]}}
            assert "$limit" in pipeline[2]
            return [{"all_docs": [{"_id": 1}]}]

        def count(self, _db_id: str, _collection: str) -> int:
            return 100_000

    executor = _MongoPrefixExecutor(FakeMongo(), local_data={"account": [{"_id": 1}]})

    result = run_per_stage_check(
        db_id="financial",
        mql='db.account.aggregate([{"$facet":{"all_docs":[{"$project":{"_id":1}}]}}])',
        executor=executor,
    )

    assert result.ok is True


def test_whole_query_transient_exec_error_is_retryable_feedback(
    tmp_path: Path,
) -> None:
    """[H1] A whole_query realization that hits a transient Mongo exec exception returns
    retryable feedback (boundary_failure falsy) so smart_solve_record retries it,
    instead of crashing with an unhandled exception."""
    settings = Settings.from_env(
        overrides={"TEND_LLM_STUB": "0"},
        run_id="solver-h1-transient",
        require_bird=False,
    )
    assert settings.stub is False
    log = setup_logging(tmp_path / "run", console=False)

    class FlakyMongo:
        def available(self) -> bool:
            return True

        def norm_exec(self, db_id: str, mql: str):
            raise ExecutionError("transient executor timeout", context={"db_id": db_id})

    ctx = type(
        "Ctx",
        (),
        {"settings": settings, "mongo": FlakyMongo(), "log": log},
    )()
    boundary = SolverBoundary.from_settings(settings, logger=log)
    plan = PhysicalPlan.from_json(
        {
            "collection": "orders",
            "stages": [
                {"op": "$match", "note": "filter", "stage": {"$match": {"status": "active"}}},
            ],
        }
    )

    try:
        result = solver_workflow.realize_plan_whole_query(
            ctx, boundary, db_id="shop", plan=plan, attempt=2
        )
    finally:
        log.close()

    assert result["ok"] is False
    assert result["mql"] is None
    feedback = result["feedback"]
    assert feedback["error_code"] == "EXEC_ERROR"
    # The fix: transient exec errors are retryable, not terminal boundary failures.
    assert not feedback["boundary_failure"]
    assert feedback["attempt"] == 2
    assert "transient executor timeout" in feedback["message"]


def test_whole_query_disabled_operator_stays_terminal_boundary_failure(
    tmp_path: Path,
) -> None:
    """[H1] A disabled operator in whole_query mode stays a terminal boundary failure
    (boundary_failure truthy), unlike a transient exec error."""
    settings = Settings.from_env(
        overrides={"TEND_LLM_STUB": "0"},
        run_id="solver-h1-disabled",
        require_bird=False,
    )
    log = setup_logging(tmp_path / "run", console=False)

    class NeverCalledMongo:
        def available(self) -> bool:  # pragma: no cover - guard never reaches exec
            return True

        def norm_exec(self, db_id: str, mql: str):  # pragma: no cover
            raise AssertionError("disabled operator must short-circuit before exec")

    ctx = type(
        "Ctx",
        (),
        {"settings": settings, "mongo": NeverCalledMongo(), "log": log},
    )()
    boundary = SolverBoundary.from_settings(settings, logger=log)
    plan = PhysicalPlan.from_json(
        {
            "collection": "orders",
            "stages": [
                {"op": "$merge", "note": "banned", "stage": {"$merge": {"into": "sink"}}},
            ],
        }
    )

    try:
        result = solver_workflow.realize_plan_whole_query(
            ctx, boundary, db_id="shop", plan=plan, attempt=0
        )
    finally:
        log.close()

    assert result["ok"] is False
    assert result["feedback"]["boundary_failure"] is True


def test_realize_plan_per_stage_disabled_operator_returns_structured_boundary_failure(
    stub_settings: Settings,
    tmp_path: Path,
) -> None:
    """[H2] realize_plan_per_stage with an MQL that trips assert_no_disabled returns a
    structured boundary-failure dict (no unhandled exception bubbling out)."""
    log = setup_logging(tmp_path / "run", console=False)
    ctx = type(
        "Ctx",
        (),
        {"settings": stub_settings, "mongo": None, "log": log},
    )()
    boundary = SolverBoundary.from_settings(stub_settings, logger=log)
    plan = PhysicalPlan.from_json(
        {
            "collection": "orders",
            "stages": [
                {"op": "$merge", "note": "banned sink", "stage": {"$merge": {"into": "sink"}}},
            ],
        }
    )

    try:
        result = solver_workflow.realize_plan_per_stage(
            ctx,
            boundary,
            db_id="shop",
            plan=plan,
            target_fields=[],
            shape_policy="reshape",
        )
    finally:
        log.close()

    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["mql"] is None
    feedback = result["feedback"]
    assert feedback is not None
    assert feedback["error_code"] == CheckpointCode.DISABLED_OPERATOR.value
    assert feedback["suspect_field"] == "$merge"


def test_noop_prefix_executor_materializes_dotted_target_field_for_has_path() -> None:
    """[H7] In stub mode the _NoopPrefixExecutor must synthesize a doc where a dotted
    target field (e.g. 'timeline.events') is nested so per_stage._has_path finds it
    (not reported missing)."""
    executor = _NoopPrefixExecutor({1: ("timeline.events", "name")})
    request = PrefixExecutionRequest(
        db_id="shop",
        collection="dossiers",
        stage_index=1,
        stage={"$project": {"_id": 0}},
        pipeline=(),
        mql="db.dossiers.aggregate([])",
    )

    result = executor.execute_prefix(request)
    doc = result.variants[0].documents[0]

    # The dotted field must be set as a nested object, not a literal "timeline.events" key.
    assert doc.get("timeline") == {"events": 1}
    assert _has_path(doc, "timeline.events") is True
    assert _has_path(doc, "name") is True
    # Sanity: a genuinely absent path is still reported missing.
    assert _has_path(doc, "timeline.missing") is False


def test_prune_native_shape_collection_with_no_matching_hint_yields_empty_lists() -> None:
    """[F2] _prune_shape_collection_for_native_context with non-empty hints that match
    nothing must prune to EMPTY lists, not leak the full unfiltered collection shape."""
    collection_shape = {
        "dynamic_key_paths": ["flows_by_symbol", "accounts_by_frequency"],
        "array_paths": ["flows_by_symbol.*[]", "accounts_by_frequency.*[]"],
        "dynamic_array_object_paths": ["flows_by_symbol.*[]"],
        "array_object_dynamic_paths": ["accounts_by_frequency.*[].entries"],
        "dynamic_key_samples": {
            "flows_by_symbol": ["UVER"],
            "accounts_by_frequency": ["POPLATEK_MESICNE"],
        },
        "field_locus": {
            "flows_by_symbol": [],
            "accounts_by_frequency": [],
        },
    }
    native_context = {"feature_field": "unrelated.path.with.no.match"}

    # Precondition: the hint is non-empty (so the prune branch is exercised).
    assert solver_workflow._native_query_path_hints(native_context) == (
        "unrelated.path.with.no.match",
    )

    pruned = solver_workflow._prune_shape_collection_for_native_context(
        collection_shape, native_context
    )

    assert pruned["dynamic_key_paths"] == []
    assert pruned["array_paths"] == []
    assert pruned["dynamic_array_object_paths"] == []
    assert pruned["array_object_dynamic_paths"] == []
    assert pruned["dynamic_key_samples"] == {}
    assert pruned["field_locus"] == {}
