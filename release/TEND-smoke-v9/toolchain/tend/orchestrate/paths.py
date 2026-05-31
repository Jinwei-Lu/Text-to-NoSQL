"""Path constants for out/TEND and audit layout."""

from __future__ import annotations

from pathlib import Path

from tend.config import REPO_ROOT

DEFAULT_OUT_ROOT = REPO_ROOT / "out" / "TEND"
DEFAULT_AUDIT_ROOT = REPO_ROOT / "out" / "audit"
FIXTURES_SNAPSHOT_ALIASES = (
    "fixtures-snapshot",
    str(REPO_ROOT / "fixtures-snapshot"),
    str(REPO_ROOT / "proposals" / "fixtures-snapshot"),
)


def tend_root(out_root: Path | str | None = None) -> Path:
    return Path(out_root or DEFAULT_OUT_ROOT)


def audit_root(out_root: Path | str | None = None) -> Path:
    root = tend_root(out_root)
    if root.name == "TEND":
        return root.parent / "audit"
    return root / "audit"


def train_json(out_root: Path | str | None = None) -> Path:
    return tend_root(out_root) / "train.json"


def test_json(out_root: Path | str | None = None) -> Path:
    return tend_root(out_root) / "test.json"


def tend_json(out_root: Path | str | None = None) -> Path:
    return tend_root(out_root) / "TEND.json"


def spider_db_catalog_json(out_root: Path | str | None = None) -> Path:
    return tend_root(out_root) / "spider_db_catalog.json"


def mongodb_schema_dir(out_root: Path | str | None = None) -> Path:
    return tend_root(out_root) / "mongodb_schema"


def mongodb_data_dir(out_root: Path | str | None = None) -> Path:
    return tend_root(out_root) / "mongodb_data"


def agent_design_rationale_dir(out_root: Path | str | None = None) -> Path:
    return tend_root(out_root) / "agent_design_rationale"


def db_schema_path(db_id: str, out_root: Path | str | None = None) -> Path:
    return mongodb_schema_dir(out_root) / f"{db_id}.json"


def db_data_path(db_id: str, out_root: Path | str | None = None) -> Path:
    return mongodb_data_dir(out_root) / f"{db_id}.json"


def db_rationale_path(db_id: str, out_root: Path | str | None = None) -> Path:
    return agent_design_rationale_dir(out_root) / f"{db_id}.yaml"


def global_audit_dir(out_root: Path | str | None = None) -> Path:
    return audit_root(out_root) / "_global"


def coverage_report_path(out_root: Path | str | None = None) -> Path:
    return global_audit_dir(out_root) / "coverage_report.json"


def record_audit_dir(db_id: str, record_id: int | str, out_root: Path | str | None = None) -> Path:
    return audit_root(out_root) / db_id / str(record_id)


def resolve_input_root(raw: str | Path) -> Path:
    candidate = Path(raw)
    if candidate.exists():
        return candidate.resolve()
    text = str(raw)
    if text in FIXTURES_SNAPSHOT_ALIASES:
        for alias in FIXTURES_SNAPSHOT_ALIASES:
            path = Path(alias)
            if path.exists():
                return path.resolve()
    return candidate
