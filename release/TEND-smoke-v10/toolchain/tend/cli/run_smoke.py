"""Small-scale real-LLM smoke sweep: ~10 dbs × N records, full Phase A+B+publish.

Purpose:
- Validate that the real LLM pipeline (no stubs/fixtures) survives end-to-end on a
  cross-section of Spider dbs before scaling up to Full-C (~17k records).
- Surface issues like rate limits, cached payload shapes, temperature constraints,
  transcript continuity, and witness/snapshot coverage gaps.

Selection: 6 fixture dbs (default) + N auto-picked Spider dbs from the catalog.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tend.config import FIXTURES_ROOT, REPO_ROOT, assert_pilot_llm_live
from tend.core import logging as log_module
from tend.core.io import load_json
from tend.orchestrate.coverage import CoverageController
from tend.orchestrate.audit_materialize import materialize_audit_trail
from tend.orchestrate.record_metadata import derive_record_axes
from tend.orchestrate.paths import (
    DEFAULT_OUT_ROOT,
    coverage_report_path,
    global_audit_dir,
    test_json,
)
from tend.orchestrate.publish import publish_dataset
from tend.phase_a.catalog import FIXTURE_DB_IDS, _flex_eligible, select_spider_dbs

FIXTURE_DEFAULT_RECORD_IDS = {
    "orchestra": 1001,
    "concert_singer": 1002,
    "cre_doc_tracking_db": 1003,
    "flight_2": 1004,
    "student_assessment": 1005,
    "world_1": 1006,
}


_L4_PATTERNS = ("window_facet_filter", "set_window", "polymorphic_dispatch", "dynamic_key_aggregation")
_FLEX_PATTERNS = ("polymorphic_dispatch", "attribute_bag_unfold", "schema_version_fallback")


def _pick_plan_pattern(
    db_idx: int,
    rec_idx: int,
    records_per_db: int,
    *,
    flex_eligible: bool = False,
) -> str | None:
    """Diversify patterns to meet H5 (>=30% L4) and H7 (>=15% flex).

    Distribution per db (5 records): 2 L4 patterns + 1 flex pattern + 2 default.
    Flex patterns are only scheduled on flex_eligible dbs so C9 __variants holds.
    """
    if db_idx == 0:
        return None  # orchestra uses its own window_facet_filter fixture
    slot = rec_idx % records_per_db
    if slot < 2:
        return _L4_PATTERNS[db_idx % len(_L4_PATTERNS)]
    if slot == 2 and flex_eligible:
        return _FLEX_PATTERNS[db_idx % len(_FLEX_PATTERNS)]
    if slot == 2:
        return _L4_PATTERNS[(db_idx + 1) % len(_L4_PATTERNS)]
    return None


def _difficulty_from_canonical_form(cfs: dict[str, Any]) -> str:
    must_contain = set(cfs.get("must_contain", []))
    if must_contain & {"$setWindowFields", "$facet"}:
        return "L4"
    if must_contain & {"$lookup"}:
        return "L3"
    if must_contain & {"$unwind", "$group"}:
        return "L2"
    return "L1"


def _infeasibility_from_canonical_form(cfs: dict[str, Any]) -> str:
    must_contain = set(cfs.get("must_contain", []))
    if must_contain & {"$setWindowFields", "$facet"}:
        return "structural_pipeline"
    if "$objectToArray" in must_contain or "$function" in must_contain:
        return "structural_schema_flex"
    if must_contain & {"$unwind", "$group"}:
        return "semantic"
    return "feasible"


def select_smoke_dbs(extra_count: int = 4) -> list[str]:
    """6 fixtures + up to `extra_count` newly-selected Spider dbs.

    Extras are prioritised by dynamic flex_eligible (so the smoke run actually
    exercises schema-flex H1–H4 paths), then by table count as a tie-breaker.
    """
    fixtures = list(FIXTURE_DB_IDS)
    if extra_count <= 0:
        return fixtures
    catalog_result = select_spider_dbs(
        auto_select_qualifying=True,
        max_selected=None,
    )
    candidates: list[dict[str, Any]] = []
    for entry in catalog_result["catalog"]["databases"]:
        if not entry.get("selected"):
            continue
        if entry["db_id"] in fixtures:
            continue
        candidates.append(entry)
    candidates.sort(
        key=lambda e: (
            0 if e.get("flex_eligible") else 1,
            -int(e.get("table_count", 0)),
        )
    )
    extras = [c["db_id"] for c in candidates[:extra_count]]
    return fixtures + extras


def _schema_flex_eligible(schema_path: Path) -> bool:
    """True when Phase-A schema declares __variants (required for C9 publish)."""
    if not schema_path.is_file():
        return False
    schema = load_json(schema_path)
    if not isinstance(schema, dict):
        return False
    return any(
        isinstance(coll, dict) and bool(coll.get("__variants"))
        for coll in schema.values()
    )


def _load_phase_a_context(
    db_id: str, phase_a_root: Path, rationale_path: Path, *, schema_path: Path | None = None
) -> dict[str, Any]:
    wp_candidates = [
        phase_a_root.parent / "audit" / db_id / "wp_output.yaml",
        phase_a_root / "audit" / db_id / "wp_output.yaml",
    ]
    scenario_summary = ""
    for wp_audit in wp_candidates:
        if wp_audit.exists():
            wp_data = yaml.safe_load(wp_audit.read_text(encoding="utf-8"))
            if isinstance(wp_data, dict):
                scenario_summary = wp_data.get("scenario_summary", "")
            break

    schema_pattern = "embed"
    if rationale_path.exists():
        rat = yaml.safe_load(rationale_path.read_text(encoding="utf-8"))
        if isinstance(rat, dict):
            patterns = rat.get("patterns_applied") or ["embed"]
            if patterns:
                schema_pattern = patterns[0]

    # build_phase_a writes audit to `out_root.parent / "audit" / db_id /` (i.e.
    # repo/out/audit/<db_id>/), NOT phase_a_root/audit. Check both.
    candidate_paths = [
        phase_a_root.parent / "audit" / db_id / "migration_log.json",
        phase_a_root / "audit" / db_id / "migration_log.json",
    ]
    world_sig = "sha256:" + "0" * 64
    for migration_log_path in candidate_paths:
        if migration_log_path.exists():
            mlog = load_json(migration_log_path)
            if isinstance(mlog, dict) and mlog.get("world_signature"):
                world_sig = mlog["world_signature"]
            break

    return {
        "scenario_summary": scenario_summary,
        "schema_pattern": schema_pattern,
        "world_signature": world_sig,
        "flex_eligible": _schema_flex_eligible(
            schema_path or (phase_a_root / "mongodb_schema" / f"{db_id}.json")
        ),
    }


def _copy_library_assets(
    db_id: str, phase_a_paths: dict[str, Path], snapshot: Path
) -> None:
    (snapshot / "mongodb_schema").mkdir(parents=True, exist_ok=True)
    (snapshot / "mongodb_data").mkdir(parents=True, exist_ok=True)
    (snapshot / "agent_design_rationale").mkdir(parents=True, exist_ok=True)
    (snapshot / "fixtures").mkdir(parents=True, exist_ok=True)

    schema_src = Path(phase_a_paths["schema"])
    data_src = Path(phase_a_paths["data"])
    rationale_src = Path(phase_a_paths["rationale"])
    if schema_src.exists():
        shutil.copy2(schema_src, snapshot / "mongodb_schema" / f"{db_id}.json")
    if data_src.exists():
        shutil.copy2(data_src, snapshot / "mongodb_data" / f"{db_id}.json")
    if rationale_src.exists():
        shutil.copy2(rationale_src, snapshot / "agent_design_rationale" / f"{db_id}.yaml")

    fdir = FIXTURES_ROOT / db_id
    if fdir.exists():
        fixtures_mirror = snapshot / "fixtures" / db_id
        fixtures_mirror.mkdir(parents=True, exist_ok=True)
        for src in fdir.glob("*"):
            if not src.is_file():
                continue
            if src.name in {"mongodb_schema.json", "mongodb_data.json"}:
                continue
            shutil.copy2(src, fixtures_mirror / src.name)


REF_PATH_FIELDS = (
    "property_verification_ref",
    "round_trip_ref",
    "_diagnostic_bridge_ref",
    "mutations_ref",
)


def _sync_audit_dir_for_record_id(
    db_id: str,
    old_rid: int,
    new_rid: int,
    *,
    snapshot: Path | None = None,
) -> None:
    if old_rid == new_rid:
        return
    roots = [REPO_ROOT / "out" / "audit"]
    if snapshot is not None:
        roots.append(snapshot / "audit")
    for root in roots:
        src = root / db_id / str(old_rid)
        dst = root / db_id / str(new_rid)
        if not src.is_dir():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)


def _normalize_record_audit_refs(rec: dict[str, Any], snapshot: Path) -> None:
    """Align audit/*_ref paths with the published record_id."""
    rid = int(rec["record_id"])
    db_id = str(rec["db_id"])
    prefix = f"audit/{db_id}/"
    for key in REF_PATH_FIELDS:
        ref = rec.get(key)
        if not isinstance(ref, str) or not ref.startswith(prefix):
            continue
        parts = ref.split("/")
        if len(parts) < 4:
            continue
        try:
            ref_rid = int(parts[2])
        except ValueError:
            continue
        if ref_rid == rid:
            continue
        repo = REPO_ROOT / "out" / "audit" / db_id
        src = repo / str(ref_rid)
        dst = repo / str(rid)
        if src.is_dir() and not (dst / "pv.yaml").exists():
            _sync_audit_dir_for_record_id(db_id, ref_rid, rid, snapshot=snapshot)
        parts[2] = str(rid)
        rec[key] = "/".join(parts)


def _finalize_unique_record_ids(records: list[dict[str, Any]], snapshot: Path) -> None:
    used_ids: set[int] = set()
    for rec in records:
        old_rid = int(rec["record_id"])
        rid = old_rid
        while rid in used_ids:
            rid += 1
        if rid != old_rid:
            db_id = str(rec["db_id"])
            old_token = f"/{old_rid}/"
            new_token = f"/{rid}/"
            for key in REF_PATH_FIELDS:
                ref = rec.get(key)
                if isinstance(ref, str) and old_token in ref:
                    rec[key] = ref.replace(old_token, new_token)
            _sync_audit_dir_for_record_id(db_id, old_rid, rid, snapshot=snapshot)
        rec["record_id"] = rid
        used_ids.add(rid)
        _normalize_record_audit_refs(rec, snapshot)


def _write_audit_refs(record: dict[str, Any], snapshot: Path) -> None:
    """Ensure audit ref targets exist under the publish snapshot."""
    repo_audit = REPO_ROOT / "out" / "audit"
    materialize_audit_trail(snapshot, [record], input_root=repo_audit.parent)

    ref_keys = [
        "property_verification_ref",
        "round_trip_ref",
        "_diagnostic_bridge_ref",
        "mutations_ref",
    ]
    for key in ref_keys:
        ref_path = record.get(key)
        if not ref_path or not ref_path.startswith("audit/"):
            continue
        dest = snapshot / ref_path
        if dest.exists():
            continue
        src = repo_audit / ref_path.removeprefix("audit/")
        if src.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            yaml.dump({"stub": True, "record_id": record.get("record_id"), "db_id": record.get("db_id")}),
            encoding="utf-8",
        )


def _build_record_from_synth_valid(
    *,
    db_id: str,
    record_id: int,
    synth: dict[str, Any],
    valid: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    ms = synth["ms"]
    cfs = ms["canonical_form_set"]
    qp = synth["qps"]["query_plan"]
    nnc_verdict = valid["nnc"]["nnc_verdict"]
    axes = derive_record_axes(ms["MQL"], qp, context)

    nnc_difficulty = nnc_verdict.get("difficulty") or _difficulty_from_canonical_form(cfs)
    qp_difficulty = qp.get("target_difficulty", "L1")
    difficulty = max(nnc_difficulty, qp_difficulty)

    qp_flex = qp.get("schema_flex_mode", "none")
    if qp_flex != "none" and not context.get("flex_eligible", False):
        qp_flex = "none"
    nnc_infeasibility = nnc_verdict.get("sql_infeasibility_class") or _infeasibility_from_canonical_form(cfs)
    infeasibility = "structural_schema_flex" if qp_flex != "none" else nnc_infeasibility
    if qp_flex != "none":
        difficulty = max(difficulty, qp_difficulty, "L4")

    record = {
        "record_id": record_id,
        "db_id": db_id,
        "nl_queries": valid["nlp"]["nl_queries"],
        "MQL": ms["MQL"],
        "canonical_form_set": cfs,
        "difficulty": difficulty,
        "sql_infeasibility_class": infeasibility,
        "schema_flex": qp_flex,
        "shape_policy": qp.get("shape_policy", "reshape"),
        "world_signature": context["world_signature"],
        "domain_id": db_id.split("_")[0],
        **axes,
        "agent_design_rationale_ref": f"agent_design_rationale/{db_id}.yaml",
        "property_verification_ref": f"audit/{db_id}/{record_id}/pv.yaml",
        "round_trip_ref": f"audit/{db_id}/{record_id}/rtv.yaml",
        "_diagnostic_bridge_ref": f"audit/{db_id}/{record_id}/nnc.yaml",
    }
    mut_src = REPO_ROOT / "out" / "audit" / db_id / str(record_id) / "mutations.json"
    if mut_src.is_file():
        record["mutations_ref"] = f"audit/{db_id}/{record_id}/mutations.json"
    return record


RA_REFLOW_MAX_ROUNDS = 2


@dataclass(frozen=True)
class _SmokeRecordJob:
    db_idx: int
    db_id: str
    record_id: int
    plan_pattern: str | None
    context: dict[str, Any]
    phase_a_root: Path
    llm_stub: bool
    prefer_fixture: bool


def _smoke_run_phase_a(
    db_id: str,
    *,
    phase_a_root: Path,
    smoke_snapshot: Path,
    llm_stub: bool,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    from tend.cli.build_phase_a import build_phase_a

    log_module.bind(db_id=db_id, stage="build", agent="phase_a")
    failures: list[dict[str, Any]] = []
    log_module.emit("smoke.phase_a.start", db_id=db_id)
    try:
        paths = build_phase_a(db_id, phase_a_root, seed=42, llm_stub=llm_stub)
    except Exception as exc:  # noqa: BLE001
        log_module.emit(
            "smoke.phase_a.fail",
            db_id=db_id,
            error=str(exc),
            level="ERROR",
        )
        failures.append({"db_id": db_id, "stage": "phase_a", "error": str(exc)})
        return None, failures
    _copy_library_assets(db_id, paths, smoke_snapshot)
    context = _load_phase_a_context(
        db_id, phase_a_root, Path(paths["rationale"]), schema_path=Path(paths["schema"])
    )
    log_module.emit(
        "smoke.phase_a.done",
        db_id=db_id,
        world_signature=context["world_signature"],
    )
    return context, failures


def _smoke_run_record(job: _SmokeRecordJob) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    log_module.bind(
        db_id=job.db_id,
        record_id=job.record_id,
        stage="build",
        agent="phase_a",
    )
    record = _run_one_record(
        db_id=job.db_id,
        record_id=job.record_id,
        phase_a_root=job.phase_a_root,
        context=job.context,
        llm_stub=job.llm_stub,
        prefer_fixture=job.prefer_fixture,
        plan_pattern=job.plan_pattern,
    )
    if record is None:
        return None, {"db_id": job.db_id, "record_id": job.record_id, "stage": "phase_b"}
    status = record.pop("_phase_b_status", "fail")
    ra_pass = record.pop("_ra_pass", False)
    if status != "ok":
        log_module.emit(
            "smoke.record.rejected",
            db_id=job.db_id,
            record_id=job.record_id,
            status=status,
            ra_pass=ra_pass,
        )
        return None, {
            "db_id": job.db_id,
            "record_id": job.record_id,
            "stage": "phase_b.valid",
            "status": status,
            "ra_pass": ra_pass,
        }
    return record, None


def _run_one_record(
    *,
    db_id: str,
    record_id: int,
    phase_a_root: Path,
    context: dict[str, Any],
    llm_stub: bool = False,
    prefer_fixture: bool = False,
    ra_reflow_rounds: int = RA_REFLOW_MAX_ROUNDS,
    plan_pattern: str | None = None,
) -> dict[str, Any] | None:
    from tend.cli.build_phase_b_synth import run_phase_b_synth
    from tend.cli.build_phase_b_valid import run_phase_b_valid

    log_module.emit("smoke.phase_b_synth.start", db_id=db_id, record_id=record_id)
    try:
        synth = run_phase_b_synth(
            db_id,
            out_root=phase_a_root,
            record_id=record_id,
            llm_stub=llm_stub,
            plan_pattern=plan_pattern,
        )
    except Exception as exc:  # noqa: BLE001
        log_module.emit(
            "smoke.phase_b_synth.fail",
            db_id=db_id,
            record_id=record_id,
            error=str(exc),
            level="ERROR",
        )
        return None

    cfs = synth["ms"]["canonical_form_set"]
    qp = synth["qps"]["query_plan"]
    qp_difficulty = qp.get("target_difficulty", "L1")
    qp_flex = qp.get("schema_flex_mode", "none")
    if qp_flex != "none" and not context.get("flex_eligible", False):
        qp_flex = "none"
    cfs_difficulty = _difficulty_from_canonical_form(cfs)
    cfs_infeasibility = _infeasibility_from_canonical_form(cfs)
    difficulty = qp_difficulty if qp_difficulty > cfs_difficulty else cfs_difficulty
    if qp_flex != "none":
        infeasibility = "structural_schema_flex"
        difficulty = max(difficulty, qp_difficulty, "L4")
    else:
        infeasibility = cfs_infeasibility
    seed_record = {
        "record_id": record_id,
        "db_id": db_id,
        "nl_queries": {"canonical": "(pending NLP)", "colloquial": "(pending NLP)"},
        "MQL": synth["ms"]["MQL"],
        "canonical_form_set": cfs,
        "difficulty": difficulty,
        "sql_infeasibility_class": infeasibility,
        "schema_flex": qp_flex,
        "shape_policy": qp.get("shape_policy", "reshape"),
        "world_signature": context["world_signature"],
        "schema_pattern": context["schema_pattern"],
    }

    bundle = {
        "record": seed_record,
        "query_plan": {**synth["qps"]["query_plan"], "db_id": db_id, "record_id": record_id},
        "scenario_summary": context["scenario_summary"],
        "schema_pattern": context["schema_pattern"],
        "world_signature": context["world_signature"],
    }

    augmented_snapshot: dict[str, Any] | None = None
    augmented_world_sig: str | None = None
    valid: dict[str, Any] | None = None

    for round_idx in range(ra_reflow_rounds + 1):
        snapshot_arg = augmented_snapshot
        if augmented_world_sig:
            bundle["record"]["world_signature"] = augmented_world_sig
            bundle["world_signature"] = augmented_world_sig

        log_module.emit(
            "smoke.phase_b_valid.start",
            db_id=db_id,
            record_id=record_id,
            ra_round=round_idx,
        )
        try:
            valid = run_phase_b_valid(
                db_id,
                record_id,
                out_root=phase_a_root,
                snapshot=snapshot_arg,
                bundle=bundle,
                prefer_fixture=prefer_fixture,
            )
        except Exception as exc:  # noqa: BLE001
            log_module.emit(
                "smoke.phase_b_valid.fail",
                db_id=db_id,
                record_id=record_id,
                ra_round=round_idx,
                error=str(exc),
                level="ERROR",
            )
            return None

        ra_payload = valid.get("ra", {}).get("ra_audit", {})
        ra_pass = bool(ra_payload.get("pass"))
        pending = bool(ra_payload.get("pending_augment"))

        if ra_pass or not pending:
            break

        next_snapshot = valid.get("ra", {}).get("snapshot")
        next_world_sig = valid.get("ra", {}).get("world_signature")
        if not isinstance(next_snapshot, dict):
            log_module.emit(
                "smoke.ra_reflow.no_snapshot",
                db_id=db_id,
                record_id=record_id,
                ra_round=round_idx,
            )
            break
        if round_idx >= ra_reflow_rounds:
            log_module.emit(
                "smoke.ra_reflow.budget_exhausted",
                db_id=db_id,
                record_id=record_id,
                ra_round=round_idx,
            )
            break

        augmented_snapshot = next_snapshot
        if isinstance(next_world_sig, str) and next_world_sig:
            augmented_world_sig = next_world_sig
        log_module.emit(
            "smoke.ra_reflow.retry",
            db_id=db_id,
            record_id=record_id,
            next_round=round_idx + 1,
        )

    if valid is None:
        return None

    if augmented_world_sig:
        context = {**context, "world_signature": augmented_world_sig}

    record = _build_record_from_synth_valid(
        db_id=db_id,
        record_id=record_id,
        synth=synth,
        valid=valid,
        context=context,
    )
    record["_phase_b_status"] = valid.get("status", "fail")
    record["_ra_pass"] = bool(valid.get("ra", {}).get("ra_audit", {}).get("pass"))
    log_module.emit(
        "smoke.record.done",
        db_id=db_id,
        record_id=record_id,
        status=valid.get("status"),
        ra_pass=valid.get("ra", {}).get("ra_audit", {}).get("pass"),
    )
    return record


def run_smoke(
    out_root: Path,
    *,
    records_per_db: int = 5,
    db_ids: list[str] | None = None,
    test_ratio: float = 0.20,
    skip_llm_check: bool = False,
    extra_db_count: int = 4,
    llm_stub: bool | None = None,
    skip_publish: bool = False,
    with_evaluate: bool = True,
    with_disclosure: bool = True,
    release_tag: str = "smoke-v0",
    workers: int | None = None,
) -> dict[str, Any]:
    if not skip_llm_check:
        assert_pilot_llm_live()

    from tend.config import default_llm_stub

    if llm_stub is None:
        llm_stub = default_llm_stub()
    prefer_fixture = llm_stub
    if workers is None:
        workers = max(1, int(os.getenv("TEND_LLM_WORKERS", "128")))

    run_dir = log_module.init_run_dir()
    log_module.configure_logging(quiet=os.getenv("TEND_QUIET") == "1")
    db_ids = db_ids or select_smoke_dbs(extra_count=extra_db_count)
    log_module.emit(
        "smoke.start",
        db_count=len(db_ids),
        records_per_db=records_per_db,
        db_ids=db_ids,
        llm_stub=llm_stub,
        workers=workers,
    )

    phase_a_root = REPO_ROOT / "out" / "TEND"
    phase_a_root.mkdir(parents=True, exist_ok=True)

    smoke_snapshot = REPO_ROOT / "fixtures-snapshot" / "smoke-publish"
    smoke_snapshot.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []

    target_total = max(1, len(db_ids) * records_per_db)
    coverage = CoverageController.with_defaults(target_records=target_total)
    contexts: dict[str, dict[str, Any]] = {}
    records_lock = threading.Lock()

    def _accept_record(record: dict[str, Any]) -> None:
        with records_lock:
            coverage.accept(record)
            records.append(record)
            _write_audit_refs(record, smoke_snapshot)

    if workers <= 1:
        for db_idx, db_id in enumerate(db_ids):
            context, phase_failures = _smoke_run_phase_a(
                db_id,
                phase_a_root=phase_a_root,
                smoke_snapshot=smoke_snapshot,
                llm_stub=llm_stub,
            )
            failures.extend(phase_failures)
            if context is None:
                continue
            contexts[db_id] = context
            base_rid = FIXTURE_DEFAULT_RECORD_IDS.get(db_id, 30000 + db_idx * 100)
            for rec_idx in range(records_per_db):
                job = _SmokeRecordJob(
                    db_idx=db_idx,
                    db_id=db_id,
                    record_id=base_rid + rec_idx,
                    plan_pattern=_pick_plan_pattern(
                        db_idx,
                        rec_idx,
                        records_per_db,
                        flex_eligible=context.get("flex_eligible", False),
                    ),
                    context=context,
                    phase_a_root=phase_a_root,
                    llm_stub=llm_stub,
                    prefer_fixture=prefer_fixture,
                )
                record, fail = _smoke_run_record(job)
                if fail:
                    failures.append(fail)
                    continue
                if record:
                    _accept_record(record)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            phase_futures = {
                pool.submit(
                    _smoke_run_phase_a,
                    db_id,
                    phase_a_root=phase_a_root,
                    smoke_snapshot=smoke_snapshot,
                    llm_stub=llm_stub,
                ): db_id
                for db_id in db_ids
            }
            for future in as_completed(phase_futures):
                db_id = phase_futures[future]
                context, phase_failures = future.result()
                failures.extend(phase_failures)
                if context is not None:
                    contexts[db_id] = context

        record_jobs: list[_SmokeRecordJob] = []
        for db_idx, db_id in enumerate(db_ids):
            context = contexts.get(db_id)
            if context is None:
                continue
            base_rid = FIXTURE_DEFAULT_RECORD_IDS.get(db_id, 30000 + db_idx * 100)
            for rec_idx in range(records_per_db):
                record_jobs.append(
                    _SmokeRecordJob(
                        db_idx=db_idx,
                        db_id=db_id,
                        record_id=base_rid + rec_idx,
                        plan_pattern=_pick_plan_pattern(
                            db_idx,
                            rec_idx,
                            records_per_db,
                            flex_eligible=context.get("flex_eligible", False),
                        ),
                        context=context,
                        phase_a_root=phase_a_root,
                        llm_stub=llm_stub,
                        prefer_fixture=prefer_fixture,
                    )
                )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            record_futures = {pool.submit(_smoke_run_record, job): job for job in record_jobs}
            for future in as_completed(record_futures):
                record, fail = future.result()
                if fail:
                    failures.append(fail)
                elif record:
                    _accept_record(record)

    _finalize_unique_record_ids(records, smoke_snapshot)
    materialize_audit_trail(smoke_snapshot, records, input_root=REPO_ROOT / "out")

    (smoke_snapshot / "records.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    catalog_result = select_spider_dbs(force_selected=db_ids)
    catalog = catalog_result["catalog"]
    id_set = set(db_ids)
    for entry in catalog["databases"]:
        if entry["db_id"] in id_set:
            entry["selected"] = True
            if not entry.get("selection_reason"):
                entry["selection_reason"] = "release run (smoke/full build)"
    selected_entries = [entry for entry in catalog["databases"] if entry.get("selected")]
    catalog["selected_flex_ratio"] = sum(1 for e in selected_entries if e.get("flex_eligible")) / max(
        len(selected_entries), 1
    )
    (smoke_snapshot / "spider_db_catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)

    coverage_report = {
        "target_records": target_total,
        "produced_records": len(records),
        "supply_relax_active": False,
        "quota_state": coverage.quota_state(),
    }
    global_audit = global_audit_dir(out_root)
    global_audit.mkdir(parents=True, exist_ok=True)
    coverage_report_path(out_root).write_text(
        json.dumps(coverage_report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    result: dict[str, Any] | None = None
    publish_error: str | None = None
    if not skip_publish:
        try:
            result = publish_dataset(smoke_snapshot, out_root, test_ratio=test_ratio)
        except Exception as exc:  # noqa: BLE001
            publish_error = str(exc)
            log_module.emit(
                "smoke.publish.fail",
                error=publish_error,
                level="ERROR",
            )

    tend_records = result["TEND"] if result is not None else records

    eval_dir: Path | None = None
    eval_error: str | None = None
    disclosure_summary: dict[str, Any] | None = None
    disclosure_error: str | None = None

    can_evaluate = (
        with_evaluate
        and result is not None
        and not publish_error
        and len(tend_records) > 0
        and test_json(out_root).exists()
    )
    if can_evaluate:
        eval_dir = out_root.parent / "eval" / "smoke"
        try:
            from tend.cli.evaluate import run_evaluation

            log_module.emit("smoke.evaluate.start", eval_dir=str(eval_dir))
            run_evaluation(
                test_json(out_root),
                solver="echo_gold",
                out_dir=eval_dir,
                release_tag=f"tend-release-{release_tag}",
                submission_id=f"tend-eval-{release_tag}",
                solver_id="echo-gold",
            )
            log_module.emit("smoke.evaluate.done", eval_dir=str(eval_dir))
        except Exception as exc:  # noqa: BLE001
            eval_error = str(exc)
            log_module.emit("smoke.evaluate.fail", error=eval_error, level="ERROR")

    if with_disclosure and eval_dir is not None and not eval_error:
        try:
            from tend.evaluate.disclosure import (
                check_disclosure_artifacts,
                disclosure_report,
            )

            leaderboard_path = eval_dir / "leaderboard.json"
            leaderboard = (
                json.loads(leaderboard_path.read_text(encoding="utf-8"))
                if leaderboard_path.exists()
                else {}
            )
            checks = check_disclosure_artifacts(
                eval_dir,
                leaderboard=leaderboard,
                panel_stub=True,
            )
            disclosure_summary = disclosure_report(checks)
            (eval_dir / "disclosure_report.json").write_text(
                json.dumps(disclosure_summary, indent=2),
                encoding="utf-8",
            )
            log_module.emit(
                "smoke.disclosure.done",
                complete=bool(disclosure_summary.get("complete")),
                missing=disclosure_summary.get("missing", []),
            )
        except Exception as exc:  # noqa: BLE001
            disclosure_error = str(exc)
            log_module.emit(
                "smoke.disclosure.fail", error=disclosure_error, level="ERROR"
            )

    meta = {
        "stage": "smoke",
        "llm_stub": bool(llm_stub),
        "use_fixtures": prefer_fixture,
        "panel_stub": True,
        "record_count": len(tend_records),
        "db_ids": sorted({r["db_id"] for r in tend_records}),
        "failures": failures,
        "publish_skipped": skip_publish,
        "publish_error": publish_error,
        "eval_dir": str(eval_dir) if eval_dir else None,
        "eval_error": eval_error,
        "disclosure_complete": bool(disclosure_summary.get("complete")) if disclosure_summary else False,
        "disclosure_missing": disclosure_summary.get("missing", []) if disclosure_summary else None,
        "disclosure_error": disclosure_error,
        "release_tag": release_tag,
        "workers": workers,
        "coverage_report": str(coverage_report_path(out_root)),
        "run_dir": str(run_dir),
    }
    (out_root / "_meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log_module.emit(
        "smoke.done",
        records=len(tend_records),
        failures=len(failures),
        publish_skipped=skip_publish,
        publish_error=publish_error,
        eval_error=eval_error,
        disclosure_error=disclosure_error,
    )
    return {
        "publish": result,
        "meta": meta,
        "db_ids": db_ids,
        "failures": failures,
        "records": records,
        "eval_dir": str(eval_dir) if eval_dir else None,
        "disclosure_summary": disclosure_summary,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Real-LLM smoke run (10 dbs × N records by default)."
    )
    parser.add_argument("--out", default=str(DEFAULT_OUT_ROOT / "smoke"))
    parser.add_argument("--records-per-db", type=int, default=5)
    parser.add_argument("--extra-dbs", type=int, default=4, help="Number of non-fixture spider dbs to add")
    parser.add_argument("--db-ids", nargs="*", default=None, help="Override db_ids list explicitly")
    parser.add_argument("--test-ratio", type=float, default=0.20)
    parser.add_argument(
        "--allow-stub",
        action="store_true",
        help="Skip the real-LLM check (CI/local testing only).",
    )
    parser.add_argument(
        "--llm-stub",
        action="store_true",
        help="Force stub mode (implies --allow-stub); use only for fast plumbing checks.",
    )
    parser.add_argument(
        "--skip-publish",
        action="store_true",
        help="Skip publish_dataset (H5/H7/H8/H9 may reject tiny samples).",
    )
    parser.add_argument(
        "--no-evaluate",
        dest="with_evaluate",
        action="store_false",
        default=True,
        help="Skip post-publish evaluate (disclosure check also skipped).",
    )
    parser.add_argument(
        "--no-disclosure",
        dest="with_disclosure",
        action="store_false",
        default=True,
        help="Skip disclosure-checklist verification.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel LLM workers for Phase A (per db) and Phase B (per record). "
        "Default: TEND_LLM_WORKERS env or 128.",
    )
    parser.add_argument(
        "--release-tag",
        default="smoke-v0",
        help="Release tag suffix for evaluate/leaderboard/panel/disclosure.",
    )
    args = parser.parse_args(argv)

    if args.allow_stub or args.llm_stub:
        os.environ["TEND_PILOT_ALLOW_STUB"] = "1"
    if args.llm_stub:
        os.environ["TEND_LLM_STUB"] = "1"

    try:
        result = run_smoke(
            Path(args.out),
            records_per_db=args.records_per_db,
            db_ids=args.db_ids,
            test_ratio=args.test_ratio,
            skip_llm_check=args.allow_stub or args.llm_stub,
            extra_db_count=args.extra_dbs,
            llm_stub=True if args.llm_stub else None,
            skip_publish=args.skip_publish,
            with_evaluate=args.with_evaluate,
            with_disclosure=args.with_disclosure,
            release_tag=args.release_tag,
            workers=args.workers,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"run_smoke failed: {exc}", file=sys.stderr)
        return 1

    meta = result["meta"]
    hard_error = meta.get("publish_error") or meta.get("eval_error")
    status_word = "OK" if not hard_error else "PARTIAL"
    print(
        f"Smoke {status_word}: {meta['record_count']} records across {len(result['db_ids'])} dbs "
        f"-> {args.out}"
    )
    print(f"DB IDs: {', '.join(result['db_ids'])}")
    if meta.get("publish_error"):
        print(f"Publish error: {meta['publish_error']}")
    if meta.get("eval_error"):
        print(f"Eval error: {meta['eval_error']}")
    if meta.get("disclosure_error"):
        print(f"Disclosure error: {meta['disclosure_error']}")
    if meta.get("eval_dir"):
        print(
            f"Eval dir: {meta['eval_dir']} "
            f"(disclosure_complete={meta['disclosure_complete']})"
        )
        if meta.get("disclosure_missing"):
            print(f"Disclosure missing: {', '.join(meta['disclosure_missing'])}")
    if result["failures"]:
        print(f"Failures: {len(result['failures'])}")
        for fail in result["failures"]:
            print(f"  - {fail}")
    return 0 if not hard_error else 2


if __name__ == "__main__":
    raise SystemExit(main())
