"""Six-pool LLM disjointness manifest and gate verification."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tend.config import load_pool_roster, pool_disjoint_strict
from tend.errors import DisjointnessViolation

POOL_NAMES = (
    "A_construct",
    "B_rtv",
    "C_nnc_attack",
    "B_panel",
    "S_solver",
    "D_bridge",
)


def collect_pool_assignments(roster: dict[str, Any]) -> dict[str, list[str]]:
    """Map each model id to one or more pool labels (duplicates allowed)."""
    assignments: dict[str, list[str]] = defaultdict(list)
    for pool, value in roster.items():
        if pool == "B_panel" and isinstance(value, dict):
            for bucket, models in value.items():
                for model in models:
                    label = f"B_panel.{bucket}"
                    if label not in assignments[model]:
                        assignments[model].append(label)
        elif isinstance(value, list):
            for model in value:
                if pool not in assignments[model]:
                    assignments[model].append(pool)
    return dict(assignments)


def flatten_pool_models(
    roster: dict[str, Any],
    *,
    strict: bool | None = None,
) -> dict[str, str]:
    """Return model -> primary pool label. Raises when strict and model is duplicated."""
    if strict is None:
        strict = pool_disjoint_strict()
    assignments = collect_pool_assignments(roster)
    mapping: dict[str, str] = {}
    for model, pools in assignments.items():
        if strict and len(pools) > 1:
            raise DisjointnessViolation(
                f"Duplicate model {model} in {pools[0]} and {pools[1]}"
            )
        if strict:
            if model in mapping:
                raise DisjointnessViolation(
                    f"Duplicate model {model} in {mapping[model]} and {pools[0]}"
                )
        mapping[model] = pools[0]
    return mapping


def verify_six_pool_disjoint(
    roster: dict[str, Any] | None = None,
    *,
    s_solver: list[str] | None = None,
    strict: bool | None = None,
) -> dict[str, Any]:
    if strict is None:
        strict = pool_disjoint_strict()

    roster = dict(roster or load_pool_roster())
    if s_solver is not None:
        roster["S_solver"] = list(s_solver)

    assignments = collect_pool_assignments(roster)
    unique_models = sorted(assignments)
    has_cross_pool_duplicates = any(len(pools) > 1 for pools in assignments.values())

    if strict:
        mapping = flatten_pool_models(roster, strict=True)
        shared_model_mode = False
    else:
        mapping = flatten_pool_models(roster, strict=False)
        shared_model_mode = has_cross_pool_duplicates or len(unique_models) <= 1

    pools: dict[str, set[str]] = {name: set() for name in POOL_NAMES}
    for model, pool_labels in assignments.items():
        for pool_label in pool_labels:
            root = pool_label.split(".", 1)[0]
            pools.setdefault(root, set()).add(model)

    violations: list[dict[str, str]] = []
    if strict:
        pool_list = list(pools.keys())
        for index, left in enumerate(pool_list):
            for right in pool_list[index + 1 :]:
                overlap = pools[left] & pools[right]
                if overlap:
                    violations.append(
                        {
                            "left": left,
                            "right": right,
                            "overlap": ",".join(sorted(overlap)),
                        }
                    )
        if violations:
            raise DisjointnessViolation(f"Pool overlap detected: {violations[:3]}")

    digest = hashlib.sha256(
        json.dumps(assignments, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "pool_count": len(POOL_NAMES),
        "model_count": len(unique_models),
        "manifest_digest": f"sha256:{digest}",
        "violations": violations,
        "shared_model_mode": shared_model_mode,
        "pool_disjoint_strict": strict,
        "pools": {pool: sorted(models) for pool, models in pools.items()},
        "model_assignments": {m: pools for m, pools in sorted(assignments.items())},
    }


def write_disjointness_manifest(
    out_dir: Path,
    *,
    release: str,
    roster: dict[str, Any] | None = None,
    s_solver: list[str] | None = None,
    strict: bool | None = None,
) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report = verify_six_pool_disjoint(roster, s_solver=s_solver, strict=strict)

    construction_gate = {
        **report,
        "gate": "construction",
        "scope": "A_construct vs B_panel vs C_nnc_attack vs D_bridge",
    }
    evaluation_gate = {
        **report,
        "gate": "evaluation",
        "scope": "S_solver vs A_construct vs B_panel",
        "s_solver": list((roster or load_pool_roster()).get("S_solver", s_solver or [])),
    }
    manifest = {
        **report,
        "release": release,
        "roster_yaml_digest": _roster_digest(),
    }

    paths = {
        "construction_gate": out_dir / "construction_gate.json",
        "evaluation_gate": out_dir / "evaluation_gate.json",
        "manifest": out_dir / f"manifest_{release}.json",
    }
    paths["construction_gate"].write_text(
        json.dumps(construction_gate, indent=2), encoding="utf-8"
    )
    paths["evaluation_gate"].write_text(
        json.dumps(evaluation_gate, indent=2), encoding="utf-8"
    )
    paths["manifest"].write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return paths


def _roster_digest() -> str:
    path = Path(__file__).resolve().parents[1] / "core" / "llm_pools.yaml"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def load_manifest_digest(manifest_path: Path) -> str:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    return str(payload.get("manifest_digest", ""))
