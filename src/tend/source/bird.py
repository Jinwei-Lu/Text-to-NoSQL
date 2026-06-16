"""BIRD mini-dev loader: schema, workload, column semantics, and SQLite probes.

One :class:`BirdSource` is built per construction run and shared read-only by native
materializers, validators, and census helpers. It is deliberately pure data access:
MongoDB design decisions live in `tend.construction.designs`.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..errors import SourceError

# canonical BIRD mini-dev databases (11); fail-fast if any is missing (test-only needs all)
BIRD_DBS: tuple[str, ...] = (
    "california_schools", "card_games", "codebase_community",
    "debit_card_specializing", "european_football_2", "financial",
    "formula_1", "student_club", "superhero", "thrombosis_prediction",
    "toxicology",
)

DOMAIN: dict[str, str] = {
    "financial": "finance", "debit_card_specializing": "finance",
    "california_schools": "education", "student_club": "education",
    "codebase_community": "community", "card_games": "games",
    "european_football_2": "sports", "formula_1": "sports",
    "superhero": "entertainment", "toxicology": "chemistry",
    "thrombosis_prediction": "medical",
}


@dataclass(frozen=True)
class ColumnSchema:
    table: str
    name: str
    type: str                       # BIRD column_types entry (text/integer/real/date/...)
    description: str = ""
    value_description: str = ""      # enum/semantics text from database_description csv

    @property
    def has_enum(self) -> bool:
        return bool(self.value_description.strip())


@dataclass(frozen=True)
class ForeignKey:
    child_table: str
    child_col: str
    parent_table: str
    parent_col: str


@dataclass(frozen=True)
class WorkloadQuery:
    question_id: int
    question: str
    evidence: str
    sql: str
    difficulty: str                  # simple | moderate | challenging


@dataclass
class DbSchema:
    db_id: str
    domain: str
    tables: list[str]
    columns: list[ColumnSchema]
    foreign_keys: list[ForeignKey]
    primary_keys: dict[str, list[str]]  # table -> pk columns, preserving composite PKs
    sqlite_path: Path

    def columns_of(self, table: str) -> list[ColumnSchema]:
        return [c for c in self.columns if c.table == table]

    @property
    def table_count(self) -> int:
        return len(self.tables)


class BirdSource:
    """Loads and caches all BIRD mini-dev assets for a run."""

    def __init__(self, bird_root: Path) -> None:
        self.root = Path(bird_root)
        if not self.root.exists():
            raise SourceError(f"bird_root does not exist: {self.root}")
        self._tables_json = self.root / "dev_tables.json"
        self._workload_json = self.root / "mini_dev_sqlite.json"
        for p in (self._tables_json, self._workload_json):
            if not p.exists():
                raise SourceError(f"missing BIRD asset: {p}")
        self._schemas: dict[str, DbSchema] = {}
        self._workload: dict[str, list[WorkloadQuery]] = {}
        self._conns: dict[str, sqlite3.Connection] = {}
        self._load_schemas()
        self._load_workload()

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #
    def _load_schemas(self) -> None:
        try:
            raw = json.loads(self._tables_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SourceError(
                "failed reading dev_tables.json",
                context={"path": str(self._tables_json), "error_type": type(exc).__name__},
            ) from exc
        if not isinstance(raw, list):
            raise SourceError(
                "dev_tables.json must contain a list",
                context={"path": str(self._tables_json), "field": "root"},
            )
        by_db: dict[str, dict] = {}
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise SourceError(
                    "dev_tables.json entry must be an object",
                    context={"path": str(self._tables_json), "index": index},
                )
            try:
                db_id = entry["db_id"]
            except KeyError as exc:
                raise SourceError(
                    "dev_tables.json entry missing db_id",
                    context={"path": str(self._tables_json), "index": index, "field": "db_id"},
                ) from exc
            by_db[str(db_id)] = entry
        missing = [db for db in BIRD_DBS if db not in by_db]
        if missing:
            raise SourceError(
                "dev_tables.json missing expected dbs",
                context={
                    "path": str(self._tables_json),
                    "missing": missing,
                    "found": sorted(by_db),
                },
            )
        for db_id in BIRD_DBS:
            self._schemas[db_id] = self._build_db_schema(db_id, by_db[db_id])

    def _build_db_schema(self, db_id: str, d: dict) -> DbSchema:
        tnames = self._schema_list_field(db_id, d, "table_names_original")
        gcols = self._schema_list_field(db_id, d, "column_names_original")
        ctypes = self._schema_list_field(db_id, d, "column_types")
        if len(ctypes) != len(gcols):
            raise SourceError(
                "dev_tables.json column_types length mismatch",
                context={
                    "path": str(self._tables_json),
                    "db_id": db_id,
                    "field": "column_types",
                    "column_count": len(gcols),
                    "type_count": len(ctypes),
                },
            )
        sqlite_path = self.root / "dev_databases" / db_id / f"{db_id}.sqlite"
        if not sqlite_path.exists():
            raise SourceError(f"missing sqlite for {db_id}", context={"path": str(sqlite_path)})

        descs = self._load_descriptions(db_id, tnames)
        columns: list[ColumnSchema] = []
        gidx_to_col: dict[int, ColumnSchema] = {}
        for i, raw_col in enumerate(gcols):
            try:
                tbl_idx, cname = raw_col
            except (TypeError, ValueError) as exc:
                raise SourceError(
                    "dev_tables.json column entry malformed",
                    context={
                        "path": str(self._tables_json),
                        "db_id": db_id,
                        "field": "column_names_original",
                        "index": i,
                    },
                ) from exc
            if not isinstance(tbl_idx, int):
                raise SourceError(
                    "dev_tables.json table index must be an integer",
                    context={
                        "path": str(self._tables_json),
                        "db_id": db_id,
                        "field": "column_names_original",
                        "index": i,
                    },
                )
            if tbl_idx < 0:  # the synthetic "*" column
                continue
            if tbl_idx >= len(tnames):
                raise SourceError(
                    "dev_tables.json table index out of range",
                    context={
                        "path": str(self._tables_json),
                        "db_id": db_id,
                        "field": "column_names_original",
                        "index": i,
                        "table_index": tbl_idx,
                        "table_count": len(tnames),
                    },
                )
            table = tnames[tbl_idx]
            desc, vdesc = descs.get((table.lower(), cname.lower()), ("", ""))
            col = ColumnSchema(table=table, name=cname, type=ctypes[i],
                               description=desc, value_description=vdesc)
            columns.append(col)
            gidx_to_col[i] = col

        fks: list[ForeignKey] = []
        for fk_index, raw_fk in enumerate(d.get("foreign_keys", [])):
            try:
                child_idx, parent_idx = raw_fk
            except (TypeError, ValueError) as exc:
                raise SourceError(
                    "dev_tables.json foreign key entry malformed",
                    context={
                        "path": str(self._tables_json),
                        "db_id": db_id,
                        "field": "foreign_keys",
                        "index": fk_index,
                    },
                ) from exc
            for role, col_idx in (("child", child_idx), ("parent", parent_idx)):
                if not isinstance(col_idx, int) or col_idx < 0 or col_idx >= len(gcols):
                    raise SourceError(
                        "dev_tables.json foreign key column index out of range",
                        context={
                            "path": str(self._tables_json),
                            "db_id": db_id,
                            "field": "foreign_keys",
                            "index": fk_index,
                            "role": role,
                            "column_index": col_idx,
                            "column_count": len(gcols),
                        },
                    )
            c, p = gidx_to_col.get(child_idx), gidx_to_col.get(parent_idx)
            if c and p:
                fks.append(ForeignKey(c.table, c.name, p.table, p.name))

        pks: dict[str, list[str]] = {}
        for pk_index, pk in enumerate(d.get("primary_keys", [])):
            idxs = pk if isinstance(pk, list) else [pk]
            for col_idx in idxs:
                if not isinstance(col_idx, int) or col_idx < 0 or col_idx >= len(gcols):
                    raise SourceError(
                        "dev_tables.json primary key column index out of range",
                        context={
                            "path": str(self._tables_json),
                            "db_id": db_id,
                            "field": "primary_keys",
                            "index": pk_index,
                            "column_index": col_idx,
                            "column_count": len(gcols),
                        },
                    )
            cols = [gidx_to_col[i] for i in idxs if i in gidx_to_col]
            if cols and cols[0].table not in pks:
                pks[cols[0].table] = [c.name for c in cols]

        return DbSchema(
            db_id=db_id, domain=DOMAIN.get(db_id, "unknown"), tables=tnames,
            columns=columns, foreign_keys=fks, primary_keys=pks, sqlite_path=sqlite_path,
        )

    def _schema_list_field(self, db_id: str, data: dict, field_name: str) -> list:
        try:
            value = data[field_name]
        except KeyError as exc:
            raise SourceError(
                "dev_tables.json schema entry missing field",
                context={
                    "path": str(self._tables_json),
                    "db_id": db_id,
                    "field": field_name,
                },
            ) from exc
        if not isinstance(value, list):
            raise SourceError(
                "dev_tables.json schema field must be a list",
                context={
                    "path": str(self._tables_json),
                    "db_id": db_id,
                    "field": field_name,
                },
            )
        return value

    def _load_descriptions(
        self, db_id: str, tables: list[str]
    ) -> dict[tuple[str, str], tuple[str, str]]:
        """(table_lower, col_lower) -> (column_description, value_description)."""
        out: dict[tuple[str, str], tuple[str, str]] = {}
        desc_dir = self.root / "dev_databases" / db_id / "database_description"
        if not desc_dir.is_dir():
            return out
        for table in tables:
            csv_path = desc_dir / f"{table}.csv"
            if not csv_path.exists():
                continue
            try:
                with open(csv_path, encoding="utf-8-sig", errors="replace", newline="") as f:
                    for row in csv.DictReader(f):
                        col = (row.get("original_column_name") or "").strip()
                        if not col:
                            continue
                        out[(table.lower(), col.lower())] = (
                            (row.get("column_description") or "").strip(),
                            (row.get("value_description") or "").strip(),
                        )
            except (OSError, csv.Error) as exc:  # malformed description is non-fatal
                raise SourceError(
                    f"failed reading description csv: {csv_path}",
                    context={"db_id": db_id, "table": table},
                ) from exc
        return out

    def _load_workload(self) -> None:
        raw = json.loads(self._workload_json.read_text(encoding="utf-8"))
        for r in raw:
            q = WorkloadQuery(
                question_id=int(r["question_id"]), question=r.get("question", ""),
                evidence=r.get("evidence", ""), sql=r.get("SQL", ""),
                difficulty=r.get("difficulty", ""),
            )
            self._workload.setdefault(r["db_id"], []).append(q)

    # ------------------------------------------------------------------ #
    # accessors
    # ------------------------------------------------------------------ #
    @property
    def db_ids(self) -> tuple[str, ...]:
        return BIRD_DBS

    def schema(self, db_id: str) -> DbSchema:
        try:
            return self._schemas[db_id]
        except KeyError as exc:
            raise SourceError(f"unknown db_id: {db_id}",
                              context={"known": list(self._schemas)}) from exc

    def workload(self, db_id: str) -> list[WorkloadQuery]:
        return self._workload.get(db_id, [])

    def connection(self, db_id: str) -> sqlite3.Connection:
        """Cached read-only SQLite connection."""
        if db_id not in self._conns:
            path = self.schema(db_id).sqlite_path
            self._conns[db_id] = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return self._conns[db_id]

    # ------------------------------------------------------------------ #
    # SQLite probes (deterministic; used by census and mechanism detectors)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _q(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    @lru_cache(maxsize=4096)
    def row_count(self, db_id: str, table: str) -> int:
        try:
            cur = self.connection(db_id).execute(f"SELECT COUNT(*) FROM {self._q(table)}")
            return int(cur.fetchone()[0])
        except sqlite3.Error as exc:
            raise SourceError(f"row_count failed for {db_id}.{table}") from exc

    @lru_cache(maxsize=8192)
    def distinct_count(self, db_id: str, table: str, col: str) -> int:
        cur = self.connection(db_id).execute(
            f"SELECT COUNT(DISTINCT {self._q(col)}) FROM {self._q(table)}")
        return int(cur.fetchone()[0])

    @lru_cache(maxsize=8192)
    def null_rate(self, db_id: str, table: str, col: str) -> float:
        n = self.row_count(db_id, table)
        if n == 0:
            return 0.0
        cur = self.connection(db_id).execute(
            f"SELECT SUM(CASE WHEN {self._q(col)} IS NULL THEN 1 ELSE 0 END) "
            f"FROM {self._q(table)}")
        nnull = cur.fetchone()[0] or 0
        return nnull / n

    def fk_coverage(self, db_id: str, fk: ForeignKey) -> float:
        """Fraction of parent rows referenced by >=1 child (low => sparse optionality)."""
        n_parent = self.row_count(db_id, fk.parent_table)
        if n_parent == 0:
            return 0.0
        cur = self.connection(db_id).execute(
            f"SELECT COUNT(DISTINCT {self._q(fk.child_col)}) FROM {self._q(fk.child_table)} "
            f"WHERE {self._q(fk.child_col)} IS NOT NULL")
        return int(cur.fetchone()[0]) / n_parent

    def close(self) -> None:
        for conn in self._conns.values():
            conn.close()
        self._conns.clear()

    def __enter__(self) -> "BirdSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
