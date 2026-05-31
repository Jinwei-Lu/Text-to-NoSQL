"""4-panel pr aggregation and empirical difficulty bucketing."""

from __future__ import annotations

from typing import Any

from .fingerprint import mean_fingerprint
from .metrics import METRIC_KEYS

PANELS = ("small", "medium", "large", "frontier")
PR_FIELDS = ("pr_small", "pr_medium", "pr_large", "pr_frontier")


def empirical_difficulty_bucket(pr_medium: float) -> str:
    if pr_medium >= 0.8:
        return "easy"
    if pr_medium >= 0.5:
        return "medium"
    if pr_medium >= 0.2:
        return "hard"
    return "expert"


def aggregate_panels(
    fingerprints: list[dict[str, Any]],
    panel_pr_meta: dict[int, dict[str, float]],
) -> dict[str, Any]:
    """Bucket fingerprints by panel-derived empirical difficulty (pr_medium)."""
    panel_buckets: dict[str, list[tuple[int, ...]]] = {panel: [] for panel in PANELS}
    pr_values: dict[str, list[float]] = {field: [] for field in PR_FIELDS}
    difficulty_counts: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0, "expert": 0}

    for row in fingerprints:
        record_id = int(row["record_id"])
        pr = panel_pr_meta.get(record_id, {})
        pr_medium = float(pr.get("pr_medium", 0.5))
        bucket = empirical_difficulty_bucket(pr_medium)
        difficulty_counts[bucket] += 1

        dominant_panel = max(PANELS, key=lambda panel: float(pr.get(f"pr_{panel}", 0.0)))
        panel_buckets[dominant_panel].append(tuple(row["fp"]))

        for field in PR_FIELDS:
            if field in pr:
                pr_values[field].append(float(pr[field]))

    total = max(len(fingerprints), 1)
    empirical_difficulty = {
        bucket: round(count / total, 6) for bucket, count in difficulty_counts.items()
    }

    return {
        "observation_only": True,
        "solver_ex_by_panel": {
            panel: mean_fingerprint(panel_buckets[panel]).get("EX", 0.0) for panel in PANELS
        },
        "pr_distribution": {
            **{
                field: round(sum(values) / len(values), 6) if values else 0.0
                for field, values in pr_values.items()
            },
            "empirical_difficulty": empirical_difficulty,
        },
    }


def mean_metric_by_panel_bucket(
    fingerprints: list[dict[str, Any]],
    panel_pr_meta: dict[int, dict[str, float]],
    panel: str,
) -> dict[str, float]:
    rows: list[tuple[int, ...]] = []
    for row in fingerprints:
        record_id = int(row["record_id"])
        pr = panel_pr_meta.get(record_id, {})
        if max(PANELS, key=lambda p: float(pr.get(f"pr_{p}", 0.0))) == panel:
            rows.append(tuple(row["fp"]))
    return mean_fingerprint(rows)


def stub_panel_pr(records: list[dict[str, Any]], *, seed: float = 0.5) -> dict[int, dict[str, float]]:
    """Deterministic MVP stub: flat pr quadruple per record."""
    return {
        int(record["record_id"]): {
            "pr_small": seed,
            "pr_medium": seed,
            "pr_large": seed,
            "pr_frontier": seed,
        }
        for record in records
    }
