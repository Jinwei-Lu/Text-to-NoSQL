"""Solver-side boundary guards for the SMART reference solver.

The proposal makes the safety boundary explicit: the solver may read released schema,
released NLQ/db identifiers, and local execution data in the right phases, but it must not
consume gold answers, audit traces, rejected assets, or train records. This module keeps
those rules close to the code that loads solver inputs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import Anomaly, DisabledOperatorError, GateError
from ..execution.ast_check import DISABLED_OPERATORS, DISABLED_SYSTEM_VARS, parse_pipeline
from ..observability import RunLogger
from .contracts import SolverDisclosure

_EN_PRESERVE = (
    "attach",
    "augment",
    "add field",
    "preserve structure",
    "in place",
    "decorate",
    "annotate",
    "keep every",
)
_ZH_PRESERVE = ("附加", "增补", "标注", "就地计算", "保持原结构", "保留每个", "不改变文档数")
_REDUCE = ("sum", "count", "average", "avg", "top", "group by", "total", "聚合", "总数", "平均")
_RESHAPE = ("flatten", "list all", "unwind", "展开", "透视", "重塑")
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
        """Return a solver-visible record with every forbidden gold/audit field removed."""
        forbidden = set(self.allow_list.get("test_record_forbidden_fields", []))
        safe, removed = _redact_forbidden_fields(record, forbidden=forbidden)
        if removed and self.logger:
            self.logger.info("solver_forbidden_fields_redacted", fields=removed)
        return safe

    def assert_stage_can_use_tool(self, stage: str, tool: str) -> None:
        spec = self.allow_list.get("tools", {}).get(tool)
        if not spec:
            raise GateError("unknown solver tool", context={"tool": tool})
        allowed = set(spec.get("callable_by_stages", []))
        if stage not in allowed:
            raise GateError(
                "solver tool called from forbidden stage",
                context={"stage": stage, "tool": tool, "allowed": sorted(allowed)},
            )

    def assert_no_disabled(self, mql: str, *, stage_index: int | None = None) -> None:
        hits = disabled_hits_in_mql(mql)
        if hits:
            err = DisabledOperatorError(
                "pipeline uses banned operators",
                context={"hits": hits, "stage_index": stage_index},
            )
            if self.logger:
                self.logger.anomaly(err, kind=Anomaly.DISABLED_OPERATOR)
            raise err

    def disclosure(self, settings: Settings, *, r_max: int, witness_k: int) -> SolverDisclosure:
        return build_disclosure(settings, self.allow_list, r_max=r_max, witness_k=witness_k)


def load_solver_allow_list(schema_dir: Path) -> dict[str, Any]:
    path = schema_dir / "solver_allow_list.json"
    return json.loads(path.read_text(encoding="utf-8"))


def build_disclosure(
    settings: Settings,
    allow_list: dict[str, Any],
    *,
    r_max: int,
    witness_k: int,
) -> SolverDisclosure:
    backbone = settings.llm.model
    s_solver = _dedupe_model_ids([backbone, *settings.llm.agent_models.values()])
    detail = check_disjointness(s_solver, allow_list, require_manifests=not settings.stub)
    return SolverDisclosure(
        s_solver=s_solver,
        backbone=backbone,
        r_max=r_max,
        witness_k=witness_k,
        disjointness_ok=detail["ok"],
        disjointness_detail=detail,
    )


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


def infer_shape_policy(nlq: str) -> str:
    low = nlq.lower()
    if any(k in low for k in _EN_PRESERVE) or any(k in nlq for k in _ZH_PRESERVE):
        return "preserve"
    if any(k in low for k in _RESHAPE) or any(k in nlq for k in _RESHAPE):
        return "reshape"
    if any(k in low for k in _REDUCE) or any(k in nlq for k in _REDUCE):
        return "reduce"
    return "reshape"


def extract_target_fields(nlq: str) -> list[str]:
    """Conservative target-field extraction for preserve prompts.

    It handles the proposal's canonical ``attach a field name: ...`` form and common
    snake_case field names. The LLM may still provide richer target metadata; this only
    supplies a deterministic fallback for contracts and tests.
    """
    candidates: list[str] = []
    patterns = [
        r"(?:attach|add field|annotate|decorate)\s+(?:a\s+field\s+)?([A-Za-z_][A-Za-z0-9_]*)",
        r"字段\s*([A-Za-z_][A-Za-z0-9_]*)",
        r"([A-Za-z_][A-Za-z0-9_]*)\s*:",
    ]
    for pat in patterns:
        for match in re.finditer(pat, nlq, flags=re.IGNORECASE):
            field = match.group(1)
            if "_" in field and field not in candidates:
                candidates.append(field)
    return candidates


def disabled_hits_in_mql(mql: str) -> list[str]:
    try:
        _, pipeline = parse_pipeline(mql)
    except Exception:
        # Parse failures are handled by the caller's LLM/schema path; this guard only reports
        # disabled tokens it can identify reliably.
        pipeline = []
    hits = disabled_hits_in_node(pipeline)
    for var in DISABLED_SYSTEM_VARS:
        if var in mql:
            hits.append(var)
    return sorted(set(hits))


def disabled_hits_in_node(node: Any) -> list[str]:
    hits: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in DISABLED_OPERATORS:
                    hits.append(key)
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and value in DISABLED_SYSTEM_VARS:
            hits.append(value)

    walk(node)
    return sorted(set(hits))


def render_mql(collection: str, stages: list[dict[str, Any]]) -> str:
    payload = json.dumps(stages, ensure_ascii=False)
    return f"db.{collection}.aggregate({payload})"


def _norm_model(model: str) -> str:
    return str(model).strip().lower()


def _model_id_set(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()
    ids: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            value = item.get("model_id") or item.get("id")
        else:
            value = item
        normalized = _norm_model(str(value or ""))
        if normalized:
            ids.add(normalized)
    return ids


def _dedupe_model_ids(models: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for model in models:
        normalized = _norm_model(model)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(str(model).strip())
    return out


def _redact_forbidden_fields(value: Any, *, forbidden: set[str], path: str = "") -> tuple[Any, list[str]]:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        removed: list[str] = []
        for key, child in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            if key_text in forbidden or key_text.endswith("_ref"):
                removed.append(child_path)
                continue
            clean_child, child_removed = _redact_forbidden_fields(
                child,
                forbidden=forbidden,
                path=child_path,
            )
            clean[key] = clean_child
            removed.extend(child_removed)
        return clean, sorted(removed)
    if isinstance(value, list):
        clean_items: list[Any] = []
        removed: list[str] = []
        for index, child in enumerate(value):
            clean_child, child_removed = _redact_forbidden_fields(
                child,
                forbidden=forbidden,
                path=f"{path}[{index}]",
            )
            clean_items.append(clean_child)
            removed.extend(child_removed)
        return clean_items, sorted(removed)
    return value, []
