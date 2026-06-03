from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from tend.solver.agents import SmartIntentFormalizer
from tend.solver.agents import SmartNosqlPlanner
from tend.solver.agents import _observed_literal_violations
from tend.solver.guards import build_disclosure, check_disjointness


def _settings(model: str, *, stub: bool = False):
    llm = SimpleNamespace(model=model, agent_models={})
    return SimpleNamespace(llm=llm, stub=stub)


def _allow_list(
    *,
    construction_model_ids: list[str] | None = None,
    frozen_panels: dict[str, list[str]] | None = None,
) -> dict:
    construction = (
        construction_model_ids
        if construction_model_ids is not None
        else ["construction-model-a"]
    )
    panels = frozen_panels if frozen_panels is not None else {
        "small": ["small-model-a"],
        "medium": ["medium-model-a"],
        "large": ["large-model-a"],
        "frontier": ["frontier-model-a"],
    }
    return {
        "four_party_disjointness": {"construction_model_ids": construction},
        "frozen_panels": panels,
    }


@pytest.mark.parametrize(
    ("model", "allow_list", "hit_key", "expected_hit"),
    [
        (
            "deepseek-v4-flash",
            _allow_list(construction_model_ids=["deepseek-v4-flash"]),
            "construction_pool_hits",
            "deepseek-v4-flash",
        ),
        (
            "claude-4-opus",
            _allow_list(frozen_panels={
                "small": ["small-model-a"],
                "medium": ["medium-model-a"],
                "large": ["large-model-a"],
                "frontier": ["claude-4-opus"],
            }),
            "frozen_panel_hits",
            "claude-4-opus",
        ),
    ],
)
def test_reused_construction_or_frozen_model_marks_disjointness_not_ok(
    model: str,
    allow_list: dict,
    hit_key: str,
    expected_hit: str,
) -> None:
    disclosure = build_disclosure(_settings(model), allow_list, r_max=2, witness_k=3)

    assert disclosure.disjointness_ok is False
    assert disclosure.disjointness_detail[hit_key] == [expected_hit]
    assert disclosure.disjointness_detail["manifest_errors"] == []


@pytest.mark.parametrize(
    "allow_list",
    [
        {"frozen_panels": _allow_list()["frozen_panels"]},
        _allow_list(construction_model_ids=[]),
    ],
)
def test_non_stub_disjointness_fails_closed_for_missing_or_empty_construction_manifest(
    allow_list: dict,
) -> None:
    detail = check_disjointness(["solver-model-a"], allow_list, require_manifests=True)

    assert detail["ok"] is False
    assert any("construction_model_ids" in error for error in detail["manifest_errors"])


@pytest.mark.parametrize(
    "frozen_panels",
    [
        None,
        {},
        {
            "small": ["small-model-a"],
            "medium": [],
            "large": ["large-model-a"],
            "frontier": ["frontier-model-a"],
        },
    ],
)
def test_non_stub_disjointness_fails_closed_for_missing_or_empty_frozen_panels(
    frozen_panels: dict[str, list[str]] | None,
) -> None:
    allow_list = _allow_list(frozen_panels=frozen_panels)
    if frozen_panels is None:
        allow_list.pop("frozen_panels")

    detail = check_disjointness(["solver-model-a"], allow_list, require_manifests=True)

    assert detail["ok"] is False
    assert any("frozen_panels" in error for error in detail["manifest_errors"])


def test_non_stub_disjointness_rejects_construction_role_labels_as_manifest() -> None:
    detail = check_disjointness(
        ["solver-model-a"],
        _allow_list(construction_model_ids=["QPS", "MS"]),
        require_manifests=True,
    )

    assert detail["ok"] is False
    assert any("role labels" in error for error in detail["manifest_errors"])


def test_planner_schema_and_contract_require_stage_diagnostics() -> None:
    output = {
        "collection": "account",
        "stages": [{"op": "$match", "stage": {"$match": {"status": "active"}}}],
        "variant_handling": [],
    }

    schema_errors = list(
        Draft202012Validator(SmartNosqlPlanner.output_schema).iter_errors(output)
    )
    violations = SmartNosqlPlanner().check_contract(
        None,
        {"logical_spec": {"shape_policy": "reshape"}, "shape_model": {}},
        output,
    )

    assert schema_errors
    assert any("stage 0 must include" in violation for violation in violations)


def test_planner_contract_accepts_structured_stage_rationale() -> None:
    output = {
        "collection": "account",
        "stages": [
            {
                "op": "$match",
                "stage": {"$match": {"status": "active"}},
                "rationale": {"variant_branch": "active account subset"},
            }
        ],
        "variant_handling": [],
    }

    schema_errors = list(
        Draft202012Validator(SmartNosqlPlanner.output_schema).iter_errors(output)
    )
    violations = SmartNosqlPlanner().check_contract(
        None,
        {"logical_spec": {"shape_policy": "reshape"}, "shape_model": {}},
        output,
    )

    assert schema_errors == []
    assert not any("must include" in violation for violation in violations)


def test_planner_contract_requires_nested_event_native_outputs() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["native_context_bucket", "native_filtered_events", "native_event_count"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "feature_type": "nested_event_stream",
            "query_pattern": "thrombosis_event_evidence_filter",
        },
    }
    bad_plan = {
        "collection": "patient_clinical_profiles",
        "stages": [
            {
                "op": "$addFields",
                "note": "Uses a non-contract helper field.",
                "stage": {
                    "$addFields": {
                        "native_context_bucket": {"$ifNull": ["$timeline.context", "missing"]},
                        "filtered_events": [],
                    }
                },
            }
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert "nested_event_stream plan must define native_filtered_events" in violations
    assert "nested_event_stream plan must define native_event_count" in violations
    assert any("native_filtered_events has size > 0" in item for item in violations)


def test_planner_contract_accepts_nested_event_native_output_shape() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["native_context_bucket", "native_filtered_events", "native_event_count"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "feature_type": "nested_event_stream",
            "query_pattern": "thrombosis_event_evidence_filter",
        },
    }
    good_plan = {
        "collection": "patient_clinical_profiles",
        "stages": [
            {
                "op": "$addFields",
                "note": "Build native event output fields.",
                "stage": {
                    "$addFields": {
                        "native_context_bucket": {"$ifNull": ["$timeline.context", "missing"]},
                        "native_filtered_events": {
                            "$filter": {
                                "input": {"$cond": [{"$isArray": "$timeline.events"}, "$timeline.events", []]},
                                "as": "event",
                                "cond": {"$ne": ["$$event.event_time", None]},
                            }
                        },
                    }
                },
            },
            {
                "op": "$addFields",
                "note": "Count filtered events.",
                "stage": {"$addFields": {"native_event_count": {"$size": "$native_filtered_events"}}},
            },
            {
                "op": "$match",
                "note": "Keep documents with filtered events.",
                "stage": {"$match": {"$expr": {"$gt": [{"$size": "$native_filtered_events"}, 0]}}},
            },
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, good_plan) == []


def test_planner_contract_accepts_nested_event_count_match() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["native_context_bucket", "native_filtered_events", "native_event_count"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "feature_type": "nested_event_stream",
            "query_pattern": "thrombosis_event_evidence_filter",
        },
    }
    plan = {
        "collection": "patient_clinical_profiles",
        "stages": [
            {
                "op": "$addFields",
                "note": "Build native filtered events and event count.",
                "stage": {
                    "$addFields": {
                        "native_context_bucket": {"$ifNull": ["$timeline.context", "missing"]},
                        "native_filtered_events": [],
                        "native_event_count": {"$size": "$native_filtered_events"},
                    }
                },
            },
            {
                "op": "$match",
                "note": "Keep non-empty filtered event arrays via count.",
                "stage": {"$match": {"native_event_count": {"$gt": 0}}},
            },
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, plan) == []


def test_planner_contract_rejects_nested_event_date_alias_without_event_time() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["native_context_bucket", "native_filtered_events", "native_event_count"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "feature_type": "nested_event_stream",
            "query_pattern": "thrombosis_event_evidence_filter",
        },
    }
    plan = {
        "collection": "patient_clinical_profiles",
        "stages": [
            {
                "op": "$addFields",
                "note": "Wrongly filters using a bare date alias.",
                "stage": {
                    "$addFields": {
                        "native_context_bucket": {"$ifNull": ["$timeline.context", "missing"]},
                        "native_filtered_events": {
                            "$filter": {
                                "input": "$timeline.events",
                                "cond": {"$gte": ["$$this.date", "1995-11-16"]},
                            }
                        },
                        "native_event_count": {"$size": "$native_filtered_events"},
                    }
                },
            },
            {
                "op": "$match",
                "note": "Keep non-empty filtered events.",
                "stage": {"$match": {"native_event_count": {"$gt": 0}}},
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, plan)

    assert any("observed event_time field" in violation for violation in violations)


def test_planner_contract_requires_missing_vs_present_native_fields() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["native_presence_state", "native_context_bucket"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "feature_type": "missing_vs_present",
            "query_pattern": "missing_vs_present",
        },
    }
    bad_plan = {
        "collection": "molecule_graphs",
        "stages": [
            {
                "op": "$addFields",
                "note": "Classify presence with a non-contract alias.",
                "stage": {"$addFields": {"presence_state_classification": "missing"}},
            }
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert "missing_vs_present plan must define native_presence_state" in violations
    assert "missing_vs_present plan must define native_context_bucket" in violations


def test_planner_contract_accepts_missing_vs_present_native_fields() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["native_presence_state", "native_context_bucket"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "feature_type": "missing_vs_present",
            "query_pattern": "missing_vs_present",
        },
    }
    good_plan = {
        "collection": "molecule_graphs",
        "stages": [
            {
                "op": "$addFields",
                "note": "Classify the feature field and context bucket.",
                "stage": {
                    "$addFields": {
                        "native_presence_state": {"$ifNull": ["$assay.panel.presence_state", "missing"]},
                        "native_context_bucket": {"$ifNull": ["$assay.label.presence_state", "missing"]},
                    }
                },
            }
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, good_plan) == []


def test_intent_postprocess_canonicalizes_financial_loan_schedule_targets() -> None:
    agent = SmartIntentFormalizer()
    ctx = SimpleNamespace(log=SimpleNamespace(info=lambda *args, **kwargs: None))
    result = SimpleNamespace(transcript_ref="llm/intent.md", diagnostics_ref="llm/intent.json")
    output = {
        "entity": "account_ledgers",
        "per": "status-region-year",
        "compute": [],
        "aggregate": [],
        "output": {
            "target_fields": [
                "status",
                "region",
                "year",
                "due_month_count",
                "scheduled_total",
                "paid_total",
                "avg_salary_context",
            ]
        },
        "shape_policy": "reduce",
        "target_fields": [
            "status",
            "region",
            "year",
            "due_month_count",
            "scheduled_total",
            "paid_total",
            "avg_salary_context",
        ],
        "clause_coverage": ["loan schedule groups"],
    }

    spec = agent.postprocess(
        ctx,
        {"native_task_context": {"query_pattern": "financial.loan_schedule"}},
        output,
        result,
    )

    assert spec["target_fields"] == [
        "loan_status",
        "region",
        "year",
        "due_months",
        "scheduled_total",
        "paid_total",
        "avg_salary",
    ]
    assert spec["output"]["target_fields"] == spec["target_fields"]


def test_intent_postprocess_canonicalizes_financial_district_mix_targets() -> None:
    agent = SmartIntentFormalizer()
    ctx = SimpleNamespace(log=SimpleNamespace(info=lambda *args, **kwargs: None))
    result = SimpleNamespace(transcript_ref="llm/intent.md", diagnostics_ref="llm/intent.json")
    output = {
        "entity": "district_market_contexts",
        "per": "district-frequency",
        "compute": [],
        "aggregate": [],
        "output": {
            "target_fields": [
                "district_id",
                "district_name",
                "region",
                "fee_frequency",
                "total_accounts",
                "loan_accounts",
                "loan_share",
                "female_count",
                "male_count",
                "salary_context",
            ]
        },
        "shape_policy": "reshape",
        "target_fields": [
            "district_id",
            "district_name",
            "region",
            "fee_frequency",
            "total_accounts",
            "loan_accounts",
            "loan_share",
            "female_count",
            "male_count",
            "salary_context",
        ],
        "clause_coverage": ["district frequency groups"],
    }

    spec = agent.postprocess(
        ctx,
        {"native_task_context": {"query_pattern": "financial.district_frequency_gender_loan_mix"}},
        output,
        result,
    )

    assert spec["target_fields"] == [
        "district_id",
        "district_name",
        "region",
        "avg_salary",
        "salary_band",
        "frequency_key",
        "account_count",
        "loan_account_count",
        "female_count",
        "male_count",
        "loan_account_share",
        "female_share",
    ]
    assert spec["output"]["target_fields"] == spec["target_fields"]


def test_intent_postprocess_canonicalizes_financial_party_role_targets() -> None:
    agent = SmartIntentFormalizer()
    ctx = SimpleNamespace(log=SimpleNamespace(info=lambda *args, **kwargs: None))
    result = SimpleNamespace(transcript_ref="llm/intent.md", diagnostics_ref="llm/intent.json")
    output = {
        "entity": "party_relationship_graphs",
        "per": "document",
        "compute": [],
        "aggregate": [],
        "output": {"target_fields": ["owner_card_count", "district_context"]},
        "shape_policy": "preserve",
        "target_fields": ["owner_card_count", "district_context"],
        "clause_coverage": ["party role card mix"],
    }

    spec = agent.postprocess(
        ctx,
        {"native_task_context": {"query_pattern": "financial.party_role_card_loan_mix"}},
        output,
        result,
    )

    assert spec["shape_policy"] == "reshape"
    assert spec["target_fields"] == [
        "account_id",
        "district_name",
        "region",
        "frequency",
        "loan_status_bucket",
        "role_keys",
        "owner_count",
        "disponent_count",
        "owner_cards",
        "disponent_cards",
    ]


def test_intent_postprocess_canonicalizes_dynamic_key_entry_targets() -> None:
    agent = SmartIntentFormalizer()
    ctx = SimpleNamespace(log=SimpleNamespace(info=lambda *args, **kwargs: None))
    result = SimpleNamespace(transcript_ref="llm/intent.md", diagnostics_ref="llm/intent.json")
    output = {
        "entity": "party_relationship_graphs",
        "per": "dynamic-entry",
        "compute": [],
        "aggregate": [],
        "output": {"target_fields": ["relationships.members_by_role"]},
        "shape_policy": "preserve",
        "target_fields": ["relationships.members_by_role"],
        "clause_coverage": ["dynamic key entries"],
    }

    spec = agent.postprocess(
        ctx,
        {"native_task_context": {"query_pattern": "disposition_role_card_network"}},
        output,
        result,
    )

    assert spec["shape_policy"] == "reshape"
    assert spec["target_fields"] == [
        "native_context_bucket",
        "native_key",
        "native_value",
    ]


def test_intent_postprocess_canonicalizes_dynamic_key_totals_targets() -> None:
    agent = SmartIntentFormalizer()
    ctx = SimpleNamespace(log=SimpleNamespace(info=lambda *args, **kwargs: None))
    result = SimpleNamespace(transcript_ref="llm/intent.md", diagnostics_ref="llm/intent.json")
    output = {
        "entity": "counterparty_flow_profiles",
        "per": "dynamic-key",
        "compute": [],
        "aggregate": [],
        "output": {"target_fields": ["native_key"]},
        "shape_policy": "reshape",
        "target_fields": ["native_key"],
        "clause_coverage": ["summarize sample edge totals"],
    }

    spec = agent.postprocess(
        ctx,
        {
            "nlq": "summarize sample_edges.account_id totals across dynamic flows_by_symbol keys",
            "native_task_context": {"query_pattern": "counterparty_operation_symbol_matrix"},
        },
        output,
        result,
    )

    assert spec["shape_policy"] == "reduce"
    assert spec["target_fields"] == ["entry_count", "metric_total"]


def test_planner_contract_requires_financial_loan_schedule_semantics() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reduce",
            "target_fields": ["status", "region", "year", "month_count"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.loan_schedule",
            "feature_field": "loan.repayment_schedule.by_due_month",
        },
    }
    bad_plan = {
        "collection": "account_ledgers",
        "stages": [
            {
                "op": "$addFields",
                "note": "Build month map.",
                "stage": {
                    "$addFields": {
                        "months_array": {
                            "$ifNull": ["$loan.repayment_schedule.by_due_month", {}]
                        }
                    }
                },
            },
            {
                "op": "$group",
                "note": "Only count months by status/region/year.",
                "stage": {
                    "$group": {
                        "_id": {"status": "$status", "region": "$region", "year": "$year"},
                        "month_count": {"$sum": 1},
                    }
                },
            },
            {
                "op": "$project",
                "note": "Project reduced output.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "status": "$_id.status",
                        "region": "$_id.region",
                        "year": "$_id.year",
                        "month_count": 1,
                    }
                },
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any("financial.loan_schedule plan must output fields" in item for item in violations)
    assert any("status_bucket" in item for item in violations)
    assert any("scheduled_amount" in item for item in violations)
    assert any("observed_payment_total" in item for item in violations)


def test_planner_contract_accepts_financial_loan_schedule_semantics() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reduce",
            "target_fields": [
                "loan_status",
                "region",
                "year",
                "due_months",
                "scheduled_total",
                "paid_total",
                "avg_salary",
            ],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.loan_schedule",
            "feature_field": "loan.repayment_schedule.by_due_month",
        },
    }
    good_plan = {
        "collection": "account_ledgers",
        "stages": [
            {
                "op": "$project",
                "note": "Expose loan schedule, status bucket, region, and salary.",
                "stage": {
                    "$project": {
                        "loan_status": "$loan.contract.status_bucket",
                        "region": "$district_context.region",
                        "salary": "$district_context.avg_salary",
                        "dues": {
                            "$objectToArray": {
                                "$ifNull": ["$loan.repayment_schedule.by_due_month", {}]
                            }
                        },
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "One row per due month.",
                "stage": {"$unwind": "$dues"},
            },
            {
                "op": "$group",
                "note": "Aggregate schedule amounts by loan status, region, and year.",
                "stage": {
                    "$group": {
                        "_id": {
                            "loan_status": "$loan_status",
                            "region": "$region",
                            "year": {"$substr": ["$dues.k", 0, 4]},
                        },
                        "due_months": {"$sum": 1},
                        "scheduled_total": {"$sum": "$dues.v.scheduled_amount"},
                        "paid_total": {"$sum": "$dues.v.observed_payment_total"},
                        "avg_salary": {"$avg": "$salary"},
                    }
                },
            },
            {
                "op": "$project",
                "note": "Project final schedule metrics.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "loan_status": "$_id.loan_status",
                        "region": "$_id.region",
                        "year": "$_id.year",
                        "due_months": 1,
                        "scheduled_total": 1,
                        "paid_total": 1,
                        "avg_salary": 1,
                    }
                },
            },
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, good_plan) == []


def test_planner_contract_requires_financial_district_mix_semantics() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": [
                "district",
                "fee_frequency",
                "loan_account_share",
                "f_client_count",
                "m_client_count",
                "salary_band",
            ],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.district_frequency_gender_loan_mix",
            "feature_field": "accounts_by_frequency",
        },
    }
    bad_plan = {
        "collection": "district_market_contexts",
        "stages": [
            {
                "op": "$project",
                "note": "Project broad district output.",
                "stage": {
                    "$project": {
                        "district": 1,
                        "fee_frequency": 1,
                        "loan_account_share": 1,
                        "f_client_count": 1,
                        "m_client_count": 1,
                        "salary_band": 1,
                    }
                },
            }
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any(
        "financial.district_frequency_gender_loan_mix plan must output fields" in item
        for item in violations
    )
    assert any("district.name" in item for item in violations)
    assert any("loan_presence_state" in item for item in violations)
    assert any("clients_by_gender" in item for item in violations)


def test_planner_contract_requires_financial_district_mix_support_filters() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": [
                "district_id",
                "district_name",
                "region",
                "avg_salary",
                "salary_band",
                "frequency_key",
                "account_count",
                "loan_account_count",
                "female_count",
                "male_count",
                "loan_account_share",
                "female_share",
            ],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.district_frequency_gender_loan_mix",
            "feature_field": "accounts_by_frequency",
        },
    }
    bad_plan = {
        "collection": "district_market_contexts",
        "stages": [
            {
                "op": "$project",
                "note": "Project accounts and gender groups.",
                "stage": {
                    "$project": {
                        "district_id": "$district.district_id",
                        "district_name": "$district.name",
                        "region": "$district.region",
                        "avg_salary": "$district.avg_salary",
                        "salary_band": 1,
                        "frequency_accounts": {"$objectToArray": "$accounts_by_frequency"},
                        "female_clients": "$clients_by_gender.F",
                        "male_clients": "$clients_by_gender.M",
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "One row per frequency.",
                "stage": {"$unwind": "$frequency_accounts"},
            },
            {
                "op": "$project",
                "note": "Compute shares but omit support filters and tie-break.",
                "stage": {
                    "$project": {
                        "district_id": 1,
                        "district_name": 1,
                        "region": 1,
                        "avg_salary": 1,
                        "salary_band": 1,
                        "frequency_key": "$frequency_accounts.k",
                        "account_count": {"$size": "$frequency_accounts.v"},
                        "loan_account_count": {
                            "$size": {
                                "$filter": {
                                    "input": "$frequency_accounts.v",
                                    "as": "account",
                                    "cond": {
                                        "$eq": [
                                            "$$account.loan_presence_state",
                                            "present",
                                        ]
                                    },
                                }
                            }
                        },
                        "female_count": {"$size": "$female_clients"},
                        "male_count": {"$size": "$male_clients"},
                        "loan_account_share": 1,
                        "female_share": 1,
                    }
                },
            },
            {
                "op": "$sort",
                "note": "Sort by share only.",
                "stage": {"$sort": {"loan_account_share": -1}},
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any("account_count >= 20" in item for item in violations)
    assert any("loan_account_count >= 1" in item for item in violations)
    assert any("female_count >= 10" in item for item in violations)
    assert any("male_count >= 10" in item for item in violations)
    assert any("sort by loan_account_share and account_count" in item for item in violations)


def test_planner_contract_accepts_financial_district_mix_support_filters() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": [
                "district_id",
                "district_name",
                "region",
                "avg_salary",
                "salary_band",
                "frequency_key",
                "account_count",
                "loan_account_count",
                "female_count",
                "male_count",
                "loan_account_share",
                "female_share",
            ],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.district_frequency_gender_loan_mix",
            "feature_field": "accounts_by_frequency",
        },
    }
    good_plan = {
        "collection": "district_market_contexts",
        "stages": [
            {
                "op": "$project",
                "note": "Expose district, accounts by frequency, and gender arrays.",
                "stage": {
                    "$project": {
                        "district_id": "$district.district_id",
                        "district_name": "$district.name",
                        "region": "$district.region",
                        "avg_salary": "$district.avg_salary",
                        "salary_band": 1,
                        "frequencies": {"$objectToArray": {"$ifNull": ["$accounts_by_frequency", {}]}},
                        "female_clients": {"$ifNull": ["$clients_by_gender.F", []]},
                        "male_clients": {"$ifNull": ["$clients_by_gender.M", []]},
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "One row per fee frequency.",
                "stage": {"$unwind": "$frequencies"},
            },
            {
                "op": "$addFields",
                "note": "Count accounts, loans, and gender support.",
                "stage": {
                    "$addFields": {
                        "account_count": {"$size": {"$ifNull": ["$frequencies.v", []]}},
                        "loan_account_count": {
                            "$size": {
                                "$filter": {
                                    "input": {"$ifNull": ["$frequencies.v", []]},
                                    "as": "account",
                                    "cond": {
                                        "$eq": [
                                            "$$account.loan_presence_state",
                                            "present",
                                        ]
                                    },
                                }
                            }
                        },
                        "female_count": {"$size": "$female_clients"},
                        "male_count": {"$size": "$male_clients"},
                    }
                },
            },
            {
                "op": "$match",
                "note": "Keep only supported district-frequency groups.",
                "stage": {
                    "$match": {
                        "account_count": {"$gte": 20},
                        "loan_account_count": {"$gte": 1},
                        "female_count": {"$gte": 10},
                        "male_count": {"$gte": 10},
                    }
                },
            },
            {
                "op": "$project",
                "note": "Project final district frequency shares.",
                "stage": {
                    "$project": {
                        "district_id": 1,
                        "district_name": 1,
                        "region": 1,
                        "avg_salary": 1,
                        "salary_band": 1,
                        "frequency_key": "$frequencies.k",
                        "account_count": 1,
                        "loan_account_count": 1,
                        "female_count": 1,
                        "male_count": 1,
                        "loan_account_share": {
                            "$divide": ["$loan_account_count", "$account_count"]
                        },
                        "female_share": {
                            "$divide": [
                                "$female_count",
                                {"$add": ["$female_count", "$male_count"]},
                            ]
                        },
                    }
                },
            },
            {
                "op": "$sort",
                "note": "Sort by concentration, then account support.",
                "stage": {"$sort": {"loan_account_share": -1, "account_count": -1}},
            },
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, good_plan) == []


def test_planner_contract_requires_financial_district_mix_root_id() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": [
                "district_id",
                "district_name",
                "region",
                "avg_salary",
                "salary_band",
                "frequency_key",
                "account_count",
                "loan_account_count",
                "female_count",
                "male_count",
                "loan_account_share",
                "female_share",
            ],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.district_frequency_gender_loan_mix",
            "feature_field": "accounts_by_frequency",
        },
    }
    bad_plan = {
        "collection": "district_market_contexts",
        "stages": [
            {
                "op": "$project",
                "note": "Drop root id while projecting otherwise correct fields.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "district_id": "$district.district_id",
                        "district_name": "$district.name",
                        "region": "$district.region",
                        "avg_salary": "$district.avg_salary",
                        "salary_band": 1,
                        "frequencies": {"$objectToArray": {"$ifNull": ["$accounts_by_frequency", {}]}},
                        "female_clients": {"$ifNull": ["$clients_by_gender.F", []]},
                        "male_clients": {"$ifNull": ["$clients_by_gender.M", []]},
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "One row per fee frequency.",
                "stage": {"$unwind": "$frequencies"},
            },
            {
                "op": "$addFields",
                "note": "Count accounts, loans, and gender support.",
                "stage": {
                    "$addFields": {
                        "account_count": {"$size": {"$ifNull": ["$frequencies.v", []]}},
                        "loan_account_count": {
                            "$size": {
                                "$filter": {
                                    "input": {"$ifNull": ["$frequencies.v", []]},
                                    "as": "account",
                                    "cond": {
                                        "$eq": [
                                            "$$account.loan_presence_state",
                                            "present",
                                        ]
                                    },
                                }
                            }
                        },
                        "female_count": {"$size": "$female_clients"},
                        "male_count": {"$size": "$male_clients"},
                    }
                },
            },
            {
                "op": "$match",
                "note": "Keep only supported district-frequency groups.",
                "stage": {
                    "$match": {
                        "account_count": {"$gte": 20},
                        "loan_account_count": {"$gte": 1},
                        "female_count": {"$gte": 10},
                        "male_count": {"$gte": 10},
                    }
                },
            },
            {
                "op": "$project",
                "note": "Project final district frequency shares without _id.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "district_id": 1,
                        "district_name": 1,
                        "region": 1,
                        "avg_salary": 1,
                        "salary_band": 1,
                        "frequency_key": "$frequencies.k",
                        "account_count": 1,
                        "loan_account_count": 1,
                        "female_count": 1,
                        "male_count": 1,
                        "loan_account_share": {
                            "$divide": ["$loan_account_count", "$account_count"]
                        },
                        "female_share": {
                            "$divide": [
                                "$female_count",
                                {"$add": ["$female_count", "$male_count"]},
                            ]
                        },
                    }
                },
            },
            {
                "op": "$sort",
                "note": "Sort by concentration, then account support.",
                "stage": {"$sort": {"loan_account_share": -1, "account_count": -1}},
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any("preserve the root _id" in item for item in violations)


def test_planner_contract_rejects_financial_loan_schedule_sentinel_defaults() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reduce",
            "target_fields": [
                "loan_status",
                "region",
                "year",
                "due_months",
                "scheduled_total",
                "paid_total",
                "avg_salary",
            ],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.loan_schedule",
            "feature_field": "loan.repayment_schedule.by_due_month",
        },
    }
    bad_plan = {
        "collection": "account_ledgers",
        "stages": [
            {
                "op": "$project",
                "note": "Coerce grouping dimensions to invented sentinel strings.",
                "stage": {
                    "$project": {
                        "loan_status": {"$ifNull": ["$loan.contract.status_bucket", "unknown"]},
                        "region": {"$ifNull": ["$district_context.region", "unknown region"]},
                        "salary": "$district_context.avg_salary",
                        "dues": {
                            "$objectToArray": {
                                "$ifNull": ["$loan.repayment_schedule.by_due_month", {}]
                            }
                        },
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "One row per due month.",
                "stage": {"$unwind": "$dues"},
            },
            {
                "op": "$group",
                "note": "Aggregate schedule by status, region, and year.",
                "stage": {
                    "$group": {
                        "_id": {
                            "loan_status": "$loan_status",
                            "region": "$region",
                            "year": {"$substr": ["$dues.k", 0, 4]},
                        },
                        "due_months": {"$sum": 1},
                        "scheduled_total": {"$sum": "$dues.v.scheduled_amount"},
                        "paid_total": {"$sum": "$dues.v.observed_payment_total"},
                        "avg_salary": {"$avg": "$salary"},
                    }
                },
            },
            {
                "op": "$project",
                "note": "Project final metrics.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "loan_status": "$_id.loan_status",
                        "region": "$_id.region",
                        "year": "$_id.year",
                        "due_months": 1,
                        "scheduled_total": 1,
                        "paid_total": 1,
                        "avg_salary": 1,
                    }
                },
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any("invented sentinel strings" in item for item in violations)


def test_planner_contract_rejects_financial_loan_schedule_scheduled_payment_fallback() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reduce",
            "target_fields": [
                "loan_status",
                "region",
                "year",
                "due_months",
                "scheduled_total",
                "paid_total",
                "avg_salary",
            ],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.loan_schedule",
            "feature_field": "loan.repayment_schedule.by_due_month",
        },
    }
    bad_plan = {
        "collection": "account_ledgers",
        "stages": [
            {
                "op": "$project",
                "note": "Build due entries and incorrectly fall back to scheduled_payment.",
                "stage": {
                    "$project": {
                        "loan_status": "$loan.contract.status_bucket",
                        "region": "$district_context.region",
                        "salary": "$district_context.avg_salary",
                        "dues": {
                            "$objectToArray": {
                                "$ifNull": ["$loan.repayment_schedule.by_due_month", {}]
                            }
                        },
                        "scheduled_amount": {
                            "$ifNull": [
                                "$dues.v.scheduled_amount",
                                "$dues.v.scheduled_payment",
                            ]
                        },
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "One row per due month.",
                "stage": {"$unwind": "$dues"},
            },
            {
                "op": "$group",
                "note": "Aggregate schedule by status, region, and year.",
                "stage": {
                    "$group": {
                        "_id": {
                            "loan_status": "$loan_status",
                            "region": "$region",
                            "year": {"$substr": ["$dues.k", 0, 4]},
                        },
                        "due_months": {"$sum": 1},
                        "scheduled_total": {"$sum": "$scheduled_amount"},
                        "paid_total": {"$sum": "$dues.v.observed_payment_total"},
                        "avg_salary": {"$avg": "$salary"},
                    }
                },
            },
            {
                "op": "$project",
                "note": "Project final metrics.",
                "stage": {
                    "$project": {
                        "_id": 0,
                        "loan_status": "$_id.loan_status",
                        "region": "$_id.region",
                        "year": "$_id.year",
                        "due_months": 1,
                        "scheduled_total": 1,
                        "paid_total": 1,
                        "avg_salary": 1,
                    }
                },
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any("scheduled_amount only" in item for item in violations)


def test_planner_contract_accepts_dynamic_key_entry_row_shape() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": ["native_context_bucket", "native_key", "native_value"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "disposition_role_card_network",
            "feature_field": "relationships.members_by_role",
        },
    }
    good_plan = {
        "collection": "party_relationship_graphs",
        "stages": [
            {
                "op": "$addFields",
                "note": "Bucket the loan id context without filtering by it.",
                "stage": {
                    "$addFields": {
                        "native_context_bucket": {
                            "$cond": [
                                {"$gte": ["$loan_link.loan_id", 5172]},
                                "loan_link.loan_id>= 5172",
                                "loan_link.loan_id< 5172",
                            ]
                        }
                    }
                },
            },
            {
                "op": "$project",
                "note": "Expose dynamic role entries.",
                "stage": {
                    "$project": {
                        "_id": 1,
                        "native_context_bucket": 1,
                        "native_dynamic_entries": {
                            "$objectToArray": {
                                "$ifNull": ["$relationships.members_by_role", {}]
                            }
                        },
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "One row per dynamic role key.",
                "stage": {"$unwind": "$native_dynamic_entries"},
            },
            {
                "op": "$match",
                "note": "Keep OWNER role entries.",
                "stage": {"$match": {"native_dynamic_entries.k": "OWNER"}},
            },
            {
                "op": "$project",
                "note": "Project native entry row fields.",
                "stage": {
                    "$project": {
                        "_id": 1,
                        "native_context_bucket": 1,
                        "native_key": "$native_dynamic_entries.k",
                        "native_value": "$native_dynamic_entries.v",
                    }
                },
            },
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, good_plan) == []


def test_planner_contract_rejects_old_dynamic_matching_array_shape() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": ["native_context_bucket", "native_key", "native_value"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "disposition_role_card_network",
            "feature_field": "relationships.members_by_role",
        },
    }
    bad_plan = {
        "collection": "party_relationship_graphs",
        "stages": [
            {
                "op": "$project",
                "note": "Use the old array shape and incorrectly filter by context.",
                "stage": {
                    "$project": {
                        "_id": 1,
                        "native_matching_dynamic_entries": {
                            "$objectToArray": {
                                "$ifNull": ["$relationships.members_by_role", {}]
                            }
                        },
                    }
                },
            },
            {
                "op": "$match",
                "note": "Wrongly treat context bucket threshold as a record filter.",
                "stage": {"$match": {"loan_link.loan_id": {"$gte": 5172}}},
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any("native_dynamic_entries" in item for item in violations)
    assert any("not filter records by that context path" in item for item in violations)


def test_planner_postprocess_canonicalizes_dynamic_entry_object_unwind_form() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": ["native_context_bucket", "native_key", "native_value"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "disposition_role_card_network",
            "feature_field": "relationships.members_by_role",
        },
    }
    bad_plan = {
        "collection": "party_relationship_graphs",
        "stages": [
            {
                "op": "$addFields",
                "note": "Bucket the loan id context.",
                "stage": {
                    "$addFields": {
                        "native_context_bucket": {
                            "$cond": [
                                {"$gte": ["$loan_link.loan_id", 5172]},
                                "loan_link.loan_id>= 5172",
                                "loan_link.loan_id< 5172",
                            ]
                        }
                    }
                },
            },
            {
                "op": "$project",
                "note": "Expose dynamic entries.",
                "stage": {
                    "$project": {
                        "_id": 1,
                        "native_context_bucket": 1,
                        "native_dynamic_entries": {
                            "$objectToArray": {
                                "$ifNull": ["$relationships.members_by_role", {}]
                            }
                        },
                    }
                },
            },
            {
                "op": "$unwind",
                "note": "Use object-form unwind that leaks helper field paths.",
                "stage": {
                    "$unwind": {
                        "path": "$native_dynamic_entries",
                        "preserveNullAndEmptyArrays": False,
                    }
                },
            },
            {
                "op": "$match",
                "note": "Keep OWNER role entries.",
                "stage": {"$match": {"native_dynamic_entries.k": "OWNER"}},
            },
            {
                "op": "$project",
                "note": "Project entry fields.",
                "stage": {
                    "$project": {
                        "native_context_bucket": 1,
                        "native_key": "$native_dynamic_entries.k",
                        "native_value": "$native_dynamic_entries.v",
                    }
                },
            },
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, bad_plan) == []

    result = SimpleNamespace(transcript_ref="llm/plan.md", diagnostics_ref="llm/plan.json")
    ctx = SimpleNamespace(log=SimpleNamespace(info=lambda *args, **kwargs: None))
    normalized = planner.postprocess(ctx, inputs, bad_plan, result)

    assert normalized["stages"][2]["stage"] == {"$unwind": "$native_dynamic_entries"}


def test_planner_contract_requires_financial_party_role_card_output_fields() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "reshape",
            "target_fields": ["owner_count", "district_context"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "query_pattern": "financial.party_role_card_loan_mix",
            "feature_field": "relationships.members_by_role",
        },
    }
    bad_plan = {
        "collection": "party_relationship_graphs",
        "stages": [
            {
                "op": "$project",
                "note": "Use non-canonical summary field names.",
                "stage": {
                    "$project": {
                        "owner_card_count": 1,
                        "disponent_card_count": 1,
                        "loan_state": "$loan_link.status_bucket",
                        "district_context": "$account.district.name",
                    }
                },
            }
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, bad_plan)

    assert any("financial.party_role_card_loan_mix plan must output fields" in item for item in violations)
    assert any("account_id" in item for item in violations)
    assert any("disponent_cards" in item for item in violations)


def test_planner_preserve_contract_allows_target_fields_retained_by_project() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["timeline.events", "native_context_bucket"],
        },
        "shape_model": {},
        "witness_digest": {},
    }
    plan = {
        "collection": "patient_clinical_profiles",
        "stages": [
            {
                "op": "$addFields",
                "note": "Add context while keeping original timeline events in projection.",
                "stage": {
                    "$addFields": {
                        "native_context_bucket": {"$ifNull": ["$timeline.context", "missing"]}
                    }
                },
            },
            {
                "op": "$project",
                "note": "Retain the original event stream as an output field.",
                "stage": {"$project": {"_id": 1, "timeline.events": 1, "native_context_bucket": 1}},
            },
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, plan) == []


def test_planner_preserve_contract_allows_original_target_retained_by_exclusion_project() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["relationships.members_by_role"],
        },
        "shape_model": {},
        "witness_digest": {},
        "native_task_context": {
            "feature_field": "relationships.members_by_role",
        },
    }
    plan = {
        "collection": "party_relationship_graphs",
        "stages": [
            {
                "op": "$addFields",
                "note": "Add temporary fields while retaining original relationship map.",
                "stage": {
                    "$addFields": {
                        "_owner_entries": {
                            "$objectToArray": {
                                "$ifNull": ["$relationships.members_by_role", {}]
                            }
                        },
                        "_loan_match": True,
                    }
                },
            },
            {
                "op": "$project",
                "note": "Remove only temporary helper fields.",
                "stage": {"$project": {"_owner_entries": 0, "_loan_match": 0}},
            },
        ],
        "variant_handling": [],
    }

    assert planner.check_contract(None, inputs, plan) == []


def test_planner_preserve_contract_rejects_original_target_excluded_by_project() -> None:
    planner = SmartNosqlPlanner()
    inputs = {
        "logical_spec": {
            "shape_policy": "preserve",
            "target_fields": ["relationships.members_by_role"],
        },
        "shape_model": {},
        "witness_digest": {},
    }
    plan = {
        "collection": "party_relationship_graphs",
        "stages": [
            {
                "op": "$project",
                "note": "Incorrectly drop the original relationship map.",
                "stage": {"$project": {"relationships.members_by_role": 0}},
            },
        ],
        "variant_handling": [],
    }

    violations = planner.check_contract(None, inputs, plan)

    assert any("relationships.members_by_role" in item for item in violations)


def test_observed_literal_guard_ignores_mongo_field_references() -> None:
    violations = _observed_literal_violations(
        "district_market_contexts",
        [
            {
                "stage": {
                    "$project": {
                        "_id": "$_id",
                        "district_id": "$district.district_id",
                    }
                }
            }
        ],
        {
            "district_market_contexts": {
                "string_values_in_sample": {
                    "_id": ["district:1", "district:2"],
                    "district.district_id": ["1", "2"],
                }
            }
        },
    )

    assert violations == []


def test_smart_prompts_name_allowed_inputs_without_only_shape_contradictions() -> None:
    prompt_dir = Path(__file__).resolve().parents[1] / "proposals" / "agent_prompts"
    intent = (prompt_dir / "smart_intent_formalizer.md").read_text(encoding="utf-8")
    planner = (prompt_dir / "smart_nosql_planner.md").read_text(encoding="utf-8")

    assert "canonical/colloquial NLQ" in intent
    assert "shape model" in intent
    assert "public native task context" in intent
    assert "feature_field" in intent
    assert "bounded checkpoint" in intent
    assert "shape model only" not in intent
    assert "preserve" in intent
    assert "inspect dynamic keys" in intent
    assert "native_dynamic_entries" in intent
    assert "native_key" in intent
    assert "native_value" in intent
    assert "role/card/loan summaries" in intent
    assert "nested_event_stream" in intent
    assert "native_filtered_events" in intent
    assert "missing_vs_present" in intent
    assert "native_presence_state" in intent

    assert "NLQ-derived logical spec" in planner
    assert "shape model" in planner
    assert "public native task context" in planner
    assert "target_shape_policy" in planner
    assert "bounded checkpoint feedback" in planner
    assert "disclosed witness digest" in planner
    assert "Use only the logical spec and shape model" not in planner
    assert "inspect dynamic keys under" in planner
    assert "native_dynamic_entries" in planner
    assert "native_key" in planner
    assert "native_value" in planner
    assert "native_context_bucket" in planner
    assert "old `native_matching_dynamic_entries` array shape" in planner
    assert "nested_event_stream" in planner
    assert "native_event_count" in planner
    assert "missing_vs_present" in planner
    assert "financial.party_role_card_loan_mix" in planner
    assert "disposition_role_card_network" in planner
    assert "loan_status_repayment_schedule" in planner
