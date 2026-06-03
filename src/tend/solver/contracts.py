"""Typed SMART solver contracts from proposals/06_solution_design.md.

The solver crosses four explicit representation boundaries:

1. ``ShapeModel`` from schema-only shape comprehension.
2. ``LogicalSpec`` from intent formalization.
3. ``PhysicalPlan`` from NoSQL heterogeneity planning.
4. ``SolverPrediction`` from query realization and self-debug.

These classes stay deliberately small and JSON-serializable. LLM agents can return plain
dicts, deterministic code can normalize them into these contracts, and every boundary can
be logged without bespoke encoders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

ShapePolicy = Literal["preserve", "reshape", "reduce"]


@dataclass(frozen=True)
class ShapeVariant:
    id: str
    discriminator: dict[str, Any] = field(default_factory=dict)
    coverage: float | None = None
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FieldLocus:
    variant: str
    path: str
    type: str = "unknown"
    presence: str = "always"


@dataclass(frozen=True)
class CollectionShape:
    variants: list[ShapeVariant] = field(default_factory=list)
    field_locus: dict[str, list[FieldLocus]] = field(default_factory=dict)
    doc_count: int | None = None
    dynamic_key_paths: list[str] = field(default_factory=list)
    dynamic_key_samples: dict[str, list[str]] = field(default_factory=dict)
    array_paths: list[str] = field(default_factory=list)
    dynamic_array_object_paths: list[str] = field(default_factory=list)
    array_object_dynamic_paths: list[str] = field(default_factory=list)
    presence_state_counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ShapeModel:
    collections: dict[str, CollectionShape] = field(default_factory=dict)
    coverage_gaps: list[str] = field(default_factory=list)
    shape_flex_signature: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "ShapeModel":
        collections: dict[str, CollectionShape] = {}
        for name, raw in payload.get("collections", {}).items():
            variants = [
                ShapeVariant(
                    id=str(v.get("id", f"v{i}")),
                    discriminator=dict(v.get("discriminator") or {}),
                    coverage=v.get("coverage"),
                    fields=dict(v.get("fields") or {}),
                )
                for i, v in enumerate(raw.get("variants", []))
            ]
            loci: dict[str, list[FieldLocus]] = {}
            for logical, entries in raw.get("field_locus", {}).items():
                loci[logical] = [
                    FieldLocus(
                        variant=str(e.get("variant", "*")),
                        path=str(e.get("path", logical)),
                        type=str(e.get("type", "unknown")),
                        presence=str(e.get("presence", "always")),
                    )
                    for e in entries
                ]
            collections[name] = CollectionShape(
                variants=variants,
                field_locus=loci,
                doc_count=raw.get("doc_count"),
                dynamic_key_paths=[str(path) for path in raw.get("dynamic_key_paths", [])],
                dynamic_key_samples={
                    str(path): [str(sample) for sample in samples]
                    for path, samples in dict(raw.get("dynamic_key_samples") or {}).items()
                },
                array_paths=[str(path) for path in raw.get("array_paths", [])],
                dynamic_array_object_paths=[
                    str(path) for path in raw.get("dynamic_array_object_paths", [])
                ],
                array_object_dynamic_paths=[
                    str(path) for path in raw.get("array_object_dynamic_paths", [])
                ],
                presence_state_counts={
                    str(state): int(count)
                    for state, count in dict(raw.get("presence_state_counts") or {}).items()
                },
            )
        return cls(
            collections=collections,
            coverage_gaps=list(payload.get("coverage_gaps", [])),
            shape_flex_signature=list(payload.get("shape_flex_signature", [])),
        )


@dataclass(frozen=True)
class LogicalSpec:
    entity: str
    per: str
    compute: list[dict[str, Any]] = field(default_factory=list)
    aggregate: list[dict[str, Any]] = field(default_factory=list)
    filter: list[dict[str, Any]] = field(default_factory=list)
    output: dict[str, Any] = field(default_factory=dict)
    shape_policy: ShapePolicy = "reshape"
    target_fields: list[str] = field(default_factory=list)
    clause_coverage: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "LogicalSpec":
        policy = payload.get("shape_policy", "reshape")
        if policy not in ("preserve", "reshape", "reduce"):
            policy = "reshape"
        target_fields = payload.get("target_fields")
        if target_fields is None:
            target_fields = payload.get("output", {}).get("target_fields", [])
        return cls(
            entity=str(payload.get("entity") or payload.get("per") or ""),
            per=str(payload.get("per") or payload.get("entity") or ""),
            compute=list(payload.get("compute", [])),
            aggregate=list(payload.get("aggregate", [])),
            filter=list(payload.get("filter", [])),
            output=dict(payload.get("output", {})),
            shape_policy=policy,
            target_fields=list(target_fields or []),
            clause_coverage=list(payload.get("clause_coverage", [])),
        )


@dataclass(frozen=True)
class PlannedStage:
    op: str
    note: str
    stage: dict[str, Any]

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PhysicalPlan:
    collection: str
    stages: list[PlannedStage]
    variant_handling: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "PhysicalPlan":
        stages: list[PlannedStage] = []
        for raw in payload.get("stages", []):
            stage = raw.get("stage")
            if stage is None:
                stage = {str(raw.get("op", "")): raw.get("spec", {})}
            op = str(raw.get("op") or next(iter(stage), ""))
            if op.startswith("$") and isinstance(stage, dict) and op not in stage:
                stage = {op: stage}
            stages.append(PlannedStage(op=op, note=str(raw.get("note", "")), stage=stage))
        return cls(
            collection=str(payload.get("collection", "")),
            stages=stages,
            variant_handling=list(payload.get("variant_handling", [])),
        )


@dataclass(frozen=True)
class SolverDisclosure:
    s_solver: list[str]
    backbone: str
    r_max: int
    witness_k: int
    no_training: bool = True
    uses_train_json: bool = False
    disjointness_ok: bool = True
    disjointness_detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SolverPrediction:
    record_id: int | None
    db_id: str
    MQL: str
    disclosure: SolverDisclosure
    shape_model: dict[str, Any]
    logical_spec: dict[str, Any]
    physical_plan: dict[str, Any]
    feedback: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)
