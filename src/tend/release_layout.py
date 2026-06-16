"""Helpers for reading legacy and packaged TEND release layouts."""
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
    """Resolve release paths for both legacy dataset and formal package layouts."""
    root = Path(dataset_dir)
    if (root / "data" / "test.json").exists():
        return ReleaseDatasetLayout(
            root=root,
            test_path=root / "data" / "test.json",
            tend_path=root / "data" / "TEND.json",
            catalog_path=root / "data" / "bird_db_catalog.json",
            mongodb_schema_dir=root / "schema" / "mongodb_schema",
            mongodb_data_dir=root / "mongodb_data",
            agent_design_rationale_dir=root / "metadata" / "agent_design_rationale",
            migration_recipe_dir=root / "metadata" / "migration_recipe",
            native_feature_manifest_dir=root / "metadata" / "native_feature_manifest",
            provenance_dir=root / "metadata" / "provenance",
        )
    if (root / "data" / "TEND.json").exists():
        tend_path = root / "data" / "TEND.json"
        return ReleaseDatasetLayout(
            root=root,
            test_path=tend_path,
            tend_path=tend_path,
            catalog_path=root / "data" / "bird_db_catalog.json",
            mongodb_schema_dir=root / "schema" / "mongodb_schema",
            mongodb_data_dir=root / "mongodb_data",
            agent_design_rationale_dir=root / "metadata" / "agent_design_rationale",
            migration_recipe_dir=root / "metadata" / "migration_recipe",
            native_feature_manifest_dir=root / "metadata" / "native_feature_manifest",
            provenance_dir=root / "metadata" / "provenance",
        )
    if (root / "data" / "TEND_lean.json").exists():
        lean_path = root / "data" / "TEND_lean.json"
        return ReleaseDatasetLayout(
            root=root,
            test_path=lean_path,
            tend_path=lean_path,
            catalog_path=root / "data" / "bird_db_catalog.json",
            mongodb_schema_dir=root / "schema" / "mongodb_schema",
            mongodb_data_dir=root / "mongodb_data",
            agent_design_rationale_dir=root / "metadata" / "agent_design_rationale",
            migration_recipe_dir=root / "metadata" / "migration_recipe",
            native_feature_manifest_dir=root / "metadata" / "native_feature_manifest",
            provenance_dir=root / "metadata" / "provenance",
        )
    return ReleaseDatasetLayout(
        root=root,
        test_path=root / "test.json",
        tend_path=root / "TEND.json",
        catalog_path=root / "bird_db_catalog.json",
        mongodb_schema_dir=root / "mongodb_schema",
        mongodb_data_dir=root / "mongodb_data",
        agent_design_rationale_dir=root / "agent_design_rationale",
        migration_recipe_dir=root / "migration_recipe",
        native_feature_manifest_dir=root / "native_feature_manifest",
        provenance_dir=root / "provenance",
    )


__all__ = ["ReleaseDatasetLayout", "resolve_release_dataset_layout"]
