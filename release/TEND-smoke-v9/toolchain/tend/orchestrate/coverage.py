"""Six-axis coverage controller with min+max quotas and supply-relax."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from tend.errors import CoverageInfeasible

SIX_AXES: dict[str, Callable[[dict[str, Any]], str]] = {
    "domain": lambda r: str(r.get("domain_id", "unknown")),
    "join_depth": lambda r: "3+" if int(r.get("join_depth", 0)) >= 3 else str(int(r.get("join_depth", 0))),
    "aggregation_depth": lambda r: str(r["aggregation_depth"]),
    "schema_pattern": lambda r: str(r["schema_pattern"]),
    "schema_flex": lambda r: str(r.get("schema_flex", "none")),
    "difficulty_tier": lambda r: str(r["difficulty"]),
}

DEFAULT_MIN_QUOTA = 0
DEFAULT_MAX_QUOTA = 10_000
MARGINAL_EPSILON = 0.01


def _cell_key(axis: str, value: str) -> tuple[str, str]:
    return axis, value


@dataclass
class CoverageController:
    """Track six-axis counts and enforce min/max quota protocol."""

    min_quota: dict[tuple[str, str], int] = field(default_factory=dict)
    max_quota: dict[tuple[str, str], int] = field(default_factory=dict)
    count: Counter[tuple[str, str]] = field(default_factory=Counter)
    supply_constrained: set[tuple[str, str]] = field(default_factory=set)
    supply_ceiling: dict[tuple[str, str], float] = field(default_factory=dict)

    @classmethod
    def with_defaults(cls, *, target_records: int = 100) -> CoverageController:
        ctrl = cls()
        per_cell_max = max(1, target_records // 4)
        per_cell_min = max(0, target_records // 40)
        for axis, key_fn in SIX_AXES.items():
            ctrl.max_quota.setdefault(_cell_key(axis, "*"), per_cell_max)
            ctrl.min_quota.setdefault(_cell_key(axis, "*"), per_cell_min)
        return ctrl

    def axis_value(self, record: dict[str, Any], axis: str) -> str:
        return SIX_AXES[axis](record)

    def effective_min(self, cell: tuple[str, str]) -> int:
        target = self.min_quota.get(cell, DEFAULT_MIN_QUOTA)
        if cell in self.supply_constrained:
            ceiling = self.supply_ceiling.get(cell, 0.0)
            return min(target, int(ceiling * 10_000) if ceiling <= 1 else int(ceiling))
        return target

    def max_for(self, cell: tuple[str, str]) -> int:
        return self.max_quota.get(cell, DEFAULT_MAX_QUOTA)

    def deficit(self, cell: tuple[str, str]) -> int:
        return max(0, self.effective_min(cell) - self.count[cell])

    def record_cells(self, record: dict[str, Any]) -> list[tuple[str, str]]:
        return [_cell_key(axis, self.axis_value(record, axis)) for axis in SIX_AXES]

    def marginal_gain(self, record: dict[str, Any]) -> float:
        gain = 0.0
        for cell in self.record_cells(record):
            if self.count[cell] >= self.max_for(cell):
                return -1.0
            if self.deficit(cell) > 0:
                gain += 1.0
            elif self.count[cell] < self.max_for(cell):
                gain += MARGINAL_EPSILON
        return gain

    def should_accept(self, record: dict[str, Any], *, split: str = "train") -> tuple[bool, str]:
        cells = self.record_cells(record)
        for cell in cells:
            if self.count[cell] >= self.max_for(cell):
                return False, f"max_quota saturated for {cell}"

        deficits = [self.deficit(cell) for cell in cells]
        if any(d > 0 for d in deficits):
            return True, "strong-pull"

        gain = self.marginal_gain(record)
        if gain >= MARGINAL_EPSILON:
            return True, "marginal"
        return False, "no marginal gain"

    def accept(self, record: dict[str, Any]) -> None:
        for cell in self.record_cells(record):
            self.count[cell] += 1

    def pick_next_cell(self) -> tuple[str, str] | None:
        deficits: list[tuple[int, tuple[str, str]]] = []
        seen: set[tuple[str, str]] = set()
        for cell, target in self.min_quota.items():
            if cell in seen:
                continue
            seen.add(cell)
            deficit = self.deficit(cell)
            if deficit > 0:
                deficits.append((deficit, cell))
        if not deficits:
            return None
        deficits.sort(key=lambda item: (-item[0], item[1]))
        return deficits[0][1]

    def quota_state(self) -> dict[str, Any]:
        cells: dict[str, dict[str, dict[str, int | bool]]] = defaultdict(dict)
        all_cells = set(self.count) | set(self.min_quota) | set(self.max_quota)
        for axis, value in sorted(all_cells):
            cell = (axis, value)
            cells[axis][value] = {
                "count": self.count[cell],
                "min_quota": self.effective_min(cell),
                "max_quota": self.max_for(cell),
                "supply_constrained": cell in self.supply_constrained,
            }
        return {"cells": cells, "supply_constrained": sorted(self.supply_constrained)}

    def mark_supply_constrained(self, axis: str, value: str, ceiling: float) -> None:
        cell = _cell_key(axis, value)
        self.supply_constrained.add(cell)
        self.supply_ceiling[cell] = ceiling

    def estimate_schema_flex_ceiling(
        self, records: list[dict[str, Any]], catalog: dict[str, Any]
    ) -> float:
        selected = [e for e in catalog.get("databases", []) if e.get("selected")]
        flex_dbs = {e["db_id"] for e in selected if e.get("flex_eligible")}
        if not records:
            return 0.0
        flex_records = sum(
            1 for r in records if r.get("schema_flex", "none") != "none" or r["db_id"] in flex_dbs
        )
        return flex_records / len(records)

    def apply_supply_relax_from_catalog(self, catalog: dict[str, Any]) -> dict[str, Any]:
        selected = [e for e in catalog.get("databases", []) if e.get("selected")]
        ratio = sum(1 for e in selected if e.get("flex_eligible")) / max(len(selected), 1)
        active = ratio < 0.30
        return {
            "flex_eligible_db_ratio": ratio,
            "supply_relax_active": active,
            "min_flex_db_ratio": 0.30,
        }

    def ensure_feasible(self, records: list[dict[str, Any]]) -> None:
        if not records:
            raise CoverageInfeasible("no records available for coverage planning")
