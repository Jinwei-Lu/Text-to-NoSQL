from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from tend.agents import AgentContext
from tend.agents.phase_b import (
    RoundTripVerifier,
    _compiled_reference_oracle_nl_contract,
    _compiled_rtv_contract_template_gaps,
)
from tend.config import Settings
from tend.observability import setup_logging


COMPILED_RTV_MODE = "compiled_reference_oracle_nl_contract"


@pytest.fixture(scope="module")
def stub_settings() -> Settings:
    return Settings.from_env(overrides={"TEND_LLM_STUB": "1"}, run_id="pytest")


@pytest.fixture()
def logger(tmp_path: Path):
    return setup_logging(tmp_path / "run")


def _events(run_dir: Path) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def _compiled_inputs(
    *,
    reference: dict,
    canonical: str,
    shape_policy: str,
    result_fields: list[str],
) -> dict:
    return {
        "verification_mode": COMPILED_RTV_MODE,
        "reference_oracle": reference,
        "result_fields": result_fields,
        "shape_policy": shape_policy,
        "compiled_gold_provenance": {
            "source": "workflow_direct_compile",
            "compiler": "_canonical_reference_mql",
            "template": reference["template"],
            "gold_lock": "norm_exec_nonempty",
        },
        "nl_queries": {
            "canonical": canonical,
            "colloquial": canonical,
        },
        "MQL": "db.account.aggregate([])",
        "schema": {
            "account": {
                "_id": "INT",
                "loan": {"type": "OBJECT", "fields": {"amount": "REAL", "duration": "INT"}},
            },
            "trans": {"account_id": "INT", "amount": "REAL"},
        },
    }


@pytest.mark.parametrize(
    ("reference", "shape_policy", "result_fields", "good_canonical", "bad_canonical", "missing"),
    [
        (
            {
                "template": "has_vs_absent_compare",
                "params": {
                    "parent_collection": "account",
                    "embed_field": "loan",
                    "metric_field": "loan.duration",
                    "agg": "max",
                },
            },
            "reduce",
            ["_id", "value"],
            (
                "Group accounts by exact loan status labels present and absent, and output "
                "value as the maximum loan duration for each group."
            ),
            "Compare accounts by loan status and output the maximum loan duration.",
            {"present", "absent"},
        ),
        (
            {
                "template": "optional_embed_projection",
                "params": {
                    "parent_collection": "account",
                    "embed_field": "loan",
                    "value_path": "amount",
                    "target_field": "loan_amount_or_default",
                    "missing_default": 0,
                },
            },
            "preserve",
            ["loan_amount_or_default"],
            (
                "Add loan_amount_or_default to each account using loan amount when present; "
                "otherwise use default zero, and keep each account document otherwise unchanged."
            ),
            "Show each account's loan amount.",
            {"loan_amount_or_default", "missing_default=0", "preserve/add-field semantics"},
        ),
        (
            {
                "template": "present_missing_projection",
                "params": {
                    "parent_collection": "account",
                    "embed_field": "loan",
                    "numerator_path": "loan.amount",
                    "target_field": "loan_to_credit_ratio",
                    "absent_value": 0,
                    "denom": {
                        "collection": "trans",
                        "local_id": "_id",
                        "foreign_field": "account_id",
                        "sum_field": "amount",
                    },
                },
            },
            "preserve",
            ["loan_to_credit_ratio"],
            (
                "Add loan_to_credit_ratio to each account: when loan is present use loan "
                "amount divided by total trans amount for the same account_id; when the "
                "denominator is zero use one, when loan is absent use default zero, and keep "
                "every account document otherwise unchanged."
            ),
            "Add loan_to_credit_ratio to each account from loan amount and keep every account.",
            {"absent_value=0", "trans", "zero_value=1"},
        ),
        (
            {
                "template": "fk_rollup",
                "params": {
                    "parent_collection": "account",
                    "child_collection": "loan",
                    "parent_key": "_id",
                    "foreign_key": "account_id",
                    "agg": "sum",
                    "value_field": "amount",
                },
            },
            "reshape",
            ["_id", "value"],
            (
                "For each account, roll up matching loan child rows by account_id, sum "
                "their amount, and output _id and value."
            ),
            "For each account, roll up matching loan child rows and output _id and value.",
            {"account_id", "sum", "amount"},
        ),
    ],
)
def test_compiled_reference_oracle_nl_contract_good_and_bad_cases(
    reference: dict,
    shape_policy: str,
    result_fields: list[str],
    good_canonical: str,
    bad_canonical: str,
    missing: set[str],
) -> None:
    good = _compiled_reference_oracle_nl_contract(
        _compiled_inputs(
            reference=reference,
            canonical=good_canonical,
            shape_policy=shape_policy,
            result_fields=result_fields,
        )
    )
    bad = _compiled_reference_oracle_nl_contract(
        _compiled_inputs(
            reference=reference,
            canonical=bad_canonical,
            shape_policy=shape_policy,
            result_fields=result_fields,
        )
    )

    assert good["rtv_pass"] is True
    assert good["rtv_mode"] == COMPILED_RTV_MODE
    assert good["template"] == reference["template"]
    assert good["compiled_gold_provenance"]["template"] == reference["template"]

    assert bad["rtv_pass"] is False
    assert missing <= set(bad["missing_terms"])
    assert "compiled reference-oracle NL contract missing required terms" in bad["rtv_reason"]
    assert bad["rtv_mode"] == COMPILED_RTV_MODE


def test_compiled_reference_oracle_nl_contract_requires_direct_compile_provenance() -> None:
    inputs = _compiled_inputs(
        reference={
            "template": "group_count",
            "params": {"collection": "account", "group_by": "frequency"},
        },
        canonical="Group accounts by frequency and output the count for each group.",
        shape_policy="reduce",
        result_fields=["_id", "count"],
    )

    missing = {key: value for key, value in inputs.items() if key != "compiled_gold_provenance"}
    wrong_template = dict(inputs)
    wrong_template["compiled_gold_provenance"] = {
        **inputs["compiled_gold_provenance"],
        "template": "simple_filter",
    }

    missing_out = _compiled_reference_oracle_nl_contract(missing)
    mismatch_out = _compiled_reference_oracle_nl_contract(wrong_template)

    assert missing_out["rtv_pass"] is False
    assert "compiled_gold_provenance from workflow direct compile is required" in missing_out[
        "violations"
    ]
    assert mismatch_out["rtv_pass"] is False
    assert any("compiled_gold_provenance.template" in v for v in mismatch_out["violations"])


def test_compiled_reference_oracle_nl_contract_rejects_inverted_filter_operator() -> None:
    reference = {
        "template": "simple_filter",
        "params": {
            "collection": "account",
            "predicates": [{"field": "balance", "op": "gt", "value": 1000}],
            "project": ["balance"],
        },
    }
    out = _compiled_reference_oracle_nl_contract(
        _compiled_inputs(
            reference=reference,
            canonical="Return accounts where balance is less than 1000 and output balance.",
            shape_policy="reshape",
            result_fields=["balance"],
        )
    )

    assert out["rtv_pass"] is False
    assert "predicate op gt" in out["missing_terms"]


def test_compiled_reference_oracle_nl_contract_rejects_omitted_rollup_match_filter() -> None:
    reference = {
        "template": "fk_rollup",
        "params": {
            "parent_collection": "account",
            "child_collection": "loan",
            "parent_key": "_id",
            "foreign_key": "account_id",
            "agg": "sum",
            "value_field": "amount",
            "match": {"field": "status", "value": "active"},
        },
    }
    out = _compiled_reference_oracle_nl_contract(
        _compiled_inputs(
            reference=reference,
            canonical=(
                "For each account, join matching loan child rows by account_id, sum amount, "
                "and output _id and value."
            ),
            shape_policy="reshape",
            result_fields=["_id", "value"],
        )
    )

    assert out["rtv_pass"] is False
    assert {"match.field='status'", "match.value='active'"} <= set(out["missing_terms"])


def test_round_trip_verifier_run_uses_compiled_mode_without_llm(stub_settings, logger) -> None:
    ctx = AgentContext(settings=stub_settings, llm=None, log=logger)
    inputs = _compiled_inputs(
        reference={
            "template": "has_vs_absent_compare",
            "params": {
                "parent_collection": "account",
                "embed_field": "loan",
                "metric_field": "loan.duration",
                "agg": "max",
            },
        },
        canonical=(
            "Group accounts by exact loan status labels present and absent, and output "
            "value as the maximum loan duration for each group."
        ),
        shape_policy="reduce",
        result_fields=["_id", "value"],
    )

    out = asyncio.run(RoundTripVerifier().run(ctx, inputs))

    assert out["rtv_pass"] is True
    assert out["rtv_mode"] == COMPILED_RTV_MODE


def test_compiled_rtv_contract_template_coverage_is_complete() -> None:
    assert _compiled_rtv_contract_template_gaps() == []


def test_workflow_compiled_gold_bypasses_ms_but_calls_compiled_rtv(stub_settings, logger) -> None:
    from tend.workflow.flows import CoverageSlot, DbArtifacts, _build_record

    reference = {
        "template": "group_count",
        "params": {"collection": "account", "group_by": "frequency"},
    }
    calls: dict[str, int] = {}
    rtv_payloads: list[dict] = []

    class _Mongo:
        def available(self):
            return True

        def norm_exec(self, db_id, mql):
            assert db_id == "financial"
            assert "db.account.aggregate" in mql
            return [{"_id": "monthly", "count": 1}]

        def count(self, db_id, collection):
            return 1

    class _WF:
        def __init__(self):
            self.ctx = AgentContext(
                settings=replace(stub_settings, stub=False),
                llm=None,
                log=logger,
                mongo=_Mongo(),
            )

        def context(self, **fields):
            return self.ctx.bind(**fields)

        async def agent(self, agent_id, inputs, ctx=None):
            calls[agent_id] = calls.get(agent_id, 0) + 1
            if agent_id == "qps":
                return {
                    "intent": {
                        "seed_mechanism": "baseline",
                        "archetype": "group_count",
                        "shape_policy": "reduce",
                    },
                }
            if agent_id == "ms":
                raise AssertionError("compiled hidden oracle should bypass MS")
            if agent_id == "mut":
                return {"mutations": [{"mutation_id": f"m{i}", "MQL": "x"} for i in range(5)]}
            if agent_id == "pv":
                return {"pv_pass": True, "property_verification": {}}
            if agent_id == "nlp":
                return {
                    "nl_queries": {
                        "canonical": "Group accounts by frequency and output count.",
                        "colloquial": "Count accounts for each frequency.",
                    }
                }
            if agent_id == "rtv":
                rtv_payloads.append(dict(inputs))
                assert inputs["verification_mode"] == COMPILED_RTV_MODE
                assert inputs["reference_oracle"] == reference
                assert inputs["result_fields"] == ["_id", "count"]
                assert inputs["shape_policy"] == "reduce"
                assert inputs["compiled_gold_provenance"]["source"] == "workflow_direct_compile"
                assert set(inputs) == {
                    "verification_mode",
                    "reference_oracle",
                    "result_fields",
                    "shape_policy",
                    "compiled_gold_provenance",
                    "nl_queries",
                    "MQL",
                    "schema",
                }
                return {"rtv_pass": True, "rtv_mode": COMPILED_RTV_MODE}
            if agent_id == "nnc":
                return {"gate_pass": True, "difficulty": "L1", "sql_infeasibility_class": "feasible"}
            if agent_id == "ra":
                return {"ra_pass": True}
            raise AssertionError(agent_id)

    artifacts = {
        "financial": DbArtifacts(
            db_id="financial",
            mongodb_schema={"account": {"_id": "INT", "frequency": "TEXT"}},
            mongodb_data={"account": [{"_id": 1, "frequency": "monthly"}]},
            rationale={},
            world_signature="sha256:" + "7" * 64,
            scenario_summary="finance account grouping",
            query_bearing=True,
        )
    }
    slot = CoverageSlot(
        db_id="financial",
        mechanism="baseline",
        archetype="group_count",
        record_id=43,
        target_difficulty="L1",
        target_sql_infeasibility_class="feasible",
        target_schema_flex="none",
        reference_oracle_seed=reference,
    )

    record = asyncio.run(_build_record(_WF(), artifacts, slot))

    assert record is not None
    assert calls.get("ms", 0) == 0
    assert calls.get("rtv", 0) == 1
    assert rtv_payloads
    assert record["MQL"].startswith("db.account.aggregate")
    assert record["shape_policy"] == "reduce"
    assert any(e["event"] == "ms_reference_oracle_compiled" for e in _events(logger.run_dir))


def test_workflow_fallback_ms_compiled_flag_uses_normal_rtv_payload(stub_settings, logger) -> None:
    from tend.workflow.flows import CoverageSlot, DbArtifacts, _build_record

    reference = {
        "template": "group_count",
        "params": {"collection": "account", "group_by": "frequency"},
    }
    calls: dict[str, int] = {}
    rtv_payloads: list[dict] = []

    class _WF:
        def __init__(self):
            self.ctx = AgentContext(settings=stub_settings, llm=None, log=logger)

        def context(self, **fields):
            return self.ctx.bind(**fields)

        async def agent(self, agent_id, inputs, ctx=None):
            calls[agent_id] = calls.get(agent_id, 0) + 1
            if agent_id == "qps":
                return {
                    "intent": {
                        "seed_mechanism": "baseline",
                        "archetype": "group_count",
                        "shape_policy": "reduce",
                    },
                }
            if agent_id == "ms":
                return {
                    "gold_locked": True,
                    "compiled_reference_oracle": True,
                    "compiled_gold_provenance": {"source": "ms_fallback"},
                    "MQL": (
                        'db.account.aggregate([{ "$group": { "_id": "$frequency", '
                        '"count": { "$sum": 1 } } }])'
                    ),
                    "canonical_form_set": {"must_contain": ["$group"]},
                    "shape_policy": "reduce",
                    "schema_flex": "none",
                    "result_fields": ["_id", "count"],
                }
            if agent_id == "mut":
                return {"mutations": [{"mutation_id": f"m{i}", "MQL": "x"} for i in range(5)]}
            if agent_id == "pv":
                return {"pv_pass": True, "property_verification": {}}
            if agent_id == "nlp":
                return {
                    "nl_queries": {
                        "canonical": "Group accounts by frequency and output count.",
                        "colloquial": "Count accounts for each frequency.",
                    }
                }
            if agent_id == "rtv":
                rtv_payloads.append(dict(inputs))
                assert set(inputs) == {"nl_queries", "MQL", "schema"}
                assert "verification_mode" not in inputs
                assert "reference_oracle" not in inputs
                assert "compiled_gold_provenance" not in inputs
                return {"rtv_pass": True}
            if agent_id == "nnc":
                return {"gate_pass": True, "difficulty": "L1", "sql_infeasibility_class": "feasible"}
            if agent_id == "ra":
                return {"ra_pass": True}
            raise AssertionError(agent_id)

    artifacts = {
        "financial": DbArtifacts(
            db_id="financial",
            mongodb_schema={"account": {"_id": "INT", "frequency": "TEXT"}},
            mongodb_data={"account": [{"_id": 1, "frequency": "monthly"}]},
            rationale={},
            world_signature="sha256:" + "8" * 64,
            scenario_summary="finance account grouping",
            query_bearing=True,
        )
    }
    slot = CoverageSlot(
        db_id="financial",
        mechanism="baseline",
        archetype="group_count",
        record_id=44,
        target_difficulty="L1",
        target_sql_infeasibility_class="feasible",
        target_schema_flex="none",
        reference_oracle_seed=reference,
    )

    record = asyncio.run(_build_record(_WF(), artifacts, slot))

    assert record is not None
    assert calls.get("ms", 0) == 1
    assert calls.get("rtv", 0) == 1
    assert rtv_payloads
    assert not any(e["event"] == "ms_reference_oracle_compiled" for e in _events(logger.run_dir))


def test_workflow_fallback_rtv_mode_spoof_still_logs_round_trip(stub_settings, logger) -> None:
    from tend.workflow.flows import CoverageSlot, DbArtifacts, _build_record

    reference = {
        "template": "group_count",
        "params": {"collection": "account", "group_by": "frequency"},
    }
    rtv_payloads: list[dict] = []

    class _WF:
        def __init__(self):
            self.ctx = AgentContext(settings=stub_settings, llm=None, log=logger)

        def context(self, **fields):
            return self.ctx.bind(**fields)

        async def agent(self, agent_id, inputs, ctx=None):
            if agent_id == "qps":
                return {
                    "intent": {
                        "seed_mechanism": "baseline",
                        "archetype": "group_count",
                        "shape_policy": "reduce",
                    },
                }
            if agent_id == "ms":
                return {
                    "gold_locked": True,
                    "compiled_reference_oracle": True,
                    "compiled_gold_provenance": {"source": "ms_fallback"},
                    "MQL": (
                        'db.account.aggregate([{ "$group": { "_id": "$frequency", '
                        '"count": { "$sum": 1 } } }])'
                    ),
                    "canonical_form_set": {"must_contain": ["$group"]},
                    "shape_policy": "reduce",
                    "schema_flex": "none",
                    "result_fields": ["_id", "count"],
                }
            if agent_id == "mut":
                return {"mutations": [{"mutation_id": f"m{i}", "MQL": "x"} for i in range(5)]}
            if agent_id == "pv":
                return {"pv_pass": True, "property_verification": {}}
            if agent_id == "nlp":
                return {
                    "nl_queries": {
                        "canonical": "Group accounts by frequency.",
                        "colloquial": "Group accounts.",
                    }
                }
            if agent_id == "rtv":
                rtv_payloads.append(dict(inputs))
                return {
                    "rtv_pass": False,
                    "rtv_mode": COMPILED_RTV_MODE,
                    "rtv_reason": "normal round-trip failure",
                }
            if agent_id == "ra":
                return {"ra_pass": True}
            raise AssertionError(agent_id)

    artifacts = {
        "financial": DbArtifacts(
            db_id="financial",
            mongodb_schema={"account": {"_id": "INT", "frequency": "TEXT"}},
            mongodb_data={"account": [{"_id": 1, "frequency": "monthly"}]},
            rationale={},
            world_signature="sha256:" + "8" * 64,
            scenario_summary="finance account grouping",
            query_bearing=True,
        )
    }
    slot = CoverageSlot(
        db_id="financial",
        mechanism="baseline",
        archetype="group_count",
        record_id=45,
        target_difficulty="L1",
        target_sql_infeasibility_class="feasible",
        target_schema_flex="none",
        reference_oracle_seed=reference,
    )

    record = asyncio.run(_build_record(_WF(), artifacts, slot))
    events = _events(logger.run_dir)
    reject = next(e for e in events if e["event"] == "rtv_reject")
    drop = next(e for e in events if e["event"] == "record_dropped" and e["stage"] == "rtv")

    assert record is None
    assert all("verification_mode" not in payload for payload in rtv_payloads)
    assert reject["rtv_mode"] == "round_trip"
    assert reject["agent_rtv_mode"] == COMPILED_RTV_MODE
    assert reject["reason"] == "normal round-trip failure"
    assert drop["rtv_mode"] == "round_trip"
    assert drop["agent_rtv_mode"] == COMPILED_RTV_MODE
