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


@dataclass(frozen=True)
class SolverBoundary:
    allow_list: dict[str, Any]
    logger: RunLogger | None = None

    @classmethod
    def from_settings(cls, settings: Settings, logger: RunLogger | None = None) -> "SolverBoundary":
        return cls(load_solver_allow_list(settings.paths.schemas), logger=logger)

    def sanitize_test_record(self, record: dict[str, Any]) -> dict[str, Any]:
        forbidden = set(self.allow_list.get("test_record_forbidden_fields", []))
        safe, removed = _redact_forbidden_fields(record, forbidden=forbidden)
        if removed and self.logger:
            self.logger.info("baseline_forbidden_fields_redacted", fields=removed)
        return safe

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
    "SolverBoundary",
    "_redact_forbidden_fields",
    "check_disjointness",
    "load_solver_allow_list",
]
