"""Types shared by the database-specific native materializers.

Each module in ``designs/`` materializes one BIRD database as a MongoDB-native DataWorld and
returns a :class:`NativeExecutionResult`; its :class:`NativeFeatureManifest` lists the native
features Phase B plans its coverage slots over.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class NativeFeature:
    id: str
    type: str
    collection: str
    field: str = ""
    query_patterns: list[str] = dc_field(default_factory=list)
    required_constructs: list[str] = dc_field(default_factory=list)
    provenance_refs: list[str] = dc_field(default_factory=list)
    coverage: dict[str, Any] = dc_field(default_factory=dict)
    extra: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "type": self.type,
            "collection": self.collection,
            "field": self.field,
            "supported_query_patterns": list(self.query_patterns),
            "required_native_constructs": list(self.required_constructs),
            "provenance_refs": list(self.provenance_refs),
            "coverage": dict(self.coverage),
        }
        out.update(self.extra)
        return out


@dataclass
class NativeFeatureManifest:
    db_id: str
    features: list[NativeFeature] = dc_field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "db_id": self.db_id,
            "features": [feature.to_dict() for feature in self.features],
        }


@dataclass
class NativeExecutionResult:
    data: dict[str, list[dict[str, Any]]]
    schema: dict[str, Any]
    manifest: NativeFeatureManifest
    provenance: dict[str, Any]
    world_signature: str


_FEATURE_KEYS = frozenset(
    {
        "id",
        "type",
        "collection",
        "field",
        "supported_query_patterns",
        "required_native_constructs",
        "provenance_refs",
        "coverage",
    }
)


def load_native_feature_manifest(
    path_or_mapping: str | Path | dict[str, Any],
) -> NativeFeatureManifest:
    raw = _load_mapping(path_or_mapping)
    features: list[NativeFeature] = []
    for feature_raw in raw.get("features") or []:
        if not isinstance(feature_raw, dict):
            continue
        features.append(
            NativeFeature(
                id=str(feature_raw.get("id") or ""),
                type=str(feature_raw.get("type") or ""),
                collection=str(feature_raw.get("collection") or ""),
                field=str(feature_raw.get("field") or ""),
                query_patterns=[
                    str(value) for value in feature_raw.get("supported_query_patterns") or []
                ],
                required_constructs=[
                    str(value) for value in feature_raw.get("required_native_constructs") or []
                ],
                provenance_refs=[
                    str(value) for value in feature_raw.get("provenance_refs") or []
                ],
                coverage=dict(feature_raw.get("coverage") or {}),
                extra={k: v for k, v in feature_raw.items() if k not in _FEATURE_KEYS},
            )
        )
    return NativeFeatureManifest(db_id=str(raw.get("db_id") or ""), features=features)


def _load_mapping(value: str | Path | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    path = Path(value)
    text = path.read_text(encoding="utf-8")
    loaded = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    if not isinstance(loaded, dict):
        raise ValueError(f"expected mapping in {path}")
    return loaded
