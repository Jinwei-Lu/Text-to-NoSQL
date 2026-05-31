"""Mandatory 13-item disclosure checklist for TEND submissions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


DISCLOSURE_ITEMS: tuple[tuple[int, str, str], ...] = (
    (1, "seven_metric_csvs", "7 metrics × 3 aggregation levels (per-record / per-slice / per-panel)"),
    (2, "six_axis_slice_matrix", "Six-axis slice × metric matrix"),
    (3, "panel_pr_quadruple", "4-panel pr quadruple + empirical_difficulty distribution"),
    (4, "sql_bridge_diagnostic_slices", "functional_sql_solvable / structural_sql_solvable slice matrices"),
    (5, "nnc_difficulty_histogram", "NNC L-tier + sql_infeasibility_class distribution"),
    (6, "ra_realism_pass_rate", "RA realism audit pass rate"),
    (7, "disjointness_gate_digests", "construction_gate + evaluation_gate manifest digests"),
    (8, "panel_manifest_digests", "≥4 panel manifest SHA-256 digests"),
    (9, "spider_db_catalog_digest", "spider_db_catalog.json digest"),
    (10, "world_signature_digest", "release world_signature rollup digest"),
    (11, "runtime_digests", "MongoDB server + mongosh image digests"),
    (12, "solver_llm_backbones", "Solver LLM backbone ID list"),
    (13, "per_record_fingerprints", "record_id → 7-bit fingerprint CSV"),
)


@dataclass(frozen=True)
class DisclosureCheck:
    item_id: int
    key: str
    description: str
    present: bool
    path: str | None = None
    detail: str | None = None


def check_disclosure_artifacts(
    eval_dir: Path,
    *,
    leaderboard: dict[str, Any] | None = None,
    panel_stub: bool = False,
) -> list[DisclosureCheck]:
    eval_dir = Path(eval_dir)
    leaderboard = leaderboard or {}
    artifacts = leaderboard.get("artifacts", {})
    disclosures = leaderboard.get("disclosures", {})
    panel_report = leaderboard.get("panel_report", {})
    scores = leaderboard.get("scores", {})

    checks: list[DisclosureCheck] = []

    def add(item_id: int, key: str, description: str, present: bool, path: str | None = None, detail: str | None = None) -> None:
        checks.append(
            DisclosureCheck(
                item_id=item_id,
                key=key,
                description=description,
                present=present,
                path=path,
                detail=detail,
            )
        )

    add(
        1,
        "seven_metric_csvs",
        DISCLOSURE_ITEMS[0][2],
        (eval_dir / "per_record_metrics.csv").exists() and bool(scores),
        str(eval_dir / "per_record_metrics.csv"),
    )
    add(
        2,
        "six_axis_slice_matrix",
        DISCLOSURE_ITEMS[1][2],
        (eval_dir / "slices").is_dir() or bool(leaderboard.get("slice_aggregates")),
        str(eval_dir / "slices"),
    )
    add(
        3,
        "panel_pr_quadruple",
        DISCLOSURE_ITEMS[2][2],
        bool(panel_report.get("pr_distribution")) and not panel_stub,
        str(eval_dir / "panel_pr.json"),
        "panel_stub=true blocks official disclosure" if panel_stub else None,
    )
    add(
        4,
        "sql_bridge_diagnostic_slices",
        DISCLOSURE_ITEMS[3][2],
        "functional_sql_solvable_rate" in disclosures.get("diagnostics_summary", {}),
    )
    add(5, "nnc_difficulty_histogram", DISCLOSURE_ITEMS[4][2], (eval_dir / "nnc_histogram.json").exists())
    add(6, "ra_realism_pass_rate", DISCLOSURE_ITEMS[5][2], (eval_dir / "ra_pass_rate.json").exists())
    add(
        7,
        "disjointness_gate_digests",
        DISCLOSURE_ITEMS[6][2],
        bool(disclosures.get("construction_gate_digest"))
        and bool(disclosures.get("evaluation_gate_digest")),
    )
    add(
        8,
        "panel_manifest_digests",
        DISCLOSURE_ITEMS[7][2],
        bool(disclosures.get("panel_manifest_digest")),
    )
    add(
        9,
        "spider_db_catalog_digest",
        DISCLOSURE_ITEMS[8][2],
        bool(disclosures.get("spider_db_catalog_digest")),
    )
    add(10, "world_signature_digest", DISCLOSURE_ITEMS[9][2], (eval_dir / "world_signature_digest.txt").exists())
    add(
        11,
        "runtime_digests",
        DISCLOSURE_ITEMS[10][2],
        bool(leaderboard.get("environment", {}).get("mongodb_server_image")),
    )
    add(
        12,
        "solver_llm_backbones",
        DISCLOSURE_ITEMS[11][2],
        bool(leaderboard.get("solver_llm_backbones")),
    )
    add(
        13,
        "per_record_fingerprints",
        DISCLOSURE_ITEMS[12][2],
        Path(str(artifacts.get("fingerprint_csv", ""))).name.endswith(".csv")
        or (eval_dir / "fingerprints.csv").exists(),
        str(artifacts.get("fingerprint_csv", eval_dir / "fingerprints.csv")),
    )

    return checks


def disclosure_complete(checks: list[DisclosureCheck], *, require_panel: bool = True) -> bool:
    for check in checks:
        if check.key == "panel_pr_quadruple" and not require_panel:
            continue
        if not check.present:
            return False
    return True


def disclosure_report(checks: list[DisclosureCheck]) -> dict[str, Any]:
    return {
        "complete": disclosure_complete(checks),
        "items": [
            {
                "id": check.item_id,
                "key": check.key,
                "description": check.description,
                "present": check.present,
                "path": check.path,
                "detail": check.detail,
            }
            for check in checks
        ],
        "missing": [check.key for check in checks if not check.present],
    }
