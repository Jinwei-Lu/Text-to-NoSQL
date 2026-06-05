"""Record + release validation against the 02 contracts (C1-C9, H1/H4-H9, JSON Schema).

Deterministic publish gate. ``validate_record`` checks one record's field contract; an optional
``MongoExecutor`` + witness enables the executable C5 check (gold must be a gold-class member).
``validate_composition`` checks the test-set composition hard constraints (02 §02-4-3).
``validate_release`` ties it together over a written release directory.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..construction.recipe import NativeFeatureManifest, load_native_feature_manifest
from ..execution import mql_signature, mql_skeleton_signature, mql_skeleton_summary, world_signature
from ..execution import ast_check
from ..execution.ast_check import DISABLED_OPERATORS, DISABLED_SYSTEM_VARS
from ..construction.verify import verify_native_record
from ..release_layout import ReleaseDatasetLayout, resolve_release_dataset_layout

DISABLED_TOKENS: frozenset[str] = DISABLED_OPERATORS | DISABLED_SYSTEM_VARS
DIFFICULTIES = ("L0", "L1", "L2", "L3", "L4")
SHAPE_POLICIES = ("preserve", "reshape", "reduce")
SQL_INFEASIBILITY_CLASSES = (
    "feasible", "semantic", "performative", "structural_pipeline", "structural_schema_flex")
SCHEMA_FLEX_MODES = (
    "none",
    "polymorphic",
    "attribute_bag",
    "schema_versioning",
    "dynamic_key",
    "derived_tag_array",
    "nested_event_stream",
    "missing_vs_present",
)

GOLD_REQUIRED = ("record_id", "db_id", "nl_queries", "MQL", "canonical_form_set")
PUBLISH_REQUIRED = ("difficulty", "sql_infeasibility_class", "shape_policy", "world_signature")

# all NL must be English-only; this gate fails any record with CJK/kana/fullwidth text
_NLQ_CJK_RE = re.compile(r"[⺀-鿿　-ヿ＀-￯]")

# composition thresholds (02 §02-4-3)
H5_L4_MIN = 0.30
H8_L0_MAX = 0.05
H7_FLEX_MIN, H7_FLEX_MIN_RELAXED = 0.25, 0.15
H9_SSF_MIN, H9_SSF_MIN_RELAXED = 0.20, 0.10
BIRD_DB_COUNT = 11
H11_SKELETON_FAMILY_MAX = 16


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
    elif _NLQ_CJK_RE.search(str(nlq.get("canonical", "")) + str(nlq.get("colloquial", ""))):
        iss.append(f"[C3 r{rid}] nl_queries must be English only (found non-English/CJK text)")

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
            elif refs_base is not None and not _record_ref_exists(refs_base, v):  # C8
                iss.append(f"[C8 r{rid}] ref '{k}' -> '{v}' does not resolve")
    if record.get("schema_flex", "x") in (None, "", "none") and "schema_flex" in record \
            and record["schema_flex"] in (None, ""):
        iss.append(f"[C1 r{rid}] schema_flex present but empty/null (omit instead)")
    return iss


def _record_ref_exists(refs_base: Path, value: str) -> bool:
    ref = Path(value)
    return ref.exists() or (refs_base / ref).exists() or (refs_base / "metadata" / ref).exists()


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
    diversity: "DiversityReport"

    def summary(self) -> str:
        c = self.composition
        d = self.diversity
        return (f"release {'OK' if self.ok else 'INVALID'}: {self.n_records} records, "
                f"L4={c.l4_ratio:.0%} L0={c.l0_ratio:.0%} flex={c.flex_ratio:.0%} ssf={c.ssf_ratio:.0%}; "
                f"distinct_pair={d.distinct_nl_mql_pairs}/{d.n_records} "
                f"min_db_pair={d.min_distinct_nl_mql_pairs_per_db}; "
                f"{len(self.record_violations)} record + {len(self.schema_violations)} schema + "
                f"{len(self.file_violations)} file violations")


@dataclass
class DiversityReport:
    n_records: int
    distinct_mql: int
    distinct_mql_skeletons: int
    distinct_canonical_nl: int
    distinct_nl_mql_pairs: int
    max_mql_skeleton_family: int
    distinct_nl_mql_pairs_per_db: dict[str, int]
    min_distinct_nl_mql_pairs_per_db: int


def validate_release(
    out_dir: str | Path, *, schemas_dir: str | Path | None = None,
    executor: Any = None, supply_relax: bool = False, require_all_dbs: bool = True,
    verify_world_signature: bool = True,
) -> ReleaseReport:
    """Validate a written release: records (C1-C9 + jsonschema), composition (H), 3-way files (C4).

    ``verify_world_signature=False`` skips raw ``mongodb_data`` loading and signature recomputation.
    """
    layout = resolve_release_dataset_layout(out_dir)
    out_dir = layout.root
    test_path = layout.test_path
    tend_path = layout.tend_path
    file_viol: list[str] = []
    test = json.loads(test_path.read_text(encoding="utf-8"))
    if tend_path.exists():
        tend = json.loads(tend_path.read_text(encoding="utf-8"))
        if tend != test:
            file_viol.append("[C4] TEND.json does not match test.json")
    else:
        file_viol.append("[C4] missing TEND.json")
    records = test if isinstance(test, list) else test.get("records", [])
    record_db_ids = sorted({str(r.get("db_id")) for r in records if r.get("db_id")})
    native_mode = layout.native_feature_manifest_dir.is_dir() or any(
        "native_feature_id" in r for r in records
    )
    native_manifests = _load_native_manifests(
        layout.native_feature_manifest_dir,
        record_db_ids,
    ) if native_mode else {}
    native_provenance = _load_native_provenance(layout.provenance_dir, record_db_ids) if native_mode else {}

    rec_viol: list[str] = []
    sch_viol: list[str] = []
    rec_viol += _duplicate_mql_violations(records)
    rec_viol += _mql_skeleton_family_violations(records)
    rec_viol += _duplicate_canonical_nl_violations(records)
    schemas_path = Path(schemas_dir) if schemas_dir else None
    schema_path = schemas_path / "record.schema.json" if schemas_path else None
    snapshots: dict[str, Any] = {}
    snapshot_signatures: dict[str, str] = {}
    if verify_world_signature:
        for r in records:
            db = r.get("db_id")
            if db and db not in snapshots:
                p = layout.mongodb_data_dir / f"{db}.json"
                snapshots[db] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        snapshot_signatures = {
            db: world_signature(snapshot)
            for db, snapshot in snapshots.items()
            if snapshot is not None
        }
    record_schema = (
        json.loads(schema_path.read_text(encoding="utf-8"))
        if schema_path and schema_path.exists()
        else None
    )

    def validate_one(index_record: tuple[int, dict[str, Any]]) -> tuple[int, list[str], list[str]]:
        index, r = index_record
        local_rec: list[str] = []
        local_sch: list[str] = []
        try:
            db = r.get("db_id")
            snap = snapshots.get(db) if db and verify_world_signature else None
            if (
                verify_world_signature
                and db
                and snap is not None
                and r.get("world_signature") != snapshot_signatures.get(db)
            ):
                local_rec.append(
                    f"[C4 r{r.get('record_id','?')}] world_signature does not match "
                    f"mongodb_data/{db}.json"
                )
            local_rec += validate_record(r, executor=executor, snapshot=snap, refs_base=out_dir)
            if native_mode or "native_feature_id" in r:
                local_rec += _validate_native_record(
                    r,
                    native_manifests.get(str(db)),
                    native_provenance.get(str(db)),
                    layout,
                )
            if record_schema is not None:
                local_sch += _validate_record_jsonschema_with_schema(r, record_schema)
        except Exception as exc:  # noqa: BLE001 - surface as validation violation, not pool crash
            local_rec.append(f"[internal-error r{r.get('record_id', '?')}] {exc}")
        return index, local_rec, local_sch

    if records:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(records), 8)) as pool:
            record_results = list(pool.map(validate_one, enumerate(records)))
        for _, local_rec, local_sch in sorted(record_results, key=lambda item: item[0]):
            rec_viol += local_rec
            sch_viol += local_sch

    comp = validate_composition(records, supply_relax=supply_relax, require_all_dbs=require_all_dbs)
    diversity = _diversity_report(records)

    # C4 3-way per-db file presence
    for db in comp.db_ids:
        required_files = (
            ("mongodb_schema", layout.mongodb_schema_dir / f"{db}.json"),
            ("mongodb_data", layout.mongodb_data_dir / f"{db}.json"),
            ("agent_design_rationale", layout.agent_design_rationale_dir / f"{db}.yaml"),
        )
        for label, path in required_files:
            if not path.exists():
                file_viol.append(f"[C4] missing {label}/{db}{path.suffix}")
    if native_mode:
        file_viol += _validate_native_artifacts(
            layout,
            comp.db_ids,
            native_manifests,
            native_provenance,
        )
        rec_viol += _native_coverage_violations(records)
    if schemas_path:
        if native_mode:
            file_viol += _validate_native_catalog_artifact(layout, schemas_path)
        else:
            file_viol += _validate_release_artifacts(
                layout,
                comp.db_ids,
                schemas_path,
                validate_mongodb_data=verify_world_signature,
            )

    ok = not (rec_viol or sch_viol or file_viol) and comp.ok
    return ReleaseReport(ok, len(records), rec_viol, sch_viol, comp, file_viol, diversity)


def _duplicate_mql_violations(records: list[dict[str, Any]]) -> list[str]:
    seen: dict[tuple[Any, str], dict[str, Any]] = {}
    violations: list[str] = []
    for record in records:
        mql = record.get("MQL")
        if not isinstance(mql, str) or not mql.strip():
            continue
        sig = mql_signature(mql)
        key = (record.get("db_id"), sig)
        previous = seen.get(key)
        if previous is None:
            seen[key] = record
            continue
        violations.append(
            "[H10 r{rid}] duplicate MQL for db_id {db!r}; duplicates r{prev} "
            "(mql_signature={sig})".format(
                rid=record.get("record_id", "?"),
                db=record.get("db_id"),
                prev=previous.get("record_id", "?"),
                sig=sig,
            )
        )
    return violations


def _mql_skeleton_family_violations(records: list[dict[str, Any]]) -> list[str]:
    by_family: dict[tuple[Any, str], list[dict[str, Any]]] = {}
    for record in records:
        mql = record.get("MQL")
        if not isinstance(mql, str) or not mql.strip():
            continue
        sig = mql_skeleton_signature(mql)
        by_family.setdefault((record.get("db_id"), sig), []).append(record)

    violations: list[str] = []
    for (db_id, sig), family in sorted(
        by_family.items(),
        key=lambda item: (str(item[0][0]), item[0][1]),
    ):
        if len(family) <= H11_SKELETON_FAMILY_MAX:
            continue
        sample = family[0]
        summary = sample.get("mql_skeleton_summary")
        if not isinstance(summary, str) or not summary:
            summary = mql_skeleton_summary(str(sample.get("MQL") or ""))
        record_ids = [record.get("record_id", "?") for record in family[:12]]
        violations.append(
            "[H11] MQL skeleton family too large for db_id {db!r}: "
            "{count} records > cap {cap}; skeleton={sig}; summary={summary}; "
            "sample_records={records}".format(
                db=db_id,
                count=len(family),
                cap=H11_SKELETON_FAMILY_MAX,
                sig=sig,
                summary=summary,
                records=record_ids,
            )
        )
    return violations


def _duplicate_canonical_nl_violations(records: list[dict[str, Any]]) -> list[str]:
    seen: dict[tuple[Any, str], dict[str, Any]] = {}
    violations: list[str] = []
    for record in records:
        nl_queries = record.get("nl_queries")
        if not isinstance(nl_queries, dict):
            continue
        canonical = _normalized_nl_text(nl_queries.get("canonical"))
        if not canonical:
            continue
        sig = _text_signature(canonical)
        key = (record.get("db_id"), sig)
        previous = seen.get(key)
        if previous is None:
            seen[key] = record
            continue
        violations.append(
            "[H12 r{rid}] duplicate canonical NL for db_id {db!r}; duplicates r{prev} "
            "(nl_signature={sig})".format(
                rid=record.get("record_id", "?"),
                db=record.get("db_id"),
                prev=previous.get("record_id", "?"),
                sig=sig,
            )
        )
    return violations


def _diversity_report(records: list[dict[str, Any]]) -> DiversityReport:
    mql_sigs: set[tuple[Any, str]] = set()
    skeleton_families: dict[tuple[Any, str], int] = {}
    canonical_sigs: set[tuple[Any, str]] = set()
    pair_sigs: set[tuple[Any, str, str]] = set()
    pair_sigs_by_db: dict[str, set[tuple[str, str]]] = {}

    for record in records:
        db_id = str(record.get("db_id") or "")
        mql = record.get("MQL")
        mql_sig = ""
        if isinstance(mql, str) and mql.strip():
            mql_sig = mql_signature(mql)
            mql_sigs.add((db_id, mql_sig))
            skeleton_sig = mql_skeleton_signature(mql)
            skeleton_key = (db_id, skeleton_sig)
            skeleton_families[skeleton_key] = skeleton_families.get(skeleton_key, 0) + 1

        nl_queries = record.get("nl_queries")
        canonical = (
            _normalized_nl_text(nl_queries.get("canonical"))
            if isinstance(nl_queries, dict)
            else ""
        )
        if canonical:
            nl_sig = _text_signature(canonical)
            canonical_sigs.add((db_id, nl_sig))
            if mql_sig:
                pair_sigs.add((db_id, nl_sig, mql_sig))
                pair_sigs_by_db.setdefault(db_id, set()).add((nl_sig, mql_sig))

    per_db = {db_id: len(sigs) for db_id, sigs in sorted(pair_sigs_by_db.items())}
    return DiversityReport(
        n_records=len(records),
        distinct_mql=len(mql_sigs),
        distinct_mql_skeletons=len(skeleton_families),
        distinct_canonical_nl=len(canonical_sigs),
        distinct_nl_mql_pairs=len(pair_sigs),
        max_mql_skeleton_family=max(skeleton_families.values(), default=0),
        distinct_nl_mql_pairs_per_db=per_db,
        min_distinct_nl_mql_pairs_per_db=min(per_db.values(), default=0),
    )


def _normalized_nl_text(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _text_signature(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _load_native_manifests(
    manifest_dir: Path,
    db_ids: list[str],
) -> dict[str, NativeFeatureManifest]:
    manifests: dict[str, NativeFeatureManifest] = {}
    for db_id in db_ids:
        path = manifest_dir / f"{db_id}.yaml"
        if path.exists():
            manifests[db_id] = load_native_feature_manifest(path)
    return manifests


def _load_native_provenance(provenance_dir: Path, db_ids: list[str]) -> dict[str, dict[str, Any]]:
    provenance: dict[str, dict[str, Any]] = {}
    for db_id in db_ids:
        path = provenance_dir / f"{db_id}.json"
        if path.exists():
            provenance[db_id] = json.loads(path.read_text(encoding="utf-8"))
    return provenance


def _validate_native_record(
    record: dict[str, Any],
    manifest: NativeFeatureManifest | None,
    provenance: dict[str, Any] | None,
    layout: ReleaseDatasetLayout,
) -> list[str]:
    rid = record.get("record_id", "?")
    db_id = str(record.get("db_id") or "")
    issues: list[str] = []
    feature_id = str(record.get("native_feature_id") or "")
    if not feature_id:
        return [f"[native r{rid}] missing native_feature_id"]
    if manifest is None:
        return [f"[native r{rid}] missing native feature manifest for db_id {db_id!r}"]
    feature = next((item for item in manifest.features if item.id == feature_id), None)
    if feature is None:
        issues.append(f"[native r{rid}] feature {feature_id!r} not found in manifest")
    else:
        if record.get("native_feature_type") != feature.type:
            issues.append(
                f"[native r{rid}] native_feature_type {record.get('native_feature_type')!r} "
                f"does not match manifest type {feature.type!r}"
            )
        verification = verify_native_record(record, manifest)
        if not verification.ok:
            issues.extend(f"[native r{rid}] {error}" for error in verification.errors)

    expected_recipe_ref = f"migration_recipe/{db_id}.yaml"
    if record.get("migration_recipe_ref") != expected_recipe_ref:
        issues.append(
            f"[native r{rid}] migration_recipe_ref must be {expected_recipe_ref!r}"
        )
    elif not (layout.migration_recipe_dir / f"{db_id}.yaml").exists():
        issues.append(f"[native r{rid}] migration recipe ref does not resolve")

    constructs = record.get("mongo_native_constructs") or []
    if not isinstance(constructs, list):
        issues.append(f"[native r{rid}] mongo_native_constructs must be a list")
    else:
        mql = str(record.get("MQL") or "")
        missing = [construct for construct in constructs if str(construct) not in mql]
        if missing:
            issues.append(
                f"[native r{rid}] claimed native constructs absent from MQL: {missing}"
            )

    provenance_refs = record.get("provenance_refs") or []
    if not isinstance(provenance_refs, list):
        issues.append(f"[native r{rid}] provenance_refs must be a list")
    elif provenance is None:
        issues.append(f"[native r{rid}] missing provenance artifact for db_id {db_id!r}")
    else:
        allowed = _native_provenance_refs(provenance)
        unresolved = [
            str(ref)
            for ref in provenance_refs
            if not _native_provenance_ref_resolves(str(ref), allowed)
        ]
        if unresolved:
            issues.append(f"[native r{rid}] unresolved provenance refs: {unresolved}")

    return issues


def _native_provenance_refs(provenance: dict[str, Any]) -> set[str]:
    refs: set[str] = set()
    entries = provenance.get("entries")
    if isinstance(entries, dict):
        refs.update(str(key) for key in entries)
        for value in entries.values():
            if isinstance(value, dict):
                refs.update(str(ref) for ref in value.get("source_columns") or [])
                refs.update(str(ref) for ref in value.get("source_tables") or [])
                refs.update(str(ref) for ref in value.get("provenance_refs") or [])
    return refs


def _native_provenance_ref_resolves(ref: str, allowed: set[str]) -> bool:
    if ref in allowed:
        return True
    if "." in ref:
        table, _ = ref.split(".", 1)
        return table in allowed
    return False


def _validate_native_artifacts(
    layout: ReleaseDatasetLayout,
    db_ids: list[str],
    manifests: dict[str, NativeFeatureManifest],
    provenance: dict[str, dict[str, Any]],
) -> list[str]:
    issues: list[str] = []
    for db_id in db_ids:
        recipe_path = layout.migration_recipe_dir / f"{db_id}.yaml"
        manifest_path = layout.native_feature_manifest_dir / f"{db_id}.yaml"
        provenance_path = layout.provenance_dir / f"{db_id}.json"
        if not recipe_path.exists():
            issues.append(f"[native] missing migration_recipe/{db_id}.yaml")
        if not manifest_path.exists():
            issues.append(f"[native] missing native_feature_manifest/{db_id}.yaml")
        if not provenance_path.exists():
            issues.append(f"[native] missing provenance/{db_id}.json")
        manifest = manifests.get(db_id)
        if manifest is not None and not manifest.features:
            issues.append(f"[native] manifest for {db_id} has no native features")
        payload = provenance.get(db_id)
        if payload is not None and not payload.get("conversion_code_ref"):
            issues.append(f"[native] provenance/{db_id}.json missing conversion_code_ref")
    return issues


def _native_coverage_violations(records: list[dict[str, Any]]) -> list[str]:
    native_records = [record for record in records if record.get("native_feature_id")]
    if not native_records:
        return ["[native] no native records represented"]
    feature_types = {str(record.get("native_feature_type") or "") for record in native_records}
    if not any(feature_types):
        return ["[native] no native feature type represented"]
    return []


def _validate_native_catalog_artifact(layout: ReleaseDatasetLayout, schemas_dir: Path) -> list[str]:
    """For native releases, validate only the shared catalog schema.

    Native ``mongodb_schema`` intentionally uses feature-manifest-oriented shapes that
    differ from the legacy proposal library schema.
    """
    try:
        import jsonschema
    except ImportError:
        return ["[schema] jsonschema is not installed; cannot validate release artifacts"]
    lib_path = schemas_dir / "library.schema.json"
    if not lib_path.exists():
        return []
    lib = json.loads(lib_path.read_text(encoding="utf-8"))
    catalog_path = layout.catalog_path
    if not catalog_path.exists():
        return ["[C4] missing bird_db_catalog.json"]
    schema = {"$schema": lib.get("$schema"), "$defs": lib.get("$defs", {}),
              "$ref": "#/$defs/bird_db_catalog"}
    validator = jsonschema.Draft202012Validator(schema)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    return [
        f"[schema bird_db_catalog.json] {err.message} at /{'/'.join(map(str, err.path))}"
        for err in validator.iter_errors(catalog)
    ]


def _validate_release_artifacts(
    layout: ReleaseDatasetLayout,
    db_ids: list[str],
    schemas_dir: Path,
    *,
    validate_mongodb_data: bool = True,
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
        catalog_path = layout.catalog_path
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
            artifacts = [
                (layout.mongodb_schema_dir / f"{db}.json", "mongodb_schema", "mongodb_schema"),
            ]
            if validate_mongodb_data:
                artifacts.append(
                    (layout.mongodb_data_dir / f"{db}.json", "mongodb_data", "mongodb_data")
                )
            for path, ref, label in artifacts:
                if path.exists():
                    db_issues += check(
                        lib_ref(ref),
                        json.loads(path.read_text(encoding="utf-8")),
                        f"{label}/{db}.json",
                    )
        if adr_schema is not None:
            path = layout.agent_design_rationale_dir / f"{db}.yaml"
            if path.exists():
                db_issues += check(
                    adr_schema,
                    yaml.safe_load(path.read_text(encoding="utf-8")),
                    f"agent_design_rationale/{db}.yaml",
                )
        return db_issues

    if db_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(db_ids), 8)) as pool:
            for db_issues in pool.map(check_db, db_ids):
                issues += db_issues
    return issues
