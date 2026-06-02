from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from tend.agents import AgentContext
from tend.agents.phase_b import NlParaphraser, QueryPlanSampler
from tend.config import Settings
from tend.observability import setup_logging


ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def _design_mode_intent() -> dict:
    return {
        "intent": {
            "seed_mechanism": "sparse_embed",
            "seed_signal": {
                "collection": "account",
                "field": "loan",
                "presence": {"present": 682, "total": 4500},
            },
            "archetype": "present_missing_projection",
            "domain_framing": {
                "entity_noun": "account",
                "metric_noun": "loan_to_credit_ratio",
            },
            "analytical_op": {
                "per": "account",
                "target_field": "loan_to_credit_ratio",
                "metric_source_fields": ["loan.amount", "trans.amount", "trans.type"],
                "aggregation": "sum trans.amount per account where trans.type is PRIJEM",
                "compute": "if loan is present then loan.amount divided by credit_sum else 0",
                "missing_default_semantics": {"loan": "missing loan emits 0"},
            },
            "shape_policy": "preserve",
            "semantic_properties": [
                {"id": "loan_present_branch", "expect": "loan-present accounts use loan.amount"},
                {"id": "loan_missing_branch", "expect": "loan-missing accounts emit 0"},
            ],
            "target_difficulty": "L4",
        },
        "qps_trace": {
            "coverage_cell": "sparse_embed|present_missing_projection|finance",
            "deficit_weight": 0.22,
            "supply_constrained": False,
        },
    }


def test_intent_schema_accepts_design_mode_without_oracle_and_rejects_oracle_keys() -> None:
    schema = _load_json("proposals/schemas/intent.schema.json")
    validator = Draft202012Validator(schema)

    valid = _design_mode_intent()
    top_level_oracle = {**valid, "reference_oracle": {"template": "group_count", "params": {}}}
    nested_oracle = json.loads(json.dumps(valid))
    nested_oracle["intent"]["reference_oracle"] = {"template": "group_count", "params": {}}

    assert list(validator.iter_errors(valid)) == []
    assert any(
        "reference_oracle" in error.message
        for error in validator.iter_errors(top_level_oracle)
    )
    assert any(
        "reference_oracle" in error.message
        for error in validator.iter_errors(nested_oracle)
    )


def test_qps_prompt_and_runtime_contract_keep_oracle_hidden(tmp_path: Path) -> None:
    static_prompt = (ROOT / "proposals/agent_prompts/qps_query_plan_sampler.md").read_text(
        encoding="utf-8"
    )
    settings = Settings.from_env(overrides={"TEND_LLM_STUB": "1"}, run_id="pytest")
    ctx = AgentContext(settings=settings, llm=None, log=setup_logging(tmp_path / "run"))
    runtime_prompt = QueryPlanSampler().render_inputs(
        ctx,
        {
            "archetype": "present_missing_projection",
            "llm_design_mode": True,
            "target_sql_infeasibility_class": "structural_schema_flex",
            "schema": {"account": {"_id": "INT", "loan": {"amount": "INT"}}},
        },
    )

    assert "Do not bind or emit a reference oracle" in static_prompt
    assert "Do not emit a top-level or nested `reference_oracle`" in static_prompt
    assert "reference_oracle is required" not in static_prompt
    assert "reference_oracle required" not in static_prompt
    assert "DO NOT emit reference_oracle" in runtime_prompt
    assert "reference_oracle" not in QueryPlanSampler.output_schema["properties"]
    assert "qps_trace" in QueryPlanSampler.output_schema["properties"]
    runtime_validator = Draft202012Validator(QueryPlanSampler.output_schema)
    valid = _design_mode_intent()
    no_trace = {"intent": valid["intent"]}
    top_level_oracle = {
        **valid,
        "reference_oracle": {"template": "group_count", "params": {}},
    }
    nested_oracle = json.loads(json.dumps(valid))
    nested_oracle["intent"]["reference_oracle"] = {"template": "group_count", "params": {}}
    copied_target_label = json.loads(json.dumps(valid))
    copied_target_label["intent"]["target_sql_infeasibility_class"] = "structural_schema_flex"

    assert list(runtime_validator.iter_errors(valid)) == []
    assert any("qps_trace" in error.message for error in runtime_validator.iter_errors(no_trace))
    assert any(
        "reference_oracle" in error.message
        for error in runtime_validator.iter_errors(top_level_oracle)
    )
    assert any(
        "reference_oracle" in error.message
        for error in runtime_validator.iter_errors(nested_oracle)
    )
    assert any(
        "target_sql_infeasibility_class" in error.message
        for error in runtime_validator.iter_errors(copied_target_label)
    )


def test_proposal_04_no_longer_marks_nlp_trace_required() -> None:
    proposal = (ROOT / "proposals/04_agent_framework.md").read_text(encoding="utf-8")

    assert "| Out | nlp_trace" not in proposal
    assert "| nlp_trace |" not in proposal


def test_nlp_runtime_and_static_schema_reject_nlp_trace(tmp_path: Path) -> None:
    agent = NlParaphraser()
    validator = Draft202012Validator(agent.output_schema)
    output_with_trace = {
        "nl_queries": {
            "canonical": "Group accounts by frequency and return the count for each group.",
            "colloquial": "Count accounts by type.",
        },
        "nlp_trace": {"rationale": "extra key must be rejected"},
    }
    static_prompt = (ROOT / "proposals/agent_prompts/nlp_nl_paraphraser.md").read_text(
        encoding="utf-8"
    )
    ctx = AgentContext(
        settings=Settings.from_env(overrides={"TEND_LLM_STUB": "1"}, run_id="pytest"),
        llm=None,
        log=setup_logging(tmp_path / "run"),
    )
    runtime_prompt = agent.render_inputs(
        ctx,
        {
            "intent": {
                "shape_policy": "reduce",
                "archetype": "group_count",
                "seed_signal": {"collection": "account", "field": "frequency"},
            }
        },
    )

    errors = list(validator.iter_errors(output_with_trace))

    assert agent.output_schema["additionalProperties"] is False
    assert set(agent.output_schema["required"]) == {"nl_queries"}
    assert any("nlp_trace" in error.message for error in errors)
    assert "nlp_trace" not in static_prompt
    assert "Do NOT emit nlp_trace" in runtime_prompt


def test_ms_prompt_and_proposal_match_runtime_optional_alternate_contract() -> None:
    prompt = (ROOT / "proposals/agent_prompts/ms_mql_synthesizer.md").read_text(
        encoding="utf-8"
    )
    proposal = (ROOT / "proposals/04_agent_framework.md").read_text(encoding="utf-8")

    assert "Execute at least two independent synthesis paths" not in prompt
    assert "≥2 条独立合成路径" not in proposal
    assert "optional `mql_alt`" in proposal
    assert "| Out | mql_alt | string | 可选" in proposal
    assert "runtime postprocess will derive required metadata" in prompt
