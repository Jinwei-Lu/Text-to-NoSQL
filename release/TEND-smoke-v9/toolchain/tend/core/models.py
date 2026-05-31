from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CanonicalFormSet:
    must_contain: tuple[str, ...]
    must_not_contain: tuple[str, ...]
    must_contain_at_root: tuple[str, ...]
    must_not_contain_at_root: tuple[str, ...]
    known_variants: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanonicalFormSet":
        return cls(
            must_contain=tuple(data.get("must_contain", [])),
            must_not_contain=tuple(data.get("must_not_contain", [])),
            must_contain_at_root=tuple(data.get("must_contain_at_root", [])),
            must_not_contain_at_root=tuple(data.get("must_not_contain_at_root", [])),
            known_variants=tuple(data.get("known_variants", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "must_contain": list(self.must_contain),
            "must_not_contain": list(self.must_not_contain),
            "must_contain_at_root": list(self.must_contain_at_root),
            "must_not_contain_at_root": list(self.must_not_contain_at_root),
            "known_variants": list(self.known_variants),
        }


@dataclass(frozen=True)
class Record:
    record_id: int
    db_id: str
    nl_queries: tuple[str, ...]
    mql: str
    canonical_form_set: CanonicalFormSet
    operator_family: str | None = None
    nosql_nativeness_level: str | None = None
    empirical_difficulty: str | None = None
    shape_policy: str | None = None
    world_signature: str | None = None
    tds_cell: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Record":
        nl_raw = data["nl_queries"]
        if isinstance(nl_raw, dict):
            nl_queries = tuple(
                nl_raw[key] for key in ("canonical", "colloquial") if key in nl_raw
            ) or tuple(nl_raw.values())
        else:
            nl_queries = tuple(nl_raw)
        return cls(
            record_id=int(data["record_id"]),
            db_id=data["db_id"],
            nl_queries=nl_queries,
            mql=data["MQL"],
            canonical_form_set=CanonicalFormSet.from_dict(data["canonical_form_set"]),
            operator_family=data.get("operator_family"),
            nosql_nativeness_level=data.get("nosql_nativeness_level"),
            empirical_difficulty=data.get("empirical_difficulty"),
            shape_policy=data.get("shape_policy"),
            world_signature=data.get("world_signature"),
            tds_cell=data.get("tds_cell"),
            raw=dict(data),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = dict(self.raw) if self.raw else {}
        payload.update(
            {
                "record_id": self.record_id,
                "db_id": self.db_id,
                "nl_queries": list(self.nl_queries),
                "MQL": self.mql,
                "canonical_form_set": self.canonical_form_set.to_dict(),
            }
        )
        if self.operator_family:
            payload["operator_family"] = self.operator_family
        if self.nosql_nativeness_level:
            payload["nosql_nativeness_level"] = self.nosql_nativeness_level
        if self.empirical_difficulty:
            payload["empirical_difficulty"] = self.empirical_difficulty
        if self.shape_policy:
            payload["shape_policy"] = self.shape_policy
        if self.world_signature:
            payload["world_signature"] = self.world_signature
        if self.tds_cell:
            payload["tds_cell"] = self.tds_cell
        return payload

    def solver_view(
        self,
        schema: dict[str, Any],
        witness: dict[str, Any],
        phenomena_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "db_id": self.db_id,
            "nl": self.nl_queries[0],
            "schema": schema,
            "witness": witness,
            "phenomena_meta": phenomena_meta or {},
        }


@dataclass(frozen=True)
class DomainTemplate:
    domain_id: str
    name: str
    entities: tuple[dict[str, Any], ...]
    relations: tuple[dict[str, Any], ...] = ()
    f_topology_hints: dict[str, Any] = field(default_factory=dict)
    distribution_priors: dict[str, Any] = field(default_factory=dict)
    phenomenon_blueprints: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DomainTemplate":
        return cls(
            domain_id=data["domain_id"],
            name=data.get("name", data["domain_id"]),
            entities=tuple(data.get("entities", [])),
            relations=tuple(data.get("relations", [])),
            f_topology_hints=dict(data.get("f_topology_hints", {})),
            distribution_priors=dict(data.get("distribution_priors", {})),
            phenomenon_blueprints=tuple(data.get("phenomenon_blueprints", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SchemaSpec:
    db_id: str
    domain_id: str
    collections: dict[str, Any]

    def to_publish_dict(self) -> dict[str, Any]:
        return self.collections


@dataclass(frozen=True)
class WitnessWorld:
    db_id: str
    data: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class PhenomenonEvidence:
    collection: str
    path: str
    document_ids: tuple[str, ...]
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PhenomenonRecord:
    phenomenon_id: str
    phenomenon_class: str
    witness_evidence: PhenomenonEvidence
    detector_signature: str
    intent_hooks: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "phenomenon_id": self.phenomenon_id,
            "phenomenon_class": self.phenomenon_class,
            "witness_evidence": {
                "collection": self.witness_evidence.collection,
                "path": self.witness_evidence.path,
                "document_ids": list(self.witness_evidence.document_ids),
                "summary": self.witness_evidence.summary,
            },
            "detector_signature": self.detector_signature,
            "intent_hooks": list(self.intent_hooks),
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class StructuredIntent:
    meta: dict[str, Any]
    intent: dict[str, Any]
    output: dict[str, Any]
    properties: dict[str, Any]
    noise_policies: dict[str, Any]
    nosql_nativeness: dict[str, Any]
    canonical_form_set: CanonicalFormSet

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta,
            "intent": self.intent,
            "output": self.output,
            "properties": self.properties,
            "noise_policies": self.noise_policies,
            "nosql_nativeness": self.nosql_nativeness,
            "canonical_form_set": self.canonical_form_set.to_dict(),
        }


@dataclass(frozen=True)
class QIR:
    pattern_family: str
    primary_operator: str
    input_shape: dict[str, Any]
    output_shape: dict[str, Any]
    referenced_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Certificate:
    record_id: str
    db_id: str
    si_hash: str
    world_signature: str
    split: str
    empirical_difficulty: str
    phase_d: dict[str, Any]
    routing: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConstructedRecord:
    record: Record
    structured_intent: StructuredIntent
    qir: QIR
    certificate: Certificate
    checker: dict[str, Any]
    mutations: list[dict[str, Any]]


@dataclass(frozen=True)
class EvaluationRow:
    record_id: int
    db_id: str
    prediction: str
    em: int
    qsm: int
    qfc: int
    ex: int
    efm: int
    evm: int
    qim: int
    ast_result: str
    forbidden_op_hit: bool
    exec_error: str | None = None

    def fingerprint(self) -> tuple[int, int, int, int, int, int, int]:
        return (self.em, self.qsm, self.qfc, self.ex, self.efm, self.evm, self.qim)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["fingerprint"] = list(self.fingerprint())
        return data
