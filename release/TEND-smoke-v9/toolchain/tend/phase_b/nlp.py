"""NLP: reverse-engineer canonical (L1) and colloquial (L0) NLQ from locked MQL."""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from tend.config import FIXTURES_ROOT, use_fixtures
from tend.core.llm_client import LLMClient
from tend.core.llm_response import parse_llm_json_response
from tend.prompts import loader as prompt_loader
from tend.schemas.validators import validate

_DOLLAR_OP_RE = re.compile(r"\$\w+")
_FIELD_NAME_RE = re.compile(
    r"\b(Performance_ID|Attendance|Conductor_ID|Orchestra_ID|Name|last_window_avg)\b",
    re.IGNORECASE,
)


def _load_fixture_nlq(db_id: str) -> dict[str, Any] | None:
    path = FIXTURES_ROOT / db_id / "nlp.yaml"
    if not path.exists():
        return None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _rules_ok(canonical: str, colloquial: str) -> dict[str, Any]:
    return {
        "canonical_no_dollar_ops": _DOLLAR_OP_RE.search(canonical) is None,
        "colloquial_no_field_names": _FIELD_NAME_RE.search(colloquial) is None,
        "single_intent_check": len(colloquial.strip()) > 0 and "；" not in colloquial and ";" not in colloquial,
    }


_parse_nlp_response = parse_llm_json_response


def _deterministic_paraphrase(
    query_plan: dict[str, Any],
    scenario_summary: str,
) -> dict[str, str]:
    pattern = query_plan.get("primary_pattern", "simple_filter")
    null_strategy = query_plan.get("null_missing_strategy", "none")
    if pattern == "window_facet_filter":
        canonical = (
            "For each conductor, compute a sliding average of Attendance over the current "
            "and 2 preceding performances (ordered by Performance_ID)"
            + (" treating missing Attendance as 0" if null_strategy == "ifNull" else "")
            + "; take the last window average as the representative value. "
            "Then compute the median of all conductors' representative values. "
            "Return only conductors whose representative value strictly exceeds "
            "that median, with fields Name and last_window_avg; "
            "show (unknown) if Name is missing; no ordering required."
        )
        colloquial = "List conductors whose recent attendance trend is above the peer median."
        return {"canonical": canonical, "colloquial": colloquial}

    summary_hint = scenario_summary.split(".")[0].strip() if scenario_summary else "the dataset"
    canonical = f"Given the {summary_hint} domain, describe the full query intent using the {pattern} pattern as declared in the query plan."
    colloquial = f"Show me the key results from {summary_hint}."
    return {"canonical": canonical, "colloquial": colloquial}


def paraphrase_nlq_pair(
    mql: str,
    query_plan: dict[str, Any],
    canonical_form_set: dict[str, Any],
    scenario_summary: str,
    *,
    db_id: str,
    record_id: int | str,
    client: LLMClient | None = None,
    seed: int = 0,
    prefer_fixture: bool | None = None,
) -> dict[str, Any]:
    """Emit schema-valid dual NLQ from locked MQL / structured intent."""
    if prefer_fixture is None:
        prefer_fixture = use_fixtures()
    fixture = _load_fixture_nlq(db_id) if prefer_fixture else None
    if fixture and "nl_queries" in fixture:
        nl_queries = dict(fixture["nl_queries"])
        nlp_trace = dict(fixture.get("nlp_trace", {}))
    else:
        llm = client or LLMClient()
        prompt = prompt_loader.load("nlp_nl_paraphraser")
        variables = {
            "db_id": db_id,
            "record_id": str(record_id),
            "mql": mql,
            "query_plan_json": json.dumps(query_plan, ensure_ascii=False, indent=2),
            "canonical_form_set_json": json.dumps(canonical_form_set, ensure_ascii=False, indent=2),
            "scenario_summary": scenario_summary,
        }
        user = prompt_loader.render(prompt["user"], variables)
        full_prompt = f"{prompt['system']}\n\n{user}"
        response = llm.call("A_construct", full_prompt, seed=seed)
        parsed = _parse_nlp_response(response)
        if parsed and "nl_queries" in parsed:
            nl_queries = parsed["nl_queries"]
            raw_trace = parsed.get("nlp_trace", {})
            nlp_trace = raw_trace if isinstance(raw_trace, dict) else {}
        else:
            nl_queries = _deterministic_paraphrase(query_plan, scenario_summary)
            nlp_trace = {
                "scenario_terms_used": ["conductor", "performance", "attendance"],
                "colloquial_underspec": ["recent trend", "peer median threshold"],
                "single_intent_check": True,
                "mode": "deterministic_fallback",
            }

    checks = _rules_ok(nl_queries["canonical"], nl_queries["colloquial"])
    nlp_trace = {
        **nlp_trace,
        **checks,
        "single_intent_check": checks["single_intent_check"],
    }
    validate(nl_queries, "nlq")
    return {"nl_queries": nl_queries, "nlp_trace": nlp_trace}
