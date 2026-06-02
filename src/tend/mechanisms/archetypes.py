"""The closed archetype catalog (proposal 04 §04-2-4).

An archetype is ``mechanism × question-shape`` pinned to a falls-out difficulty, an
``sql_infeasibility_class``, a ``shape_policy``, and the name of the reference-oracle template R
that *defines* the answer. QPS enumerates intents as ``mechanism instance × archetype × domain``
— the catalog is what makes coverage countable and query-bearing-ness structural (each archetype
is, by definition, "an operation that must hit mechanism M").

Difficulties/classes here mirror census_supply.py and 04 §04-2-4. Mechanism ids are the
SSoT/census vocabulary; :data:`MECHANISM_ALIASES` maps Session B's terms (e.g. ``optional_embed``)
onto them so intents from either session resolve.
"""
from __future__ import annotations

from dataclasses import dataclass

#: canonical mechanism ids (census / 03 §03-6-2 vocabulary)
MECHANISMS: tuple[str, ...] = (
    "polymorphic", "sparse_scalar", "sparse_embed", "dynamic_key", "versioning", "none",
)
#: mechanisms whose query-bearing recovery yields structural_schema_flex / L4
STRUCTURAL_MECHANISMS: frozenset[str] = frozenset({"polymorphic", "sparse_embed", "dynamic_key"})

#: aliases other sessions/agents may emit -> canonical id
MECHANISM_ALIASES: dict[str, str] = {
    "optional_embed": "sparse_embed",
    "sparse_optional_embed": "sparse_embed",
    "sparse": "sparse_scalar",
    "polymorphic_subtype": "polymorphic",
    "eav": "dynamic_key",
    "schema_versioning": "versioning",
    "": "none",
}


def normalize_mechanism(mechanism: str) -> str:
    """Resolve an alias/loose mechanism name to a canonical id (defaults to the input)."""
    m = (mechanism or "none").strip()
    return MECHANISM_ALIASES.get(m, m)


@dataclass(frozen=True)
class Archetype:
    """One catalog entry. ``reference_template`` keys a reference oracle in mechanisms.oracles."""

    id: str
    mechanism: str
    question_shape: str
    analytical_op: str
    reference_template: str
    difficulty: str                       # L0..L4 (falls-out)
    sql_infeasibility_class: str          # feasible|semantic|performative|structural_*
    shape_policy: str                     # preserve|reshape|reduce


def _a(mechanism: str, _id: str, shape: str, op: str, diff: str, cls: str,
       policy: str) -> Archetype:
    return Archetype(id=_id, mechanism=mechanism, question_shape=shape, analytical_op=op,
                     reference_template=_id, difficulty=diff, sql_infeasibility_class=cls,
                     shape_policy=policy)


#: mechanism -> list[Archetype]. Single source for QPS enumeration + census reachability.
ARCHETYPES: dict[str, list[Archetype]] = {
    "polymorphic": [
        _a("polymorphic", "per_subtype_agg",
           "aggregate a metric separately per discriminator subtype",
           "group by discriminator key; $switch dispatch to the subtype's own field",
           "L4", "structural_schema_flex", "reduce"),
        _a("polymorphic", "subtype_cond_projection",
           "project a value chosen by subtype, handling absent subtype fields",
           "per-doc $switch on discriminator; missing subtype field handled",
           "L4", "structural_schema_flex", "preserve"),
        _a("polymorphic", "cross_subtype_compare",
           "compare a metric across subtypes",
           "group per subtype then compare aggregates",
           "L3", "performative", "reduce"),
        _a("polymorphic", "subtype_specific_field",
           "for one subtype, return its subtype-exclusive field",
           "filter to subtype value; read its exclusive field",
           "L4", "structural_schema_flex", "reshape"),
    ],
    "sparse_embed": [
        _a("sparse_embed", "present_missing_projection",
           "attach a per-parent field that depends on a sparse optional embed (present/missing)",
           "per parent: has(embed) ? f(embed) : default; keep every parent doc",
           "L4", "structural_schema_flex", "preserve"),
        _a("sparse_embed", "has_vs_absent_compare",
           "compare parents that have the optional embed vs those that don't",
           "partition parents by embed presence; aggregate each group",
           "L4", "structural_schema_flex", "reduce"),
    ],
    "sparse_scalar": [
        _a("sparse_scalar", "existence_count",
           "count documents where a sparse field is present",
           "count docs with the field present ($exists/$type)",
           "L2", "semantic", "reduce"),
        _a("sparse_scalar", "null_coalesce_agg",
           "aggregate a sparse field coalescing missing to a default",
           "sum/avg with $ifNull default for missing",
           "L3", "semantic", "reduce"),
    ],
    "dynamic_key": [
        _a("dynamic_key", "dynamic_key_fold",
           "aggregate over a document's dynamic key/value attribute bag",
           "$objectToArray then aggregate the k/v pairs",
           "L4", "structural_schema_flex", "reduce"),
        _a("dynamic_key", "cross_keyset_value",
           "read a value across documents with heterogeneous key sets",
           "look up a key that exists in only some docs",
           "L3", "performative", "reshape"),
    ],
    "versioning": [
        _a("versioning", "cross_version_agg",
           "aggregate a field that was renamed/added across schema versions",
           "coalesce old/new field names ($ifNull chain) then aggregate",
           "L3", "semantic", "reduce"),
    ],
    "none": [
        _a("none", "simple_filter", "filter+project a single collection",
           "match a predicate, project fields", "L0", "feasible", "preserve"),
        _a("none", "topn", "top-N by a sort key",
           "sort then limit", "L1", "feasible", "reshape"),
        _a("none", "group_count", "group and count",
           "group by key, count", "L1", "feasible", "reduce"),
        _a("none", "join_nested_group", "join/unwind then group",
           "lookup or unwind embedded array, then group", "L2", "feasible", "reduce"),
        _a("none", "fk_rollup", "cross-collection rollup of child rows to each parent",
           "$lookup a child collection by foreign key, aggregate per parent (multi-collection)",
           "L3", "feasible", "reshape"),
    ],
}

#: flat id -> Archetype index
_BY_ID: dict[str, Archetype] = {a.id: a for arr in ARCHETYPES.values() for a in arr}


def archetypes_for(mechanism: str) -> list[Archetype]:
    """All archetypes reachable from a (possibly aliased) mechanism id."""
    return ARCHETYPES.get(normalize_mechanism(mechanism), [])


def get_archetype(archetype_id: str) -> Archetype:
    try:
        return _BY_ID[archetype_id]
    except KeyError as exc:
        raise KeyError(f"unknown archetype {archetype_id!r}; known={sorted(_BY_ID)}") from exc
