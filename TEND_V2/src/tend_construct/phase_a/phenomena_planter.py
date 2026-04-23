from __future__ import annotations

from copy import deepcopy
from random import Random
from typing import Any

from tend_core import (
    PhenomenonEvidence,
    PhenomenonRecord,
    detector_signature,
    world_signature,
)


def plant_phenomena(
    db_id: str,
    template: dict[str, Any],
    witness_data: dict[str, list[dict[str, Any]]],
    phenomena_seed: int,
    noise_seed: int,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any], dict[str, Any]]:
    data = deepcopy(witness_data)
    planted: list[PhenomenonRecord] = []
    trace: dict[str, Any] = {
        "db_id": db_id,
        "phenomena_seed": phenomena_seed,
        "decisions": [],
    }

    for blueprint in template.get("phenomenon_blueprints", []):
        collection = blueprint["collection"]
        path = blueprint["path"]
        phenomenon_class = blueprint["phenomenon_class"]
        rng = Random(phenomena_seed + hash(phenomenon_class) % 10000)
        document_ids = _apply_blueprint(data[collection], path, phenomenon_class, rng)
        phenomenon = PhenomenonRecord(
            phenomenon_id=f"{phenomenon_class}@{path}",
            phenomenon_class=phenomenon_class,
            witness_evidence=PhenomenonEvidence(
                collection=collection,
                path=path,
                document_ids=tuple(f"{collection}/{document_id}" for document_id in document_ids),
                summary={"seed": phenomena_seed},
            ),
            detector_signature=detector_signature(phenomenon_class, "v1"),
            intent_hooks=tuple(blueprint.get("intent_hooks", [])),
            provenance={"source": "phase_a_planter"},
        )
        planted.append(phenomenon)
        trace["decisions"].append(
            {
                "phenomenon_id": phenomenon.phenomenon_id,
                "collection": collection,
                "path": path,
                "document_ids": list(phenomenon.witness_evidence.document_ids),
            }
        )

    registry = {
        "db_id": db_id,
        "noise_seed": noise_seed,
        "phenomena_seed": phenomena_seed,
        "world_signature": world_signature(data),
        "phenomena": [item.to_dict() for item in planted],
    }
    return data, registry, trace


def _apply_blueprint(
    documents: list[dict[str, Any]],
    path: str,
    phenomenon_class: str,
    rng: Random,
) -> list[int]:
    if not documents:
        return []

    handler = _PHENOMENON_HANDLERS.get(phenomenon_class, _apply_generic)
    return handler(documents, path, rng)


def _apply_temporal_trend(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    for index, document in enumerate(documents):
        _set_path_value(document, path, 50 + index * 10)
    return [document["_id"] for document in documents[: min(5, len(documents))]]


def _apply_null_cluster(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    chosen = documents[: min(3, len(documents))]
    for document in chosen:
        _set_path_value(document, path, None)
    return [document["_id"] for document in chosen]


def _apply_outlier(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    target = documents[-1]
    current = _get_path_value(target, path)
    boosted = 999 if not isinstance(current, (int, float)) else current * 8
    _set_path_value(target, path, boosted)
    return [target["_id"]]


def _apply_high_cardinality(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    for document in documents:
        _set_path_value(document, path, f"{path}_{document['_id']}")
    return [document["_id"] for document in documents[: min(8, len(documents))]]


def _apply_cross_group_comparison(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    for index, document in enumerate(documents):
        _set_path_value(document, path, 20 + (index % 3) * 40)
    return [document["_id"] for document in documents[: min(6, len(documents))]]


def _apply_rare_event(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    target = documents[-1]
    _set_path_value(target, path, 1 if isinstance(_get_path_value(target, path), (int, float)) else "rare")
    return [target["_id"]]


def _apply_pollution(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    for document in documents[: min(3, len(documents))]:
        current = _get_path_value(document, path)
        if isinstance(current, str):
            _set_path_value(document, path, f"??{current}??")
    return [document["_id"] for document in documents[: min(3, len(documents))]]


def _apply_type_drift(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    target = documents[0]
    current = _get_path_value(target, path)
    if isinstance(current, (int, float)):
        _set_path_value(target, path, str(current))
    elif isinstance(current, str):
        _set_path_value(target, path, 999)
    return [target["_id"]]


# --- 7 new phenomena ---

def _apply_skewed_distribution(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    n = len(documents)
    for i, document in enumerate(documents):
        if i < n * 0.7:
            _set_path_value(document, path, rng.randint(1, 20))
        else:
            _set_path_value(document, path, rng.randint(200, 500))
    return [document["_id"] for document in documents]


def _apply_periodic_pattern(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    import math
    for i, document in enumerate(documents):
        base = 50 + 30 * math.sin(2 * math.pi * i / max(len(documents), 1))
        _set_path_value(document, path, round(base + rng.uniform(-5, 5), 2))
    return [document["_id"] for document in documents[: min(6, len(documents))]]


def _apply_duplicate_cluster(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    if len(documents) < 4:
        return [documents[0]["_id"]]
    anchor_value = _get_path_value(documents[0], path)
    for document in documents[1:min(4, len(documents))]:
        _set_path_value(document, path, anchor_value)
    return [document["_id"] for document in documents[:4]]


def _apply_sparse_field(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    affected: list[int] = []
    for document in documents:
        if rng.random() < 0.6:
            parts = path.split(".")
            current = document
            for part in parts[:-1]:
                current = current.get(part, {})
            if isinstance(current, dict) and parts[-1] in current:
                del current[parts[-1]]
                affected.append(document["_id"])
    return affected or [documents[0]["_id"]]


def _apply_correlation(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    for i, document in enumerate(documents):
        base = 10 + i * 7
        _set_path_value(document, path, round(base + rng.uniform(-3, 3), 2))
    return [document["_id"] for document in documents[: min(6, len(documents))]]


def _apply_boundary_value(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    affected: list[int] = []
    for i, document in enumerate(documents[:min(4, len(documents))]):
        if i % 2 == 0:
            _set_path_value(document, path, 0)
        else:
            _set_path_value(document, path, 999)
        affected.append(document["_id"])
    return affected


def _apply_hierarchical_nesting(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    for document in documents[:min(3, len(documents))]:
        _set_path_value(document, path, rng.randint(1, 100))
    return [document["_id"] for document in documents[:min(3, len(documents))]]


def _apply_generic(documents: list[dict[str, Any]], path: str, rng: Random) -> list[int]:
    return [documents[0]["_id"]]


_PHENOMENON_HANDLERS = {
    "temporal_trend": _apply_temporal_trend,
    "null_cluster": _apply_null_cluster,
    "outlier": _apply_outlier,
    "high_cardinality": _apply_high_cardinality,
    "cross_group_comparison": _apply_cross_group_comparison,
    "rare_event": _apply_rare_event,
    "pollution": _apply_pollution,
    "type_drift": _apply_type_drift,
    "skewed_distribution": _apply_skewed_distribution,
    "periodic_pattern": _apply_periodic_pattern,
    "duplicate_cluster": _apply_duplicate_cluster,
    "sparse_field": _apply_sparse_field,
    "correlation": _apply_correlation,
    "boundary_value": _apply_boundary_value,
    "hierarchical_nesting": _apply_hierarchical_nesting,
}


def _get_path_value(document: dict[str, Any], path: str) -> Any:
    current: Any = document
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _set_path_value(document: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = document
    for part in parts[:-1]:
        next_value = current.get(part)
        if not isinstance(next_value, dict):
            next_value = {}
            current[part] = next_value
        current = next_value
    current[parts[-1]] = value
