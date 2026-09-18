"""Locate the files of a TEND dataset directory.

A dataset directory is either the formal release restored from Google Drive
(``data/TEND.json`` plus ``mongodb_data/``) or the flat directory that
``tend construct`` writes (``test.json``, ``TEND.json``, ``mongodb_data/``, and the
construction artifacts next to them).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ReleaseDatasetLayout:
    root: Path
    test_path: Path
    tend_path: Path
    catalog_path: Path
    mongodb_schema_dir: Path
    mongodb_data_dir: Path
    agent_design_rationale_dir: Path
    migration_recipe_dir: Path
    native_feature_manifest_dir: Path
    provenance_dir: Path


def resolve_release_dataset_layout(dataset_dir: str | Path) -> ReleaseDatasetLayout:
    """Resolve the files of a formal release or a ``tend construct`` dataset directory."""
    root = Path(dataset_dir)
    release_tasks = root / "data" / "TEND.json"
    if release_tasks.exists():
        test_path = tend_path = release_tasks
    else:
        test_path, tend_path = root / "test.json", root / "TEND.json"
    return ReleaseDatasetLayout(
        root=root,
        test_path=test_path,
        tend_path=tend_path,
        catalog_path=root / "bird_db_catalog.json",
        mongodb_schema_dir=root / "mongodb_schema",
        mongodb_data_dir=root / "mongodb_data",
        agent_design_rationale_dir=root / "agent_design_rationale",
        migration_recipe_dir=root / "migration_recipe",
        native_feature_manifest_dir=root / "native_feature_manifest",
        provenance_dir=root / "provenance",
    )


__all__ = ["ReleaseDatasetLayout", "resolve_release_dataset_layout"]
