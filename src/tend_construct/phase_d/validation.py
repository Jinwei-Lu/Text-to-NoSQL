from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tend_core import (
    Certificate,
    ExecutionBackend,
    ast_check,
    build_execution_backend,
    load_json,
)
from tend_benchmark.metrics import rec_equiv
from .external_runner import ExternalModelRunner, NoopExternalModelRunner
from .failure_bank import instantiate_failure_modes, load_failure_bank


# ---------------------------------------------------------------------------
# V_diverse – 10-axis diversity tracking with min/max quotas
# ---------------------------------------------------------------------------

DIVERSITY_AXES = (
    "phenomenon_class",
    "persona_id",
    "pattern_family",
    "nosql_nativeness_level",
    "shape_policy",
    "difficulty_label",
    "collection_name",
    "time_field_present",
    "array_field_present",
    "operator_count_bucket",
)


@dataclass
class DiversityState:
    axis_counts: dict[str, Counter] = field(default_factory=lambda: {axis: Counter() for axis in DIVERSITY_AXES})
    global_count: int = 0
    min_quota_per_cell: int = 1
    max_quota_per_cell: int = 999

    def bump(self, record: dict[str, Any], si: dict[str, Any]) -> dict[str, Any]:
        intent = si.get("intent", {})
        props = si.get("properties", {})
        cell_key = "/".join([
            intent.get("phenomenon_class", "?"),
            intent.get("persona_id", "?"),
            intent.get("pattern_family", "?"),
        ])

        axis_values = {
            "phenomenon_class": intent.get("phenomenon_class", "?"),
            "persona_id": intent.get("persona_id", "?"),
            "pattern_family": intent.get("pattern_family", "?"),
            "nosql_nativeness_level": si.get("nosql_nativeness", {}).get("level", "?"),
            "shape_policy": si.get("output", {}).get("shape_policy", "?"),
            "difficulty_label": record.get("empirical_difficulty", "unknown"),
            "collection_name": intent.get("collection", "?"),
            "time_field_present": "yes" if intent.get("time_field") else "no",
            "array_field_present": "yes" if intent.get("array_field") else "no",
            "operator_count_bucket": _op_count_bucket(record.get("MQL", "")),
        }

        deltas: dict[str, float] = {}
        for axis, value in axis_values.items():
            before = self.axis_counts[axis][value]
            self.axis_counts[axis][value] = before + 1
            deltas[axis] = round(1 / (before + 1), 4)

        self.global_count += 1
        delta_f = min(deltas.values()) if deltas else 0.0
        under_quota = any(
            count < self.min_quota_per_cell
            for counter in self.axis_counts.values()
            for count in counter.values()
        )

        return {
            "cell": cell_key,
            "axis_values": axis_values,
            "axis_deltas": deltas,
            "global_count": self.global_count,
            "min_quota": self.min_quota_per_cell,
            "max_quota": self.max_quota_per_cell,
            "delta_f": round(delta_f, 4),
            "epsilon": 0.0,
            "under_quota": under_quota,
            "pass": True,
        }


def _op_count_bucket(mql: str) -> str:
    from tend_core.mql import extract_operator_tokens
    count = len(set(extract_operator_tokens(mql)))
    if count <= 2:
        return "1-2"
    if count <= 5:
        return "3-5"
    if count <= 8:
        return "6-8"
    return "9+"


# ---------------------------------------------------------------------------
# V_correct / V_discrim runners
# ---------------------------------------------------------------------------

class VCorrectRunner:
    def run(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


class VDiscrimRunner:
    def run(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError


class StubVCorrectRunner(VCorrectRunner):
    def __init__(self, backend: ExecutionBackend, model_runner: ExternalModelRunner | None = None):
        self.backend = backend
        self.model_runner = model_runner or NoopExternalModelRunner()

    def run(self, record: Any, witness: dict[str, Any], schema: dict[str, Any] | None = None) -> dict[str, Any]:
        gold_result = self.backend.norm_exec(record, record.mql, witness)
        response = self.model_runner.generate(
            "v_correct_neighborhood",
            {
                "prompt": (
                    f"NLQ: {record.nl_queries[0]}\n"
                    f"Schema: {schema or {}}\n"
                    f"Witness sample size: {sum(len(v) for v in witness.values())}\n"
                    "Return JSON {\"candidates\": [\"db.collection.aggregate(...)\", ...]} with alternative MQL candidates."
                )
            },
        )
        candidates = response.get("candidates", [])
        all_candidates = [
            {
                "model": "gold-seed",
                "ast_pass": ast_check(record.mql, record.canonical_form_set) == "pass",
                "exec_equiv": True,
                "action": "gold_validated",
            }
        ]
        ambiguous = False
        for candidate in candidates:
            try:
                candidate_result = self.backend.norm_exec(record, candidate, witness)
                exec_equiv = rec_equiv(candidate_result, gold_result)
            except Exception:
                exec_equiv = False
            ast_pass = ast_check(candidate, record.canonical_form_set) == "pass"
            if ast_pass and exec_equiv:
                action = "added_variant"
            elif ast_pass and not exec_equiv:
                action = "overwide_failure"
                ambiguous = True
            elif (not ast_pass) and exec_equiv:
                action = "semantic_hole_failure"
                ambiguous = True
            else:
                action = "pass"
            all_candidates.append(
                {
                    "model": "external-neighborhood",
                    "ast_pass": ast_pass,
                    "exec_equiv": exec_equiv,
                    "action": action,
                }
            )
        return {
            "neighborhood": {
                "models": ["gold-seed", "external-neighborhood"],
                "all_candidates": all_candidates,
            },
            "ambiguity": {
                "attacker_model": "external-neighborhood",
                "si_candidates_count": len(candidates),
                "si_equiv_to_gold_count": sum(1 for item in all_candidates if item["exec_equiv"]),
                "ambiguous": ambiguous,
            },
            "gold_result_size": len(gold_result),
            "pass": not ambiguous,
        }


class StubVDiscrimRunner(VDiscrimRunner):
    def __init__(
        self,
        backend: ExecutionBackend,
        failure_bank: dict[str, list[dict[str, Any]]] | None = None,
        model_runner: ExternalModelRunner | None = None,
    ):
        self.backend = backend
        self.failure_bank = failure_bank or {}
        self.model_runner = model_runner or NoopExternalModelRunner()

    def run(
        self,
        record: Any,
        witness: dict[str, Any],
        mutations: list[dict[str, Any]],
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gold_result = self.backend.norm_exec(record, record.mql, witness)
        ex_one_count = 0
        details: list[dict[str, Any]] = []
        context = _render_failure_context(record)
        bank_mutations = instantiate_failure_modes(record.operator_family or "", self.failure_bank, context)
        all_mutations = mutations + bank_mutations

        for mutation in all_mutations:
            try:
                result = self.backend.norm_exec(record, mutation["query"], witness)
                equivalent = rec_equiv(result, gold_result)
            except Exception:
                equivalent = False
            if equivalent:
                ex_one_count += 1
            details.append(
                {
                    "mutation_id": mutation["mutation_id"],
                    "equivalent": equivalent,
                    "source": mutation.get("source", "dynamic"),
                }
            )

        sql_bridge = self._run_external_bridge("sql_bridge", record, schema or {}, witness, gold_result)
        template_bridge = self._run_external_bridge("template_bridge", record, schema or {}, witness, gold_result)
        return {
            "failure_modes": {
                "total": len(all_mutations),
                "ex_one_count": ex_one_count,
                "threshold": 0.02,
                "details": details,
                "pass": ex_one_count == 0,
            },
            "dual_bridge": {
                "sql_bridge": sql_bridge,
                "template_bridge": template_bridge,
                "pass": not (sql_bridge["ex"] == 1 and sql_bridge["qim"] == 1)
                and not (template_bridge["ex"] == 1 and template_bridge["qim"] == 1),
            },
            "pass": ex_one_count == 0
            and not (sql_bridge["ex"] == 1 and sql_bridge["qim"] == 1)
            and not (template_bridge["ex"] == 1 and template_bridge["qim"] == 1),
        }

    def _run_external_bridge(
        self,
        task_name: str,
        record: Any,
        schema: dict[str, Any],
        witness: dict[str, Any],
        gold_result: list[dict[str, Any]],
    ) -> dict[str, int]:
        response = self.model_runner.generate(
            task_name,
            {
                "prompt": (
                    f"NLQ: {record.nl_queries[0]}\n"
                    f"Schema: {schema}\n"
                    "Return JSON {\"candidates\": [\"db.collection.aggregate(...)\"]} with one candidate query."
                )
            },
        )
        candidates = response.get("candidates", [])
        if not candidates:
            return {"ex": 0, "qim": 0}
        candidate = candidates[0]
        qim = int(ast_check(candidate, record.canonical_form_set) == "pass")
        try:
            candidate_result = self.backend.norm_exec(record, candidate, witness)
            ex = int(qim == 1 and rec_equiv(candidate_result, gold_result))
        except Exception:
            ex = 0
        return {"ex": ex, "qim": qim}


class MockVCorrectRunner(VCorrectRunner):
    def run(self, record: Any, witness: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        return {
            "neighborhood": {
                "models": ["mock-neighborhood"],
                "all_candidates": [
                    {
                        "model": "mock-neighborhood",
                        "ast_pass": ast_check(record.mql, record.canonical_form_set) == "pass",
                        "exec_equiv": True,
                        "action": "mock_accepted",
                    }
                ],
            },
            "ambiguity": {
                "attacker_model": "mock-ambiguity",
                "si_candidates_count": 1,
                "si_equiv_to_gold_count": 1,
                "ambiguous": False,
            },
            "gold_result_size": len(witness.get(record.db_id, [])) if isinstance(witness, dict) else 0,
            "pass": True,
        }


class MockVDiscrimRunner(VDiscrimRunner):
    def run(self, record: Any, witness: dict[str, Any], mutations: list[dict[str, Any]], **_kwargs: Any) -> dict[str, Any]:
        return {
            "failure_modes": {
                "total": len(mutations),
                "ex_one_count": 0,
                "threshold": 0.02,
                "details": [{"mutation_id": item["mutation_id"], "equivalent": False} for item in mutations],
                "pass": True,
            },
            "dual_bridge": {
                "sql_bridge": {"ex": 0, "qim": 0},
                "template_bridge": {"ex": 0, "qim": 0},
                "pass": True,
            },
            "pass": True,
        }


# ---------------------------------------------------------------------------
# calibrate_difficulty – using operator family complexity
# ---------------------------------------------------------------------------

_DIFFICULTY_MAP: dict[str, tuple[str, tuple[float, float, float, float]]] = {
    "simple_filter": ("easy", (0.9, 0.8, 0.7, 0.6)),
    "project_only": ("easy", (0.95, 0.85, 0.75, 0.65)),
    "filter_then_count": ("easy", (0.88, 0.78, 0.68, 0.55)),
    "top_k_by_aggregate": ("medium", (0.6, 0.5, 0.4, 0.3)),
    "filter_then_aggregate": ("medium", (0.6, 0.5, 0.4, 0.3)),
    "group_then_aggregate": ("medium", (0.55, 0.45, 0.35, 0.25)),
    "time_window_aggregate": ("medium", (0.55, 0.45, 0.35, 0.25)),
    "coalesce_with_default": ("medium", (0.6, 0.5, 0.4, 0.3)),
    "null_vs_missing_disambig": ("medium", (0.5, 0.4, 0.3, 0.2)),
    "type_introspection": ("medium", (0.5, 0.4, 0.3, 0.2)),
    "array_positional_select": ("medium", (0.55, 0.45, 0.35, 0.25)),
    "universal_quantifier": ("medium", (0.5, 0.4, 0.3, 0.2)),
    "existential_quantifier": ("medium", (0.5, 0.4, 0.3, 0.2)),
    "facet_split": ("hard", (0.4, 0.3, 0.2, 0.1)),
    "anomaly_vs_baseline": ("hard", (0.35, 0.25, 0.15, 0.08)),
    "array_reshape": ("hard", (0.4, 0.3, 0.2, 0.1)),
    "lookup_join": ("hard", (0.4, 0.3, 0.2, 0.1)),
    "percentile_approximation": ("hard", (0.35, 0.25, 0.15, 0.08)),
    "window_function": ("hard", (0.4, 0.3, 0.2, 0.1)),
    "polymorphic_branch": ("extra_hard", (0.3, 0.2, 0.1, 0.05)),
    "dynamic_key_expansion": ("extra_hard", (0.3, 0.2, 0.1, 0.05)),
    "graph_recursive_deep": ("extra_hard", (0.25, 0.15, 0.08, 0.03)),
    "window_function_with_facet_filter": ("extra_hard", (0.25, 0.15, 0.08, 0.03)),
}


def calibrate_difficulty(record: Any) -> dict[str, Any]:
    family = record.operator_family or "simple_filter"
    label, pr = _DIFFICULTY_MAP.get(family, ("hard", (0.4, 0.3, 0.2, 0.1)))
    return {
        "target_difficulty": label,
        "pr_small": pr[0],
        "pr_medium": pr[1],
        "pr_large": pr[2],
        "pr_frontier": pr[3],
        "empirical_difficulty": label,
        "amplify_rounds": 0,
    }


def route_split(domain_id: str) -> dict[str, Any]:
    split = "test" if domain_id[0].lower() <= "m" else "train"
    return {"split": split, "reason": "cross_domain_holdout_selected" if split == "test" else "default_train"}


# ---------------------------------------------------------------------------
# certify_record – top-level entry
# ---------------------------------------------------------------------------

def certify_record(
    bundle_root: Path,
    domain_id: str,
    record: Any,
    structured_intent: Any,
    schema: dict[str, Any],
    witness: dict[str, Any],
    mutations: list[dict[str, Any]],
    diversity_state: DiversityState,
    backend_name: str = "local-mongo",
    mongo_uri: str = "mongodb://localhost:27017",
    failure_bank_root: Path | None = None,
    model_runner: ExternalModelRunner | None = None,
) -> Certificate:
    model_runner = model_runner or NoopExternalModelRunner()
    failure_bank = load_failure_bank(failure_bank_root)
    if backend_name == "stub":
        v_correct = MockVCorrectRunner().run(record, witness)
        v_discrim = MockVDiscrimRunner().run(record, witness, mutations)
    else:
        backend = build_execution_backend(bundle_root=bundle_root, backend_name=backend_name, mongo_uri=mongo_uri)
        try:
            v_correct = StubVCorrectRunner(backend, model_runner=model_runner).run(record, witness, schema=schema)
            v_discrim = StubVDiscrimRunner(
                backend,
                failure_bank=failure_bank,
                model_runner=model_runner,
            ).run(record, witness, mutations, schema=schema)
        finally:
            backend.close()

    v_diverse = diversity_state.bump(record.to_dict(), structured_intent.to_dict())
    calibration = calibrate_difficulty(record)
    routing = route_split(domain_id)
    phase_d = {
        "v_correct": v_correct,
        "v_discrim": v_discrim,
        "v_diverse": v_diverse,
        "calibration": calibration,
    }
    return Certificate(
        record_id=f"{record.db_id}/{record.record_id}",
        db_id=record.db_id,
        si_hash=structured_intent.meta["si_hash"],
        world_signature=record.world_signature or "",
        split=routing["split"],
        empirical_difficulty=calibration["empirical_difficulty"],
        phase_d=phase_d,
        routing=routing,
    )


def load_stub_or_manifest(path: Path) -> dict[str, Any]:
    return load_json(path) if path.exists() else {}


def _render_failure_context(record: Any) -> dict[str, Any]:
    metric_field = record.raw.get("metric_field", "value")
    return {
        "collection": record.raw.get("collection", "items"),
        "mql": record.mql,
        "threshold": 70,
        "threshold_minus_20": 50,
        "top_k": 3,
        "metric_field": metric_field,
        "label_field": record.raw.get("label_field", "label"),
        "category_field": record.raw.get("category_field", "category"),
        "time_field": record.raw.get("time_field", "year"),
        "array_field": record.raw.get("array_field", "values"),
        "metric_alias": metric_field.split(".")[-1] if isinstance(metric_field, str) else "value",
    }
