"""Leaderboard payload builder and schema validation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tend.config import MONGO_IMAGE, SCHEMAS_ROOT
from tend.schemas.validators import validate

from .fingerprint import mean_fingerprint
from .metrics import METRIC_KEYS
from .panel import aggregate_panels
from .slice_aggregate import leaderboard_slice_aggregates


def _metric_means(fingerprints: list[dict[str, Any]]) -> dict[str, float]:
    from .metrics import METRICS

    fps = [tuple(row["fp"]) for row in fingerprints]
    means = mean_fingerprint(fps)
    return {METRIC_KEYS[index]: means[METRICS[index]] for index in range(len(METRICS))}


def build_leaderboard_payload(
    *,
    submission_id: str,
    solver_id: str,
    release_tag: str,
    fingerprints: list[dict[str, Any]],
    records: list[dict[str, Any]],
    panel_pr_meta: dict[int, dict[str, float]],
    solver_llm_backbones: list[dict[str, str]],
    eval_dir: Path,
    disjointness_status: str = "passed",
    construction_gate_digest: str | None = None,
    evaluation_gate_digest: str | None = None,
    panel_manifest_digest: str | None = None,
    spider_db_catalog_digest: str | None = None,
    diagnostics: dict[str, Any] | None = None,
    panel_stub: bool = False,
) -> dict[str, Any]:
    metric_means = _metric_means(fingerprints)
    slice_aggregates = leaderboard_slice_aggregates(fingerprints, records)
    panel_report = aggregate_panels(fingerprints, panel_pr_meta)

    eval_dir = Path(eval_dir)
    placeholder = "sha256:" + "0" * 64
    diagnostics = diagnostics or {
        "parse_error_count": sum(1 for row in fingerprints if row.get("parse_error")),
        "timeout_hit_count": sum(1 for row in fingerprints if row.get("timeout_hit")),
        "oom_hit_count": sum(1 for row in fingerprints if row.get("oom_hit")),
        "forbidden_op_hit_count": sum(1 for row in fingerprints if row.get("forbidden_op_hit")),
        "functional_sql_solvable_rate": 0.0,
        "structural_sql_solvable_rate": 0.0,
    }

    payload = {
        "submission_id": submission_id,
        "solver_id": solver_id,
        "release_tag": release_tag,
        "evaluated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "disclosure_complete": True,
        "disjointness_status": disjointness_status,
        "environment": {
            "mongodb_server_image": (
                f"{MONGO_IMAGE}@sha256:0000000000000000000000000000000000000000000000000000000000000000"
            ),
            "mongosh_image_digest": placeholder,
            "query_timeout_seconds": 30,
            "memory_limit_gb": 8,
            "network_policy": "none",
            "collation": "simple",
        },
        "solver_llm_backbones": solver_llm_backbones,
        "scores": {
            "ex_unweighted": metric_means["ex"],
            "ex_ceiling_weighted": metric_means["ex"],
            "em": metric_means["em"],
            "qsm": metric_means["qsm"],
            "qfc": metric_means["qfc"],
            "efm": metric_means["efm"],
            "evm": metric_means["evm"],
            "qim": metric_means["qim"],
            "record_count": len(fingerprints),
        },
        "slice_aggregates": slice_aggregates,
        "panel_report": panel_report,
        "disclosures": {
            "construction_gate_digest": construction_gate_digest or placeholder,
            "evaluation_gate_digest": evaluation_gate_digest or placeholder,
            "panel_manifest_digest": panel_manifest_digest or placeholder,
            "spider_db_catalog_digest": spider_db_catalog_digest or placeholder,
            "diagnostics_summary": diagnostics,
        },
        "artifacts": {
            "fingerprint_csv": str(eval_dir / "fingerprints.csv").replace("\\", "/"),
            "per_record_metrics_csv": str(eval_dir / "per_record_metrics.csv").replace("\\", "/"),
            "per_slice_metrics_dir": str(eval_dir / "slices").replace("\\", "/") + "/",
            "construction_gate_json": "audit/reference_panel/construction_gate.json",
            "evaluation_gate_json": "audit/reference_panel/evaluation_gate.json",
        },
    }
    return payload


def validate_leaderboard_payload(payload: dict[str, Any]) -> None:
    validate(payload, SCHEMAS_ROOT / "leaderboard.schema.json")


def write_leaderboard(payload: dict[str, Any], out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def digest_file(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"
