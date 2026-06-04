"""Boundary helpers for constrained non-agent baselines."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import GateError
from ..observability import RunLogger

_REQUIRED_FROZEN_PANELS = ("small", "medium", "large", "frontier")
_CONSTRUCTION_ROLE_LABELS = frozenset({"qps", "ms", "mut", "pv", "nlp", "rtv", "nnc", "ra"})
PUBLIC_SCHEMA_VERSION = "baseline_public_schema_v1"
_PUBLIC_COLLECTION_KEYS = frozenset(
    {
        "document_count",
        "doc_count",
        "root_entity",
        "native_shape",
        "schema_flex",
        "fields",
        "embeds",
        "foreign_keys",
        "fks",
        "dynamic_key_paths",
        "array_paths",
        "dynamic_array_object_paths",
        "array_object_dynamic_paths",
        "nested_array_paths",
        "variants",
        "__variants",
    }
)
_PRIVATE_SCHEMA_KEYS = frozenset(
    {
        "structure_audit",
        "structure_gate",
        "source_tables",
        "_provenance",
        "provenance",
        "provenance_refs",
        "migration_recipe_ref",
        "native_verification",
        "canonical_form_set",
        "mql",
        "world_signature",
        "compiler",
        "native_metadata",
        "sample_keys",
        "dynamic_key_samples",
        "array_lengths",
        "presence_state_counts",
        "collection_counts",
        "coverage",
        "source_signal",
    }
)


@dataclass(frozen=True, slots=True)
class SanitizedPayload:
    value: dict[str, Any]
    stripped_fields: list[str]


@dataclass(frozen=True)
class SolverBoundary:
    allow_list: dict[str, Any]
    logger: RunLogger | None = None

    @classmethod
    def from_settings(cls, settings: Settings, logger: RunLogger | None = None) -> "SolverBoundary":
        return cls(load_solver_allow_list(settings.paths.schemas), logger=logger)

    def sanitize_test_record(self, record: dict[str, Any]) -> dict[str, Any]:
        sanitized = sanitize_public_record(record)
        if sanitized.stripped_fields and self.logger:
            self.logger.info(
                "baseline_forbidden_fields_redacted",
                fields=sanitized.stripped_fields,
            )
        return sanitized.value

    def assert_stage_can_use_tool(self, stage: str, tool: str) -> None:
        spec = self.allow_list.get("tools", {}).get(tool)
        if not spec:
            raise GateError("unknown baseline tool", context={"tool": tool})
        allowed = set(spec.get("callable_by_stages", []))
        if stage not in allowed:
            raise GateError(
                "baseline tool called from forbidden stage",
                context={"stage": stage, "tool": tool, "allowed": sorted(allowed)},
            )


def load_solver_allow_list(schema_dir: Path) -> dict[str, Any]:
    path = schema_dir / "solver_allow_list.json"
    return json.loads(path.read_text(encoding="utf-8"))


def sanitize_public_record(record: dict[str, Any]) -> SanitizedPayload:
    """Allow-list the public record fields visible to non-EG baselines."""
    safe: dict[str, Any] = {}
    stripped: list[str] = []
    if "db_id" in record:
        safe["db_id"] = record["db_id"]
    if "record_id" in record:
        safe["record_id"] = record["record_id"]

    nl_queries = record.get("nl_queries")
    canonical = None
    if isinstance(nl_queries, dict):
        raw_canonical = nl_queries.get("canonical")
        if isinstance(raw_canonical, str) and raw_canonical.strip():
            canonical = raw_canonical
        for key in nl_queries:
            if key != "canonical":
                stripped.append(f"nl_queries.{key}")
    if canonical is None:
        for key in ("NLQ", "query"):
            candidate = record.get(key)
            if isinstance(candidate, str) and candidate.strip():
                canonical = candidate
                break
    if canonical is not None:
        safe["nl_queries"] = {"canonical": canonical}

    for key in record:
        if key in {"db_id", "record_id", "nl_queries"}:
            continue
        stripped.append(str(key))
    return SanitizedPayload(value=safe, stripped_fields=_dedupe(stripped))


def sanitize_public_schema(schema: dict[str, Any]) -> SanitizedPayload:
    """Normalize and strip schema metadata before it reaches baseline prompts."""
    stripped: list[str] = []
    if not isinstance(schema, dict):
        return SanitizedPayload(
            value={"public_schema_version": PUBLIC_SCHEMA_VERSION, "collections": {}},
            stripped_fields=[],
        )

    public: dict[str, Any] = {"public_schema_version": PUBLIC_SCHEMA_VERSION}
    if "db_id" in schema:
        public["db_id"] = _sanitize_schema_value(schema["db_id"], "db_id", stripped)

    raw_collections = schema.get("collections")
    if isinstance(raw_collections, dict):
        public["collections"] = _sanitize_collections(
            raw_collections,
            stripped,
            path_prefix="collections",
        )
        for key in schema:
            if key in {"db_id", "collections"}:
                continue
            if _is_private_schema_key(key):
                stripped.append(str(key))
    else:
        public["collections"] = _sanitize_collections(
            {
                key: value
                for key, value in schema.items()
                if isinstance(value, dict) and not _is_private_schema_key(key)
            },
            stripped,
            path_prefix="",
        )
        for key, value in schema.items():
            if isinstance(value, dict) and not _is_private_schema_key(key):
                continue
            if key == "db_id":
                continue
            stripped.append(str(key))

    return SanitizedPayload(value=public, stripped_fields=_dedupe(stripped))


def public_schema_shape(schema: dict[str, Any]) -> dict[str, Any]:
    collections = schema.get("collections") if isinstance(schema, dict) else None
    if not isinstance(collections, dict):
        return {"format": "unknown", "collection_total": 0, "collections": []}
    names = sorted(str(name) for name in collections)
    return {
        "format": "collections",
        "collection_total": len(names),
        "collections": names,
    }


def check_disjointness(
    s_solver: list[str],
    allow_list: dict[str, Any],
    *,
    require_manifests: bool = True,
) -> dict[str, Any]:
    normalized = {_norm_model(m) for m in s_solver if _norm_model(m)}
    disjointness = allow_list.get("four_party_disjointness", {})
    construction = _model_id_set(disjointness.get("construction_model_ids"))
    frozen: set[str] = set()
    manifest_errors: list[str] = []

    if require_manifests:
        if not construction:
            manifest_errors.append("four_party_disjointness.construction_model_ids missing/empty")
        role_labels = sorted(construction & _CONSTRUCTION_ROLE_LABELS)
        if role_labels:
            manifest_errors.append(
                "four_party_disjointness.construction_model_ids contains role labels "
                f"instead of model IDs: {role_labels}"
            )

    frozen_panels = allow_list.get("frozen_panels")
    if not isinstance(frozen_panels, dict):
        if require_manifests:
            manifest_errors.append("frozen_panels missing/invalid")
        frozen_panels = {}

    for panel_name in _REQUIRED_FROZEN_PANELS:
        panel_models = _model_id_set(frozen_panels.get(panel_name))
        if require_manifests and not panel_models:
            manifest_errors.append(f"frozen_panels.{panel_name} missing/empty")
        frozen.update(panel_models)

    construction_hits = sorted(normalized & construction)
    frozen_hits = sorted(normalized & frozen)
    return {
        "ok": not manifest_errors and not construction_hits and not frozen_hits,
        "construction_pool_hits": construction_hits,
        "frozen_panel_hits": frozen_hits,
        "manifest_errors": manifest_errors,
        "checked_models": sorted(s_solver),
        "required_manifests": require_manifests,
    }


def _sanitize_collections(
    raw_collections: dict[str, Any],
    stripped: list[str],
    *,
    path_prefix: str,
) -> dict[str, Any]:
    collections: dict[str, Any] = {}
    for name, raw_collection in sorted(raw_collections.items()):
        key = str(name)
        path = f"{path_prefix}.{key}" if path_prefix else key
        if _is_private_schema_key(key):
            stripped.append(path)
            continue
        collections[key] = _sanitize_collection(raw_collection, path, stripped)
    return collections


def _sanitize_collection(value: Any, path: str, stripped: list[str]) -> Any:
    if not isinstance(value, dict):
        return _sanitize_schema_value(value, path, stripped)
    if _looks_like_public_collection_summary(value):
        return _sanitize_schema_value(value, path, stripped)

    fields: dict[str, Any] = {}
    collection: dict[str, Any] = {}
    for key, child in value.items():
        key_text = str(key)
        child_path = f"{path}.{key_text}"
        if key_text == "__variants":
            collection["variants"] = _sanitize_variants(child, child_path, stripped)
            continue
        if _is_private_schema_key(key_text):
            stripped.append(child_path)
            continue
        if key_text in _PUBLIC_COLLECTION_KEYS:
            out_key = "variants" if key_text == "__variants" else key_text
            collection[out_key] = (
                _sanitize_variants(child, child_path, stripped)
                if out_key == "variants"
                else _sanitize_schema_value(child, child_path, stripped)
            )
            continue
        fields[key_text] = _sanitize_schema_value(child, child_path, stripped)
    if fields:
        collection["fields"] = fields
    return collection


def _looks_like_public_collection_summary(value: dict[str, Any]) -> bool:
    return any(key in value for key in _PUBLIC_COLLECTION_KEYS if key != "__variants")


def _sanitize_schema_value(value: Any, path: str, stripped: list[str]) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text == "__variants":
                clean["variants"] = _sanitize_variants(child, child_path, stripped)
                continue
            if _is_private_schema_key(key_text):
                stripped.append(child_path)
                continue
            out_key = "variants" if key_text == "__variants" else key_text
            clean[out_key] = (
                _sanitize_variants(child, child_path, stripped)
                if out_key == "variants"
                else _sanitize_schema_value(child, child_path, stripped)
            )
        return clean
    if isinstance(value, list):
        return [
            _sanitize_schema_value(child, f"{path}[{index}]", stripped)
            for index, child in enumerate(value)
        ]
    return value


def _sanitize_variants(value: Any, path: str, stripped: list[str]) -> Any:
    if not isinstance(value, list):
        return _sanitize_schema_value(value, path, stripped)
    variants: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, dict):
            variants.append({"value": _sanitize_schema_value(item, item_path, stripped)})
            continue
        clean: dict[str, Any] = {}
        for key in ("discriminator", "fields"):
            if key in item:
                clean[key] = _sanitize_schema_value(item[key], f"{item_path}.{key}", stripped)
        for key in item:
            if key not in {"discriminator", "fields"}:
                stripped.append(f"{item_path}.{key}")
        variants.append(clean)
    return variants


def _is_private_schema_key(key: Any) -> bool:
    text = str(key)
    lower = text.lower()
    return (
        lower in _PRIVATE_SCHEMA_KEYS
        or text == "MQL"
        or lower.startswith("surgical_")
        or lower.startswith("anti_sql_transfer")
        or lower.endswith("_ref")
        or lower.endswith("_refs")
        or ("mql" in lower and ("signature" in lower or "skeleton" in lower))
    )


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _redact_forbidden_fields(
    value: Any,
    *,
    forbidden: set[str],
    path: str = "",
) -> tuple[Any, list[str]]:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        removed: list[str] = []
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in forbidden or key_text.endswith("_ref"):
                removed.append(child_path)
                continue
            redacted, child_removed = _redact_forbidden_fields(
                child,
                forbidden=forbidden,
                path=child_path,
            )
            clean[key] = redacted
            removed.extend(child_removed)
        return clean, removed
    if isinstance(value, list):
        items: list[Any] = []
        removed: list[str] = []
        for index, child in enumerate(value):
            redacted, child_removed = _redact_forbidden_fields(
                child,
                forbidden=forbidden,
                path=f"{path}[{index}]",
            )
            items.append(redacted)
            removed.extend(child_removed)
        return items, removed
    return value, []


def _norm_model(model: str) -> str:
    return str(model).strip().lower()


def _model_id_set(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()
    ids: set[str] = set()
    for item in raw:
        normalized = _norm_model(str(item or ""))
        if normalized:
            ids.add(normalized)
    return ids


__all__ = [
    "PUBLIC_SCHEMA_VERSION",
    "SanitizedPayload",
    "SolverBoundary",
    "_redact_forbidden_fields",
    "check_disjointness",
    "load_solver_allow_list",
    "public_schema_shape",
    "sanitize_public_record",
    "sanitize_public_schema",
]
