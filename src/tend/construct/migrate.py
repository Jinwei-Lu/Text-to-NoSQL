"""DM's deterministic document-aggregate migration.

The plan is derived from *real* BIRD FK structure, not from free-form LLM output, so the
migration is reproducible and faithful (this is why DM is deterministic in the methodology):

  * a child table is a **satellite** of its parent when ``rows(child) <= rows(parent)`` —
    it embeds into the parent (object if <=1 child per parent, else array). Sparse
    satellites (e.g. loan covering 682/4500 accounts) become *optional* embeds: parents
    with no child simply lack the key, which is the present/missing heterogeneity solvers
    must reconcile.
  * larger fact tables (e.g. trans, 1M rows) are **referenced** as their own collection,
    deterministically down-sampled.
  * SQLite NULL -> missing key (not JSON null), preserving empty-vs-missing.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..source import BirdSource, DbSchema

DEFAULT_REF_SAMPLE_CAP = 40_000      # max rows kept per referenced fact table
DEFAULT_ARRAY_PROJ_CAP = 30          # max child rows projected into a parent's nested array
MigrationEventHook = Callable[..., None]


@dataclass
class EmbedEdge:
    parent: str
    child: str
    fk_col: str                      # child column referencing parent pk
    parent_pk: str
    as_array: bool                   # True if a parent can have >1 child


@dataclass
class ArrayProjEdge:
    """A 1:many *referenced* leaf child also projected into its parent as a capped nested
    array field (the child collection is kept). This materializes genuine array
    heterogeneity (e.g. account.trans[]) that single-object embeds never produce, unlocking
    nested/unwind archetypes — the same logical rows in two shapes, which is the schema-less
    challenge TEND targets."""
    parent: str
    child: str                       # also the embedded array field name on the parent
    fk_col: str                      # child column referencing the parent pk
    parent_pk: str
    cap: int                         # max children projected per parent


@dataclass
class MigrationPlan:
    db_id: str
    roots: list[str]
    embeds: dict[str, list[EmbedEdge]] = field(default_factory=dict)   # parent -> edges
    references: list[str] = field(default_factory=list)
    sample_caps: dict[str, int] = field(default_factory=dict)
    array_projections: dict[str, list[ArrayProjEdge]] = field(default_factory=dict)  # parent -> edges

    def embedded_tables(self) -> set[str]:
        return {e.child for edges in self.embeds.values() for e in edges}


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _stable_sql_value(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (2, float(value))
    if isinstance(value, bytes):
        return (3, value.hex())
    return (4, str(value))


def _stable_row_key(row: tuple) -> tuple[tuple[int, Any], ...]:
    return tuple(_stable_sql_value(value) for value in row)


def _emit_migration_event(
    event_hook: MigrationEventHook | None,
    event: str,
    **fields: Any,
) -> None:
    if event_hook is not None:
        event_hook(event, **fields)


def build_plan(
    source: BirdSource,
    db_id: str,
    *,
    ref_sample_cap: int | None = DEFAULT_REF_SAMPLE_CAP,
    array_proj_cap: int | None = DEFAULT_ARRAY_PROJ_CAP,
) -> MigrationPlan:
    """Derive embed/reference structure from FK cardinalities."""
    sch: DbSchema = source.schema(db_id)
    rows = {t: source.row_count(db_id, t) for t in sch.tables}

    # candidate satellite edges: child no larger than parent (it depends on the parent)
    candidates: list[EmbedEdge] = []
    for fk in sch.foreign_keys:
        parent, child = fk.parent_table, fk.child_table
        if parent == child:
            continue                                   # self-ref -> reference, not embed
        if rows.get(child, 0) <= rows.get(parent, 0) and rows.get(parent, 0) > 0:
            try:
                max_per = _max_children_per_parent(source, db_id, child, fk.child_col)
            except Exception:  # noqa: BLE001 - probing failure -> conservative array embed
                max_per = 2
            candidates.append(EmbedEdge(parent, child, fk.child_col,
                                        fk.parent_col,
                                        as_array=max_per > 1))

    # only embed one level: keep an edge iff its parent is not itself an embedded satellite
    # (otherwise the deeper child would be lost). Deeper children fall back to references.
    satellites = {e.child for e in candidates}
    embeds: dict[str, list[EmbedEdge]] = {}
    embedded: set[str] = set()
    for e in candidates:
        if e.parent in satellites:
            continue                                   # parent is embedded -> child -> reference
        embeds.setdefault(e.parent, []).append(e)
        embedded.add(e.child)

    references = [t for t in sch.tables if t not in embedded]
    sample_caps = (
        {
            t: ref_sample_cap
            for t in references
            if ref_sample_cap is not None and rows.get(t, 0) > ref_sample_cap
        }
        if ref_sample_cap is not None
        else {}
    )
    roots = references                                  # every non-embedded table is a root

    # Array projections: a referenced 1:many *leaf* child is ALSO projected into its root
    # parent as a capped nested array (the child collection is kept). This is the only way
    # genuine array heterogeneity arises here — single-object embeds never make arrays — so
    # it is what unlocks nested/unwind archetypes (e.g. account.trans[]).
    array_projections: dict[str, list[ArrayProjEdge]] = {}
    if array_proj_cap and array_proj_cap > 0:
        embed_parents = set(embeds)                     # tables that themselves embed -> not leaves
        seen: set[tuple[str, str]] = set()
        for fk in sch.foreign_keys:
            parent, child = fk.parent_table, fk.child_table
            if parent == child or (parent, child) in seen:
                continue
            if child in embedded or child in embed_parents:
                continue                                # child must be a referenced leaf
            if parent in embedded:
                continue                                # parent must be a root, keep one level
            try:
                max_per = _max_children_per_parent(source, db_id, child, fk.child_col)
            except Exception:  # noqa: BLE001 - probing failure -> assume 1:many (conservative)
                max_per = 2
            if max_per <= 1:
                continue                                # only genuine 1:many becomes an array
            seen.add((parent, child))
            array_projections.setdefault(parent, []).append(
                ArrayProjEdge(parent, child, fk.child_col, fk.parent_col, cap=array_proj_cap)
            )
    return MigrationPlan(db_id=db_id, roots=roots, embeds=embeds,
                         references=references, sample_caps=sample_caps,
                         array_projections=array_projections)


def _max_children_per_parent(source: BirdSource, db_id: str, child: str, fk_col: str) -> int:
    conn = source.connection(db_id)
    q = (f"SELECT MAX(c) FROM (SELECT COUNT(*) c FROM {_q(child)} "
         f"WHERE {_q(fk_col)} IS NOT NULL GROUP BY {_q(fk_col)})")
    row = conn.execute(q).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _row_to_doc(cols: list[str], values: tuple, *, pk_cols: list[str] | None) -> dict[str, Any]:
    """NULL -> missing key; set _id from pk when available."""
    doc: dict[str, Any] = {}
    for c, v in zip(cols, values):
        if v is None:
            continue                                   # missing, not JSON null
        doc[c] = v
    pk_cols = pk_cols or []
    if len(pk_cols) == 1 and pk_cols[0] in doc:
        doc["_id"] = doc[pk_cols[0]]
    elif pk_cols and all(c in doc for c in pk_cols):
        doc["_id"] = "|".join(str(doc[c]) for c in pk_cols)
    return doc


def _fetch_rows(
    source: BirdSource,
    db_id: str,
    table: str,
    cap: int | None,
    pk_cols: list[str] | None,
) -> tuple[list[str], list[tuple]]:
    conn = source.connection(db_id)
    order_cols = pk_cols or []
    order_by = f" ORDER BY {', '.join(_q(c) for c in order_cols)}" if order_cols else ""
    cur = conn.execute(f"SELECT * FROM {_q(table)}{order_by}")
    cols = [d[0] for d in cur.description]
    allrows = cur.fetchall()
    if not order_cols:
        allrows = sorted(allrows, key=_stable_row_key)
    if cap is not None and len(allrows) > cap:
        allrows = allrows[:cap]
    return cols, allrows


def migrate(
    source: BirdSource,
    db_id: str,
    plan: MigrationPlan,
    *,
    event_hook: MigrationEventHook | None = None,
) -> dict[str, list[dict]]:
    """Materialize ``{collection: [documents]}`` per the plan (deterministic)."""
    sch = source.schema(db_id)
    out: dict[str, list[dict]] = {}

    # 1) embedded children: index child docs by their FK value for attachment
    child_index: dict[str, dict[Any, list[dict]]] = {}
    for edges in plan.embeds.values():
        for e in edges:
            child_pk = sch.primary_keys.get(e.child)
            source_row_count = source.row_count(db_id, e.child)
            _emit_migration_event(
                event_hook,
                "migration_table_start",
                db_id=db_id,
                table=e.child,
                role="embedded_child",
                source_row_count=source_row_count,
                cap=None,
            )
            cols, rows = _fetch_rows(source, db_id, e.child, None, child_pk)
            idx: dict[Any, list[dict]] = {}
            for r in rows:
                doc = _row_to_doc(cols, r, pk_cols=child_pk)
                idx.setdefault(dict(zip(cols, r)).get(e.fk_col), []).append(doc)
            child_index[e.child] = idx
            _emit_migration_event(
                event_hook,
                "migration_table_done",
                db_id=db_id,
                table=e.child,
                role="embedded_child",
                source_row_count=source_row_count,
                materialized_row_count=len(rows),
                cap=None,
                capped=False,
            )

    # 1b) array-projection children: index (capped per parent) for nested-array attachment
    proj_index: dict[str, dict[Any, list[dict]]] = {}
    for edges in plan.array_projections.values():
        for e in edges:
            if e.child in proj_index:
                continue
            child_pk = sch.primary_keys.get(e.child)
            cols, child_rows = _fetch_rows(source, db_id, e.child, None, child_pk)
            idx: dict[Any, list[dict]] = {}
            for r in child_rows:
                fk_val = dict(zip(cols, r)).get(e.fk_col)
                bucket = idx.setdefault(fk_val, [])
                if len(bucket) >= e.cap:
                    continue                              # cap children per parent (stable order)
                doc = _row_to_doc(cols, r, pk_cols=child_pk)
                bucket.append({k: v for k, v in doc.items() if k not in (e.fk_col, "_id")})
            proj_index[e.child] = idx

    # 2) root collections, with embeds attached
    for table in plan.roots:
        cap = plan.sample_caps.get(table)
        pk = sch.primary_keys.get(table)
        source_row_count = source.row_count(db_id, table)
        _emit_migration_event(
            event_hook,
            "migration_table_start",
            db_id=db_id,
            table=table,
            role="root",
            source_row_count=source_row_count,
            cap=cap,
        )
        cols, rows = _fetch_rows(source, db_id, table, cap, pk)
        docs: list[dict] = []
        for r in rows:
            doc = _row_to_doc(cols, r, pk_cols=pk)
            for e in plan.embeds.get(table, []):
                pk_val = doc.get(e.parent_pk)
                kids = child_index.get(e.child, {}).get(pk_val, [])
                # strip the redundant FK back-reference inside embedded children
                kids = [{k: v for k, v in kid.items() if k not in (e.fk_col, "_id")}
                        for kid in kids]
                if not kids:
                    continue                           # optional embed: omit when absent
                doc[e.child] = kids if e.as_array else kids[0]
            for pe in plan.array_projections.get(table, []):
                projected = proj_index.get(pe.child, {}).get(doc.get(pe.parent_pk), [])
                if projected:
                    doc[pe.child] = projected          # nested array; absent when parent has none
            docs.append(doc)
        out[table] = docs
        _emit_migration_event(
            event_hook,
            "migration_table_done",
            db_id=db_id,
            table=table,
            role="root",
            source_row_count=source_row_count,
            materialized_row_count=len(docs),
            cap=cap,
            capped=cap is not None and source_row_count > cap,
        )
    return out
