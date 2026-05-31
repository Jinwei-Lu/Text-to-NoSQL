"""Schema Critic (SC) — 8 anti-pattern rules + flex pre-audit."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from tend.core import logging as log_module
from tend.errors import RetryBudgetExhausted
from tend.phase_a.sra import design_schema, eval_triggers

MAX_REJECT_ROUNDS = 2


@dataclass
class SCVerdict:
    verdict: str
    issues: list[dict[str, Any]] = field(default_factory=list)
    coverage_gaps: list[str] = field(default_factory=list)
    suggested_fixes: list[str] = field(default_factory=list)
    flex_eligible: bool = False
    flex_supply_report: dict[str, Any] = field(default_factory=dict)


def _join_depth_p95(wp_output: dict[str, Any]) -> int:
    dist = wp_output.get("join_depth_distribution", {})
    depth_keys = ["3+", "2", "1", "0"]
    cumulative = 0.0
    for key in depth_keys:
        cumulative += float(dist.get(key, 0.0))
        if cumulative >= 0.95:
            if key == "3+":
                return 3
            return int(key)
    return 2


def _hot_field_paths(wp_output: dict[str, Any]) -> set[str]:
    return {item["path"] for item in wp_output.get("hot_fields", [])}


def _access_pattern_roots(wp_output: dict[str, Any]) -> set[str]:
    roots: set[str] = set()
    for pattern in wp_output.get("access_patterns", []):
        tables = pattern.get("tables") or []
        if tables:
            roots.add(tables[0])
    return roots


def _referenced_constraints(wp_output: dict[str, Any], rationale: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    refs = " ".join(
        decision.get("reference", "") for decision in rationale.get("decisions", [])
    )
    if "access_patterns." in refs or "hot_fields." in refs or "heterogenization." in refs:
        return missing
    for idx, constraint in enumerate(wp_output.get("design_constraints", []), start=1):
        token = f"design_constraints.{idx - 1}"
        if constraint[:20] not in refs and token not in refs:
            missing.append(constraint)
    return missing


def _simulate_pattern_coverage(
    wp_output: dict[str, Any],
    schema: dict[str, Any],
    rationale: dict[str, Any],
) -> list[str]:
    gaps: list[str] = []
    patterns = wp_output.get("access_patterns", [])[:5]
    collections = set(schema.keys())
    patterns_applied = set(rationale.get("patterns_applied", []))
    for pattern in patterns:
        tables = pattern.get("tables") or []
        if not tables:
            continue
        root = tables[0]
        if root not in collections and not any(root in coll for coll in collections):
            if "mixed" not in patterns_applied and "embed" not in patterns_applied:
                gaps.append(pattern["pattern_id"])
            continue
        depth = len(tables) - 1
        if depth > 2 and "embed" not in patterns_applied and "extended_reference" not in patterns_applied:
            gaps.append(pattern["pattern_id"])
    return gaps


def _check_anti_patterns(
    wp_output: dict[str, Any],
    schema: dict[str, Any],
    rationale: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    collections = list(schema.keys())
    collection_count = len(collections)
    access_roots = _access_pattern_roots(wp_output)
    join_p95 = _join_depth_p95(wp_output)
    patterns_applied = set(rationale.get("patterns_applied", []))

    if collection_count > len(access_roots) + 1:
        issues.append(
            {
                "rule_id": "AP-UC-02",
                "severity": "error",
                "message": "Tier-1 collection count exceeds distinct root entities + 1",
                "evidence": f"collections={collection_count}, roots={len(access_roots)}",
            }
        )

    if collection_count > len(access_roots) + 1:
        for coll in collections:
            if coll in access_roots:
                continue
            issues.append(
                {
                    "rule_id": "AP-UC-01",
                    "severity": "warning",
                    "message": f"Collection {coll} may be unnecessary",
                    "evidence": "independent_query_hits=0 heuristic",
                }
            )

    lookup_budget = join_p95 + 1
    if lookup_budget < 2 and "embed" not in patterns_applied:
        issues.append(
            {
                "rule_id": "AP-EL-01",
                "severity": "error",
                "message": "Estimated $lookup chain exceeds join_depth budget",
                "evidence": f"budget={lookup_budget}, patterns={patterns_applied}",
            }
        )

    if join_p95 >= 3 and "bucket" not in patterns_applied:
        issues.append(
            {
                "rule_id": "AP-EL-02",
                "severity": "warning",
                "message": "Deep lookup chain without bucket exemption",
                "evidence": f"join_depth_p95={join_p95}",
            }
        )

    hot_fields = _hot_field_paths(wp_output)
    # Index anti-patterns apply only when explicit index metadata is declared.
    declared_indexes = rationale.get("indexes") or schema.get("__indexes") or []
    if declared_indexes:
        index_fields = {idx.get("field") for idx in declared_indexes if isinstance(idx, dict)}
        cold_indexes = index_fields - hot_fields - {"_id"}
        if len(cold_indexes) >= 3:
            issues.append(
                {
                    "rule_id": "AP-OI-01",
                    "severity": "warning",
                    "message": "Indexes on fields outside WP hot_fields top-20",
                    "evidence": ",".join(sorted(cold_indexes)[:5]),
                }
            )
        if collection_count > 0 and len(declared_indexes) > 3 * collection_count:
            issues.append(
                {
                    "rule_id": "AP-OI-02",
                    "severity": "error",
                    "message": "Index count exceeds 3× collection count",
                    "evidence": f"indexes={len(declared_indexes)}, collections={collection_count}",
                }
            )

    gaps = _simulate_pattern_coverage(wp_output, schema, rationale)
    for gap in gaps:
        issues.append(
            {
                "rule_id": "AP-WC-01",
                "severity": "error",
                "message": f"Access pattern {gap} not expressible in layout",
                "evidence": gap,
            }
        )

    missing_constraints = _referenced_constraints(wp_output, rationale)
    for constraint in missing_constraints:
        issues.append(
            {
                "rule_id": "AP-WC-02",
                "severity": "error",
                "message": "Design constraint lacks decisions[] reference",
                "evidence": constraint[:80],
            }
        )

    return issues


def _should_reject(issues: list[dict[str, Any]]) -> bool:
    errors = [issue for issue in issues if issue["severity"] == "error"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    if errors:
        return True
    warning_rules = Counter(rule["rule_id"] for rule in warnings)
    if warning_rules.get("AP-EL-02", 0) >= 2:
        return True
    if warning_rules.get("AP-OI-01", 0) >= 3:
        return True
    return False


def critique_schema(
    wp_output: dict[str, Any],
    schema: dict[str, Any],
    rationale: dict[str, Any],
    *,
    min_flex_db_ratio: float = 0.30,
    selected_flex_ratio: float | None = None,
) -> SCVerdict:
    """Run anti-pattern checks and flex pre-audit on one db."""
    issues = _check_anti_patterns(wp_output, schema, rationale)
    coverage_gaps = _simulate_pattern_coverage(wp_output, schema, rationale)
    trigger_report = eval_triggers(wp_output, db_id=wp_output["db_id"])
    flex_eligible = trigger_report["flex_eligible"]

    verdict = "pass" if not _should_reject(issues) else "reject"
    suggested_fixes: list[str] = []
    if verdict == "reject":
        for issue in issues:
            if issue["severity"] == "error":
                suggested_fixes.append(f"Address {issue['rule_id']}: {issue['message']}")

    ratio = selected_flex_ratio if selected_flex_ratio is not None else (1.0 if flex_eligible else 0.0)
    flex_report = {
        "min_flex_db_ratio": min_flex_db_ratio,
        "selected_flex_ratio": ratio,
        "supply_ceiling": ratio,
        "h7_relaxed": ratio < min_flex_db_ratio,
        "h9_relaxed": ratio < min_flex_db_ratio,
    }

    log_module.emit(
        "sc.verdict",
        db_id=wp_output["db_id"],
        agent="SC",
        stage="phase_a",
        verdict=verdict,
        issue_count=len(issues),
        flex_eligible=flex_eligible,
    )
    return SCVerdict(
        verdict=verdict,
        issues=issues,
        coverage_gaps=coverage_gaps,
        suggested_fixes=suggested_fixes,
        flex_eligible=flex_eligible,
        flex_supply_report=flex_report,
    )


def review_schema(
    wp_output: dict[str, Any],
    schema: dict[str, Any],
    rationale: dict[str, Any],
    *,
    min_flex_db_ratio: float = 0.30,
    selected_flex_ratio: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any], SCVerdict]:
    """Review schema with up to MAX_REJECT_ROUNDS SRA revisions."""
    current_schema, current_rationale = schema, rationale
    verdict: SCVerdict | None = None

    for round_idx in range(MAX_REJECT_ROUNDS + 1):
        verdict = critique_schema(
            wp_output,
            current_schema,
            current_rationale,
            min_flex_db_ratio=min_flex_db_ratio,
            selected_flex_ratio=selected_flex_ratio,
        )
        if verdict.verdict == "pass" or round_idx == MAX_REJECT_ROUNDS:
            break
        current_schema, current_rationale = design_schema(
            wp_output, db_id=wp_output["db_id"], revision=round_idx + 1
        )

    assert verdict is not None
    if verdict.verdict != "pass":
        raise RetryBudgetExhausted(
            f"SC rejected schema for {wp_output['db_id']} after {MAX_REJECT_ROUNDS} rounds"
        )

    current_rationale.setdefault("anti_pattern_checks", {"pass": True, "issues": []})
    current_rationale["anti_pattern_checks"] = {
        "pass": True,
        "issues": [issue["message"] for issue in verdict.issues if issue["severity"] == "error"],
    }
    return current_schema, current_rationale, verdict
