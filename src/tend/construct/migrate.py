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

import hashlib
from dataclasses import dataclass, field
from typing import Any

from ..source import BirdSource, DbSchema

DEFAULT_REF_SAMPLE_CAP = 40_000      # max rows kept per referenced fact table


@dataclass
class EmbedEdge:
    parent: str
    child: str
    fk_col: str                      # child column referencing parent pk
    parent_pk: str
    as_array: bool                   # True if a parent can have >1 child


@dataclass
class MigrationPlan:
    db_id: str
    roots: list[str]
    embeds: dict[str, list[EmbedEdge]] = field(default_factory=dict)   # parent -> edges
    references: list[str] = field(default_factory=list)
    sample_caps: dict[str, int] = field(default_factory=dict)

    def embedded_tables(self) -> set[str]:
        return {e.child for edges in self.embeds.values() for e in edges}


def _stable_hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()


def build_plan(source: BirdSource, db_id: str, *,
               ref_sample_cap: int = DEFAULT_REF_SAMPLE_CAP) -> MigrationPlan:
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
    sample_caps = {t: ref_sample_cap for t in references
                   if rows.get(t, 0) > ref_sample_cap}
    roots = references                                  # every non-embedded table is a root
    return MigrationPlan(db_id=db_id, roots=roots, embeds=embeds,
                         references=references, sample_caps=sample_caps)


def _max_children_per_parent(source: BirdSource, db_id: str, child: str, fk_col: str) -> int:
    conn = source.connection(db_id)
    q = (f'SELECT MAX(c) FROM (SELECT COUNT(*) c FROM "{child}" '
         f'WHERE "{fk_col}" IS NOT NULL GROUP BY "{fk_col}")')
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


def _fetch_rows(source: BirdSource, db_id: str, table: str,
                cap: int | None) -> tuple[list[str], list[tuple]]:
    conn = source.connection(db_id)
    cur = conn.execute(f'SELECT * FROM "{table}"')
    cols = [d[0] for d in cur.description]
    allrows = cur.fetchall()
    if cap is not None and len(allrows) > cap:
        allrows = sorted(allrows, key=lambda r: _stable_hash(table, r))[:cap]
    return cols, allrows


def migrate(source: BirdSource, db_id: str, plan: MigrationPlan) -> dict[str, list[dict]]:
    """Materialize ``{collection: [documents]}`` per the plan (deterministic)."""
    sch = source.schema(db_id)
    out: dict[str, list[dict]] = {}

    # 1) embedded children: index child docs by their FK value for attachment
    child_index: dict[str, dict[Any, list[dict]]] = {}
    for edges in plan.embeds.values():
        for e in edges:
            cols, rows = _fetch_rows(source, db_id, e.child, None)
            child_pk = sch.primary_keys.get(e.child)
            idx: dict[Any, list[dict]] = {}
            for r in rows:
                doc = _row_to_doc(cols, r, pk_cols=child_pk)
                idx.setdefault(dict(zip(cols, r)).get(e.fk_col), []).append(doc)
            child_index[e.child] = idx

    # 2) root collections, with embeds attached
    for table in plan.roots:
        cap = plan.sample_caps.get(table)
        cols, rows = _fetch_rows(source, db_id, table, cap)
        pk = sch.primary_keys.get(table)
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
            docs.append(doc)
        out[table] = docs
    return out
