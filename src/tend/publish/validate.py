"""Record + release validation against the 02 contracts (C1-C9, H1/H4-H9, JSON Schema).

Deterministic publish gate. ``validate_record`` checks one record's field contract; an optional
``MongoExecutor`` + witness enables the executable C5 check (gold must be a gold-class member).
``validate_composition`` checks the test-set composition hard constraints (02 §02-4-3).
``validate_release`` ties it together over a written release directory.
"""
from __future__ import annotations

import concurrent.futures
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..execution import world_signature
from ..execution import ast_check
from ..execution.ast_check import DISABLED_OPERATORS, DISABLED_SYSTEM_VARS

DISABLED_TOKENS: frozenset[str] = DISABLED_OPERATORS | DISABLED_SYSTEM_VARS
DIFFICULTIES = ("L0", "L1", "L2", "L3", "L4")
SHAPE_POLICIES = ("preserve", "reshape", "reduce")
SQL_INFEASIBILITY_CLASSES = (
    "feasible", "semantic", "performative", "structural_pipeline", "structural_schema_flex")
SCHEMA_FLEX_MODES = ("none", "polymorphic", "attribute_bag", "schema_versioning", "dynamic_key")

GOLD_REQUIRED = ("record_id", "db_id", "nl_queries", "MQL", "canonical_form_set")
PUBLISH_REQUIRED = ("difficulty", "sql_infeasibility_class", "shape_policy", "world_signature")

# composition thresholds (02 §02-4-3)
H5_L4_MIN = 0.30
H8_L0_MAX = 0.05
H7_FLEX_MIN, H7_FLEX_MIN_RELAXED = 0.25, 0.15
H9_SSF_MIN, H9_SSF_MIN_RELAXED = 0.20, 0.10
BIRD_DB_COUNT = 11


# --------------------------------------------------------------------------- #
# per-record (C1-C9)
# --------------------------------------------------------------------------- #
def validate_record(
    record: dict[str, Any], *, executor: Any = None, snapshot: dict[str, Any] | None = None,
    refs_base: Path | None = None,
) -> list[str]:
    """Return record-contract violations (empty = OK). C-ids reference 02 §02-2-4."""
    iss: list[str] = []
    rid = record.get("record_id", "?")

    for fld in GOLD_REQUIRED + PUBLISH_REQUIRED:                       # C2/C4 presence
        if fld not in record:
            iss.append(f"[C2/C4 r{rid}] missing required field '{fld}'")

    nlq = record.get("nl_queries")
    if not isinstance(nlq, dict) or set(nlq) != {"canonical", "colloquial"}:   # C2
        iss.append(f"[C2 r{rid}] nl_queries must contain exactly canonical+colloquial")
    elif not (str(nlq.get("canonical", "")).strip() and str(nlq.get("colloquial", "")).strip()):
        iss.append(f"[C3 r{rid}] canonical/colloquial must be non-empty")  # C3

    cfs = record.get("canonical_form_set")
    if not isinstance(cfs, dict):
        iss.append(f"[C6 r{rid}] canonical_form_set missing/invalid")
    else:
        mnc = set(cfs.get("must_not_contain", []))
        missing = DISABLED_TOKENS - mnc
        if missing:                                                    # C6
            iss.append(f"[C6 r{rid}] must_not_contain missing disabled ops {sorted(missing)}")

    mql = record.get("MQL", "")
    if isinstance(cfs, dict) and isinstance(mql, str) and mql:         # C5 structural half
        try:
            ok, hits = ast_check(mql, cfs)
        except Exception as exc:  # noqa: BLE001 - static check failures are record defects
            iss.append(f"[C5 r{rid}] gold MQL static check errored: {exc}")
        else:
            if not ok:
                iss.append(f"[C5 r{rid}] gold MQL fails its own AST_check: {hits}")
    if executor is not None and snapshot is not None and isinstance(cfs, dict):  # C5 executable
        try:
            if hasattr(executor, "ex_verdict"):
                ok = executor.ex_verdict(mql, record, snapshot)
                if not ok:
                    iss.append(f"[C5 r{rid}] gold MQL is not a gold-class member on its witness")
            else:
                executor.norm_exec(record["db_id"], mql)
        except Exception as exc:  # noqa: BLE001
            iss.append(f"[C5 r{rid}] gold MQL execution check errored: {exc}")

    diff = record.get("difficulty")
    if diff not in DIFFICULTIES:                                       # C7
        iss.append(f"[C7 r{rid}] difficulty {diff!r} not in {DIFFICULTIES}")
    sic = record.get("sql_infeasibility_class")
    if sic not in SQL_INFEASIBILITY_CLASSES:                           # C7
        iss.append(f"[C7 r{rid}] sql_infeasibility_class {sic!r} invalid")
    if record.get("shape_policy") not in SHAPE_POLICIES:
        iss.append(f"[C7 r{rid}] shape_policy {record.get('shape_policy')!r} invalid")

    flex = record.get("schema_flex")
    if flex is not None and flex not in SCHEMA_FLEX_MODES:             # C9
        iss.append(f"[C9 r{rid}] schema_flex {flex!r} invalid")
    if sic == "structural_schema_flex":                               # C9 compatibility
        if diff != "L4":
            iss.append(f"[C9 r{rid}] structural_schema_flex requires difficulty L4 (got {diff})")
        if (flex or "none") == "none":
            iss.append(f"[C9 r{rid}] structural_schema_flex requires schema_flex != none")

    for k, v in record.items():                                        # C1 omit-key semantics
        if k.endswith("_ref"):
            if not isinstance(v, str) or not v:
                iss.append(f"[C1 r{rid}] ref field '{k}' must be a non-empty path or omitted")
            elif refs_base is not None and not (refs_base / v).exists() \
                    and not (Path(v)).exists():                        # C8 dereferenceable
                iss.append(f"[C8 r{rid}] ref '{k}' -> '{v}' does not resolve")
    if record.get("schema_flex", "x") in (None, "", "none") and "schema_flex" in record \
            and record["schema_flex"] in (None, ""):
        iss.append(f"[C1 r{rid}] schema_flex present but empty/null (omit instead)")
    return iss


def validate_record_jsonschema(record: dict[str, Any], schema_path: Path) -> list[str]:
    """Validate a record against proposals/schemas/record.schema.json."""
    schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
    return _validate_record_jsonschema_with_schema(record, schema)


def _validate_record_jsonschema_with_schema(
    record: dict[str, Any], schema: dict[str, Any]
) -> list[str]:
    import jsonschema

    validator = jsonschema.Draft202012Validator(schema)
    return [f"[jsonschema r{record.get('record_id','?')}] {e.message} at /{'/'.join(map(str,e.path))}"
            for e in validator.iter_errors(record)]


# --------------------------------------------------------------------------- #
# composition (H1/H4-H9)
# --------------------------------------------------------------------------- #
@dataclass
class CompositionReport:
    ok: bool
    n: int
    db_ids: list[str]
    l4_ratio: float
    l0_ratio: float
    flex_ratio: float
    ssf_ratio: float
    supply_relax: bool
    violations: list[str] = field(default_factory=list)


def validate_composition(
    records: list[dict[str, Any]], *, supply_relax: bool = False, require_all_dbs: bool = True
) -> CompositionReport:
    """Check H1/H4-H9 test-composition hard constraints (02 §02-II-3)."""
    n = len(records)
    viol: list[str] = []
    db_ids = sorted({r.get("db_id") for r in records if r.get("db_id")})
    if n == 0:
        return CompositionReport(False, 0, [], 0, 0, 0, 0, supply_relax, ["empty record set"])

    def ratio(pred) -> float:
        return sum(1 for r in records if pred(r)) / n

    l4 = ratio(lambda r: r.get("difficulty") == "L4")
    l0 = ratio(lambda r: r.get("difficulty") == "L0")
    flex = ratio(lambda r: (r.get("schema_flex") or "none") != "none")
    ssf = ratio(lambda r: r.get("sql_infeasibility_class") == "structural_schema_flex")

    h7 = H7_FLEX_MIN_RELAXED if supply_relax else H7_FLEX_MIN
    h9 = H9_SSF_MIN_RELAXED if supply_relax else H9_SSF_MIN
    if require_all_dbs and len(db_ids) != BIRD_DB_COUNT:
        viol.append(f"[H4] db coverage {len(db_ids)} != {BIRD_DB_COUNT}")
    if l4 < H5_L4_MIN:
        viol.append(f"[H5] L4 ratio {l4:.3f} < {H5_L4_MIN}")
    if l0 > H8_L0_MAX:
        viol.append(f"[H8] L0 ratio {l0:.3f} > {H8_L0_MAX}")
    if flex < h7:
        viol.append(f"[H7] schema_flex ratio {flex:.3f} < {h7}" + (" [relaxed]" if supply_relax else ""))
    if ssf < h9:
        viol.append(f"[H9] structural_schema_flex ratio {ssf:.3f} < {h9}"
                    + (" [relaxed]" if supply_relax else ""))
    return CompositionReport(not viol, n, db_ids, round(l4, 3), round(l0, 3), round(flex, 3),
                             round(ssf, 3), supply_relax, viol)


# --------------------------------------------------------------------------- #
# release
# --------------------------------------------------------------------------- #
@dataclass
class ReleaseReport:
    ok: bool
    n_records: int
    record_violations: list[str]
    schema_violations: list[str]
    composition: CompositionReport
    file_violations: list[str]

    def summary(self) -> str:
        c = self.composition
        return (f"release {'OK' if self.ok else 'INVALID'}: {self.n_records} records, "
                f"L4={c.l4_ratio:.0%} L0={c.l0_ratio:.0%} flex={c.flex_ratio:.0%} ssf={c.ssf_ratio:.0%}; "
                f"{len(self.record_violations)} record + {len(self.schema_violations)} schema + "
                f"{len(self.file_violations)} file violations")


def validate_release(
    out_dir: str | Path, *, schemas_dir: str | Path | None = None,
    executor: Any = None, supply_relax: bool = False, require_all_dbs: bool = True,
) -> ReleaseReport:
    """Validate a written release: records (C1-C9 + jsonschema), composition (H), 3-way files (C4)."""
    out_dir = Path(out_dir)
    test_path = out_dir / "test.json"
    tend_path = out_dir / "TEND.json"
    file_viol: list[str] = []
    test = json.loads(test_path.read_text(encoding="utf-8"))
    if tend_path.exists():
        tend = json.loads(tend_path.read_text(encoding="utf-8"))
        if tend != test:
            file_viol.append("[C4] TEND.json does not match test.json")
    else:
        file_viol.append("[C4] missing TEND.json")
    records = test if isinstance(test, list) else test.get("records", [])

    rec_viol: list[str] = []
    sch_viol: list[str] = []
    schemas_path = Path(schemas_dir) if schemas_dir else None
    schema_path = schemas_path / "record.schema.json" if schemas_path else None
    snapshots: dict[str, Any] = {}
    for r in records:
        db = r.get("db_id")
        if db and db not in snapshots:
            p = out_dir / "mongodb_data" / f"{db}.json"
            snapshots[db] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    record_schema = (
        json.loads(schema_path.read_text(encoding="utf-8"))
        if schema_path and schema_path.exists()
        else None
    )

    def validate_one(index_record: tuple[int, dict[str, Any]]) -> tuple[int, list[str], list[str]]:
        index, r = index_record
        local_rec: list[str] = []
        local_sch: list[str] = []
        db = r.get("db_id")
        snap = snapshots.get(db) if db else None
        if db and snap is not None and r.get("world_signature") != world_signature(snap):
            local_rec.append(
                f"[C4 r{r.get('record_id','?')}] world_signature does not match "
                f"mongodb_data/{db}.json"
            )
        local_rec += validate_record(r, executor=executor, snapshot=snap, refs_base=out_dir)
        if record_schema is not None:
            local_sch += _validate_record_jsonschema_with_schema(r, record_schema)
        return index, local_rec, local_sch

    if records:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(records)) as pool:
            record_results = list(pool.map(validate_one, enumerate(records)))
        for _, local_rec, local_sch in sorted(record_results, key=lambda item: item[0]):
            rec_viol += local_rec
            sch_viol += local_sch

    comp = validate_composition(records, supply_relax=supply_relax, require_all_dbs=require_all_dbs)

    # C4 3-way per-db file presence
    for db in comp.db_ids:
        for sub in ("mongodb_schema", "mongodb_data", "agent_design_rationale"):
            ext = "yaml" if sub == "agent_design_rationale" else "json"
            if not (out_dir / sub / f"{db}.{ext}").exists():
                file_viol.append(f"[C4] missing {sub}/{db}.{ext}")
    if schemas_path:
        file_viol += _validate_release_artifacts(out_dir, comp.db_ids, schemas_path)

    ok = not (rec_viol or sch_viol or file_viol) and comp.ok
    return ReleaseReport(ok, len(records), rec_viol, sch_viol, comp, file_viol)


def _validate_release_artifacts(
    out_dir: Path, db_ids: list[str], schemas_dir: Path
) -> list[str]:
    """Validate release-level artifacts when proposal schemas are available."""
    try:
        import jsonschema
    except ImportError:
        return ["[schema] jsonschema is not installed; cannot validate release artifacts"]

    lib_path = schemas_dir / "library.schema.json"
    adr_path = schemas_dir / "agent_design_rationale.schema.json"
    lib = json.loads(lib_path.read_text(encoding="utf-8")) if lib_path.exists() else None
    adr_schema = json.loads(adr_path.read_text(encoding="utf-8")) if adr_path.exists() else None

    def check(schema: dict[str, Any], obj: Any, label: str) -> list[str]:
        validator = jsonschema.Draft202012Validator(schema)
        issues: list[str] = []
        for err in validator.iter_errors(obj):
            path = "/".join(map(str, err.path))
            issues.append(f"[schema {label}] {err.message} at /{path}")
        return issues

    def lib_ref(name: str) -> dict[str, Any]:
        assert lib is not None
        return {"$schema": lib.get("$schema"), "$defs": lib.get("$defs", {}),
                "$ref": f"#/$defs/{name}"}

    issues: list[str] = []
    if lib is not None:
        catalog_path = out_dir / "bird_db_catalog.json"
        if catalog_path.exists():
            issues += check(
                lib_ref("bird_db_catalog"),
                json.loads(catalog_path.read_text(encoding="utf-8")),
                "bird_db_catalog.json",
            )
        else:
            issues.append("[C4] missing bird_db_catalog.json")

    def check_db(db: str) -> list[str]:
        db_issues: list[str] = []
        if lib is not None:
            for sub, ref in (("mongodb_schema", "mongodb_schema"), ("mongodb_data", "mongodb_data")):
                path = out_dir / sub / f"{db}.json"
                if path.exists():
                    db_issues += check(
                        lib_ref(ref),
                        json.loads(path.read_text(encoding="utf-8")),
                        f"{sub}/{db}.json",
                    )
        if adr_schema is not None:
            path = out_dir / "agent_design_rationale" / f"{db}.yaml"
            if path.exists():
                db_issues += check(
                    adr_schema,
                    yaml.safe_load(path.read_text(encoding="utf-8")),
                    f"agent_design_rationale/{db}.yaml",
                )
        return db_issues

    if db_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(db_ids)) as pool:
            for db_issues in pool.map(check_db, db_ids):
                issues += db_issues
    return issues
