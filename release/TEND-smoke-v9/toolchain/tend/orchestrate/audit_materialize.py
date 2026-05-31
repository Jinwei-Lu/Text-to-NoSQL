"""Copy Tier-2 audit artifacts into the release bundle for C8 ref resolution."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from tend.config import REPO_ROOT
from tend.orchestrate.paths import audit_root, global_audit_dir

DB_AUDIT_FILES = (
    "wp_output.yaml",
    "migration_log.json",
    "phenomena_audit.json",
    "sra.yaml",
)

GLOBAL_AUDIT_FILES = (
    "flex_supply_report.json",
    "domain_map_warnings.json",
)


def _audit_source_roots(input_root: Path | None) -> list[Path]:
    roots: list[Path] = []
    if input_root is not None:
        candidate = input_root / "audit"
        if candidate.is_dir():
            roots.append(candidate)
    for extra in (
        REPO_ROOT / "out" / "TEND" / "audit",
        REPO_ROOT / "out" / "audit",
    ):
        if extra.is_dir() and extra not in roots:
            roots.append(extra)
    return roots


def _copy_if_newer(src: Path, dst: Path) -> None:
    if not src.is_file():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size >= src.stat().st_size:
        return
    shutil.copy2(src, dst)


def materialize_audit_trail(
    out_root: Path,
    tend_records: list[dict[str, Any]],
    *,
    input_root: Path | None = None,
) -> None:
    """Copy per-db and per-record audit files referenced by published records."""
    dest = audit_root(out_root)
    dest.mkdir(parents=True, exist_ok=True)
    sources = _audit_source_roots(input_root)

    db_ids = {str(r["db_id"]) for r in tend_records}
    for db_id in sorted(db_ids):
        for name in DB_AUDIT_FILES:
            for src_root in sources:
                src = src_root / db_id / name
                if src.is_file():
                    _copy_if_newer(src, dest / db_id / name)
                    break

    for record in tend_records:
        db_id = str(record["db_id"])
        rid = str(record["record_id"])
        dst_dir = dest / db_id / rid
        for src_root in sources:
            src_dir = src_root / db_id / rid
            if not src_dir.is_dir():
                continue
            dst_dir.mkdir(parents=True, exist_ok=True)
            for src_file in src_dir.iterdir():
                if src_file.is_file():
                    _copy_if_newer(src_file, dst_dir / src_file.name)

    global_dest = global_audit_dir(out_root)
    global_dest.mkdir(parents=True, exist_ok=True)
    for name in GLOBAL_AUDIT_FILES:
        for src_root in sources:
            src = src_root / "_global" / name
            if src.is_file():
                _copy_if_newer(src, global_dest / name)
                break
