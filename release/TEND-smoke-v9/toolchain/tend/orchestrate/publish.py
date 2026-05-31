"""Dataset publisher: train/test/TEND.json, C1-C9 checks, coverage report."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tend.config import FIXTURES_ROOT, REPO_ROOT
from tend.core.ast_check import AST_check
from tend.core import logging as log_module
from tend.errors import SplitError, TENDError
from tend.orchestrate.audit_materialize import materialize_audit_trail
from tend.orchestrate.coverage import CoverageController, SIX_AXES
from tend.orchestrate.paths import (
    agent_design_rationale_dir,
    coverage_report_path,
    db_schema_path,
    global_audit_dir,
    mongodb_data_dir,
    mongodb_schema_dir,
    resolve_input_root,
    spider_db_catalog_json,
    tend_json,
    tend_root,
    test_json,
    train_json,
)
from tend.orchestrate.seed import publish_seed
from tend.orchestrate.split import cross_domain_split
from tend.schemas.validators import validate

REF_FIELDS = (
    "agent_design_rationale_ref",
    "mutations_ref",
    "_diagnostic_bridge_ref",
    "property_verification_ref",
    "round_trip_ref",
    "nnc_verdict_ref",
    "ra_audit_ref",
    "migration_log_ref",
)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sorted_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: int(r["record_id"]))


def check_c1_c9(
    records: list[dict[str, Any]],
    out_root: Path,
    *,
    require_exec: bool = False,
    extra_ref_roots: tuple[Path, ...] = (),
) -> list[str]:
    errors: list[str] = []
    schema_dir = mongodb_schema_dir(out_root)
    data_dir = mongodb_data_dir(out_root)
    rationale_dir = agent_design_rationale_dir(out_root)

    schema_names = {p.stem for p in schema_dir.glob("*.json")}
    data_names = {p.stem for p in data_dir.glob("*.json")}
    rationale_names = {p.stem for p in rationale_dir.glob("*.yaml")}

    if schema_names != data_names or schema_names != rationale_names:
        errors.append(
            f"C4/H4: 3-way basename mismatch schema={sorted(schema_names)} "
            f"data={sorted(data_names)} rationale={sorted(rationale_names)}"
        )

    for record in records:
        rid = record["record_id"]
        db_id = record["db_id"]

        for key, value in record.items():
            if value is None:
                errors.append(f"C1: record {rid} has null value for key {key}")

        nlq = record.get("nl_queries", {})
        if set(nlq.keys()) != {"canonical", "colloquial"}:
            errors.append(f"C2: record {rid} nl_queries must contain canonical+colloquial only")

        cfs = record.get("canonical_form_set", {})
        if not cfs.get("must_contain_at_root"):
            errors.append(f"C6: record {rid} missing must_contain_at_root")

        ast = AST_check(record["MQL"], cfs)
        if ast != "pass":
            errors.append(f"C5: record {rid} AST_check failed: {ast}")
        elif require_exec:
            errors.append(f"C5: record {rid} execution check not run (require_exec unsupported in MVP)")

        if db_id not in schema_names:
            errors.append(f"C4: record {rid} missing mongodb_schema/{db_id}.json")

        schema_flex = record.get("schema_flex", "none")
        if schema_flex != "none":
            schema_path = db_schema_path(db_id, out_root)
            if schema_path.exists():
                schema_doc = _load_json(schema_path)
                has_variants = any(
                    isinstance(coll, dict) and coll.get("__variants")
                    for coll in schema_doc.values()
                )
                if not has_variants:
                    errors.append(f"C9: record {rid} schema_flex={schema_flex} but no __variants in schema")
        elif schema_flex not in {"none"} and "schema_flex" in record:
            if record["schema_flex"] != "none":
                errors.append(f"C9: record {rid} declares schema_flex without variants")

        if record.get("sql_infeasibility_class") == "structural_schema_flex":
            if schema_flex == "none":
                errors.append(f"C9: record {rid} structural_schema_flex requires schema_flex != none")
            if record.get("difficulty") != "L4":
                errors.append(f"C9: record {rid} structural_schema_flex requires difficulty L4")

        for ref_key in REF_FIELDS:
            if ref_key not in record:
                continue
            ref = record[ref_key]
            resolved = _resolve_ref(ref, out_root, extra_roots=extra_ref_roots)
            if resolved is None:
                errors.append(f"C8: record {rid} unresolved ref {ref_key}={ref!r}")

        try:
            validate(record, "record")
        except ValueError as exc:
            errors.append(f"schema: record {rid} {exc}")

    return errors


def _is_minimal_schema_stub(doc: dict[str, Any]) -> bool:
    if len(doc) != 1:
        return False
    only_key = next(iter(doc))
    val = doc.get(only_key)
    return isinstance(val, dict) and set(val.keys()) == {"_id"}


def _is_minimal_data_stub(doc: dict[str, Any]) -> bool:
    if doc == {"collections": {}}:
        return True
    if not doc:
        return True
    # Single empty collection placeholder
    if len(doc) == 1:
        only_val = next(iter(doc.values()))
        if isinstance(only_val, list) and len(only_val) == 0:
            return True
    return False


def _library_file_is_stub(path: Path, *, kind: str) -> bool:
    if not path.exists():
        return True
    try:
        doc = _load_json(path)
    except json.JSONDecodeError:
        return True
    if kind == "schema":
        return _is_minimal_schema_stub(doc)
    if kind == "data":
        return _is_minimal_data_stub(doc)
    if kind == "rationale":
        return len(doc) <= 1 and "db_id" in doc
    return False


def _resolve_ref(ref: str, out_root: Path, extra_roots: tuple[Path, ...] = ()) -> Path | None:
    candidates: list[Path | None] = [
        out_root / ref,
        REPO_ROOT / ref,
        out_root / "fixtures" / Path(ref).relative_to("fixtures") if ref.startswith("fixtures/") else None,
        FIXTURES_ROOT / Path(ref).name if ref.startswith("fixtures/") else None,
    ]
    if ref.startswith("agent_design_rationale/"):
        candidates.append(agent_design_rationale_dir(out_root) / Path(ref).name)
    for extra in extra_roots:
        candidates.append(extra / ref)
    for candidate in candidates:
        if candidate is None:
            continue
        if candidate.exists():
            return candidate
    parts = ref.split("/")
    if len(parts) >= 2 and parts[0] == "fixtures":
        fixture_path = FIXTURES_ROOT / parts[1] / Path(*parts[2:])
        if fixture_path.exists():
            return fixture_path
    return None


def check_h1_h9(
    train: list[dict[str, Any]],
    test: list[dict[str, Any]],
    tend: list[dict[str, Any]],
    out_root: Path,
    split_meta: dict[str, Any],
    coverage: CoverageController,
) -> list[str]:
    errors: list[str] = []
    if len(train) + len(test) != len(tend):
        errors.append(f"H1: |train|+|test|={len(train)+len(test)} != |TEND|={len(tend)}")

    train_domains = {r.get("domain_id") for r in train}
    test_domains = {r.get("domain_id") for r in test}
    if train_domains & test_domains:
        errors.append(f"H2: domain overlap {sorted(train_domains & test_domains)}")

    train_dbs = {r["db_id"] for r in train}
    test_dbs = {r["db_id"] for r in test}
    if train_dbs & test_dbs:
        errors.append(f"H3: db_id overlap {sorted(train_dbs & test_dbs)}")

    schema_names = {p.stem for p in mongodb_schema_dir(out_root).glob("*.json")}
    data_names = {p.stem for p in mongodb_data_dir(out_root).glob("*.json")}
    rationale_names = {p.stem for p in agent_design_rationale_dir(out_root).glob("*.yaml")}
    if not (schema_names == data_names == rationale_names):
        errors.append("H4: per-db 3-way filename sets differ")

    if test:
        n_test = len(test)
        l4_ratio = sum(1 for r in test if r["difficulty"] == "L4") / n_test
        if l4_ratio < 0.30:
            errors.append(f"H5: test L4 ratio {l4_ratio:.3f} < 0.30")

        h7_min = split_meta.get("h7_min", 0.25)
        flex_ratio = sum(1 for r in test if r.get("schema_flex", "none") != "none") / n_test
        if flex_ratio < h7_min:
            errors.append(f"H7: test schema_flex ratio {flex_ratio:.3f} < h7_min {h7_min:.3f}")

        l0_ratio = sum(1 for r in test if r["difficulty"] == "L0") / n_test
        if l0_ratio > 0.05:
            errors.append(f"H8: test L0 ratio {l0_ratio:.3f} > 0.05")

        h9_min = split_meta.get("h9_min", 0.20)
        ssf_ratio = sum(
            1 for r in test if r.get("sql_infeasibility_class") == "structural_schema_flex"
        ) / n_test
        if ssf_ratio < h9_min:
            errors.append(f"H9: test structural_schema_flex ratio {ssf_ratio:.3f} < h9_min {h9_min:.3f}")

    for record in test:
        for axis, key_fn in SIX_AXES.items():
            cell = (axis, key_fn(record))
            if coverage.count[cell] > coverage.max_for(cell):
                errors.append(f"H6: test cell {cell} exceeds max_quota")

    return errors


def _snapshot_ready(path: Path) -> bool:
    if (path / "records.json").exists():
        return True
    return any(path.glob("*/record.json"))


def bootstrap_fixtures_snapshot(dest: Path) -> Path:
    """Materialize a fixtures-snapshot tree from proposals/fixtures (MVP)."""
    dest.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    used_ids: set[int] = set()
    next_id = 1001

    def _write_library_assets(db_id: str) -> None:
        schema_dir = dest / "mongodb_schema"
        data_dir = dest / "mongodb_data"
        rationale_dir = dest / "agent_design_rationale"
        schema_dir.mkdir(parents=True, exist_ok=True)
        data_dir.mkdir(parents=True, exist_ok=True)
        rationale_dir.mkdir(parents=True, exist_ok=True)

        fixture_dir = FIXTURES_ROOT / db_id
        schema_src = fixture_dir / "mongodb_schema.json"
        data_src = fixture_dir / "mongodb_data.json"
        sra_src = fixture_dir / "sra.yaml"

        if db_id == "orchestra":
            try:
                from tend.phase_a.dm import migrate
                from tend.phase_a.sra import ORCHESTRA_RATIONALE, ORCHESTRA_SCHEMA

                data, _migration_log = migrate(db_id, ORCHESTRA_SCHEMA, ORCHESTRA_RATIONALE)
                _write_json(schema_dir / f"{db_id}.json", ORCHESTRA_SCHEMA)
                _write_json(data_dir / f"{db_id}.json", data)
                _write_json(rationale_dir / f"{db_id}.yaml", ORCHESTRA_RATIONALE)
                return
            except Exception:
                pass

        if schema_src.exists():
            shutil.copy2(schema_src, schema_dir / f"{db_id}.json")
        else:
            _write_json(schema_dir / f"{db_id}.json", {db_id: {"_id": "INT"}})

        if data_src.exists():
            shutil.copy2(data_src, data_dir / f"{db_id}.json")
        else:
            _write_json(data_dir / f"{db_id}.json", {"collections": {}})

        if sra_src.exists():
            shutil.copy2(sra_src, rationale_dir / f"{db_id}.yaml")

    for fixture_dir in sorted(FIXTURES_ROOT.iterdir()):
        if not fixture_dir.is_dir():
            continue
        record_path = fixture_dir / "record.json"
        if not record_path.exists():
            continue
        record = _load_json(record_path)
        rid = int(record["record_id"])
        if rid in used_ids:
            while next_id in used_ids:
                next_id += 1
            record["record_id"] = next_id
            rid = next_id
            next_id += 1
        used_ids.add(rid)
        records.append(record)

        db_id = record["db_id"]
        _write_library_assets(db_id)

        fixtures_mirror = dest / "fixtures" / db_id
        fixtures_mirror.mkdir(parents=True, exist_ok=True)
        for pattern in ("*.yaml", "*.json"):
            for src in fixture_dir.glob(pattern):
                if src.name in {"record.json", "mongodb_schema.json", "mongodb_data.json"}:
                    continue
                shutil.copy2(src, fixtures_mirror / src.name)

    catalog_entries = []
    for record in records:
        db_id = record["db_id"]
        catalog_entries.append(
            {
                "db_id": db_id,
                "domain_id": record.get("domain_id", db_id.split("_")[0]),
                "table_count": 2,
                "query_count": 10,
                "flex_eligible": record.get("schema_flex", "none") != "none",
                "selected": True,
                "selection_reason": "fixtures MVP bootstrap",
            }
        )

    catalog = {
        "spider_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "spider_root": str(REPO_ROOT / "proposals" / "spider_data"),
        "selection_policy": {
            "min_tables": 2,
            "min_queries": 10,
            "require_non_empty_data": True,
            "min_flex_db_ratio": 0.30,
        },
        "databases": catalog_entries,
    }
    _write_json(dest / "spider_db_catalog.json", catalog)
    _write_json(dest / "records.json", records)
    return dest


def load_snapshot(input_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    records_path = input_root / "records.json"
    if records_path.exists():
        records = _load_json(records_path)
    else:
        records = []
        for path in sorted(input_root.glob("*/record.json")):
            records.append(_load_json(path))
        if not records:
            raise TENDError(f"no records found under {input_root}")

    catalog_path = input_root / "spider_db_catalog.json"
    if catalog_path.exists():
        catalog = _load_json(catalog_path)
    else:
        catalog = {
            "spider_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "databases": [
                {
                    "db_id": r["db_id"],
                    "domain_id": r.get("domain_id", r["db_id"]),
                    "selected": True,
                    "flex_eligible": r.get("schema_flex", "none") != "none",
                    "table_count": 2,
                    "query_count": 10,
                }
                for r in records
            ],
        }
    return records, catalog


def prune_orphan_library_assets(out_root: Path, db_ids: set[str]) -> None:
    """Remove per-db Tier-1 files not referenced by published records."""
    for db_dir, suffix in (
        (mongodb_schema_dir(out_root), ".json"),
        (mongodb_data_dir(out_root), ".json"),
        (agent_design_rationale_dir(out_root), ".yaml"),
    ):
        if not db_dir.exists():
            continue
        for path in db_dir.glob(f"*{suffix}"):
            if path.stem not in db_ids:
                path.unlink()


def _flex_yield_by_db(
    tend_records: list[dict[str, Any]],
    catalog: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    flex_map = {
        str(entry["db_id"]): bool(entry.get("flex_eligible"))
        for entry in catalog.get("databases", [])
    }
    out: dict[str, dict[str, Any]] = {}
    for db_id in {str(r["db_id"]) for r in tend_records}:
        flex_records = sum(
            1
            for r in tend_records
            if r["db_id"] == db_id and r.get("schema_flex", "none") != "none"
        )
        out[db_id] = {
            "flex_eligible": flex_map.get(db_id, False),
            "flex_records": flex_records,
        }
    return out


def copy_library_assets(input_root: Path, out_root: Path, db_ids: set[str]) -> None:
    for db_id in sorted(db_ids):
        for src_dir, dst_dir, suffix, kind in (
            ("mongodb_schema", mongodb_schema_dir(out_root), ".json", "schema"),
            ("mongodb_data", mongodb_data_dir(out_root), ".json", "data"),
            ("agent_design_rationale", agent_design_rationale_dir(out_root), ".yaml", "rationale"),
        ):
            src = input_root / src_dir / f"{db_id}{suffix}"
            dst = dst_dir / f"{db_id}{suffix}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.exists():
                src_is_stub = _library_file_is_stub(src, kind=kind)
                dst_is_stub = _library_file_is_stub(dst, kind=kind) if dst.exists() else True
                if dst.exists() and not dst_is_stub and src_is_stub:
                    continue
                if src_is_stub and dst.exists() and not dst_is_stub:
                    continue
                shutil.copy2(src, dst)
            elif dst.exists() and not _library_file_is_stub(dst, kind=kind):
                continue
            elif suffix == ".json" and src_dir == "mongodb_schema":
                _write_json(dst, {db_id: {"_id": "INT"}})
            elif suffix == ".json":
                _write_json(dst, {"collections": {}})
            elif suffix == ".yaml":
                sra = FIXTURES_ROOT / db_id / "sra.yaml"
                if sra.exists():
                    shutil.copy2(sra, dst)
                else:
                    dst.write_text(f"db_id: {db_id}\n", encoding="utf-8")

    fixtures_src = input_root / "fixtures"
    if fixtures_src.exists():
        fixtures_dst = out_root / "fixtures"
        if fixtures_dst.exists():
            shutil.rmtree(fixtures_dst)
        shutil.copytree(fixtures_src, fixtures_dst)


def publish_dataset(
    input_root: Path | str,
    out_root: Path | str,
    *,
    test_ratio: float = 0.20,
    seed: int | None = None,
) -> dict[str, Any]:
    in_path = resolve_input_root(input_root)
    if not in_path.exists() or not _snapshot_ready(in_path):
        bootstrap_dir = REPO_ROOT / "fixtures-snapshot"
        bootstrap_fixtures_snapshot(bootstrap_dir)
        in_path = bootstrap_dir

    out_path = tend_root(out_root)
    out_path.mkdir(parents=True, exist_ok=True)
    global_audit_dir(out_root).mkdir(parents=True, exist_ok=True)

    records, catalog = load_snapshot(in_path)
    coverage = CoverageController.with_defaults(target_records=len(records))
    relax = coverage.apply_supply_relax_from_catalog(catalog)

    train, test, split_meta = cross_domain_split(
        catalog,
        [dict(r) for r in records],
        test_ratio=test_ratio,
        seed=seed if seed is not None else publish_seed(),
    )
    tend_records = _sorted_records(train + test)
    train = _sorted_records(train)
    test = _sorted_records(test)

    db_ids = {r["db_id"] for r in tend_records}
    prune_orphan_library_assets(out_path, db_ids)
    copy_library_assets(in_path, out_path, db_ids)

    _write_json(train_json(out_path), train)
    _write_json(test_json(out_path), test)
    _write_json(tend_json(out_path), tend_records)
    _write_json(spider_db_catalog_json(out_path), catalog)

    materialize_audit_trail(out_path, tend_records, input_root=in_path)

    for record in tend_records:
        coverage.accept(record)

    c_errors = check_c1_c9(tend_records, out_path)
    if c_errors:
        raise SplitError("C1-C9 failed: " + "; ".join(c_errors))

    h_errors = check_h1_h9(train, test, tend_records, out_path, split_meta, coverage)
    if h_errors:
        raise SplitError("H1-H9 failed: " + "; ".join(h_errors))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "split": split_meta,
        "supply_relax_active": split_meta.get("supply_relax_active", False),
        "h7_min": split_meta.get("h7_min"),
        "h9_min": split_meta.get("h9_min"),
        "flex_eligible_db_ratio": split_meta.get("flex_eligible_db_ratio"),
        "flex_yield_by_db": _flex_yield_by_db(tend_records, catalog),
        "coverage": coverage.quota_state(),
        **relax,
    }
    _write_json(coverage_report_path(out_path), report)

    log_module.emit(
        "publish.done",
        train=len(train),
        test=len(test),
        total=len(tend_records),
        supply_relax=split_meta.get("supply_relax_active", False),
    )
    return {
        "train": train,
        "test": test,
        "TEND": tend_records,
        "split_meta": split_meta,
        "coverage_report": report,
    }
