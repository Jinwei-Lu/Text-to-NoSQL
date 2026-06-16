"""MongoDB-native dataset construction package."""
from __future__ import annotations

from .artifacts import (
    write_catalog,
    write_native_feature_manifest,
    write_native_phase_a,
    write_native_recipe,
    write_provenance,
    write_records,
)
from .audit import audit_database_structure, validate_structure_gate
from .executor import NativeExecutionResult, execute_native_recipe
from .phase_a import NativeDbArtifacts, run_native_phase_a
from .phase_b import NativeCoverageSlot, plan_native_slots, run_native_phase_b
from .recipe import (
    NativeCollectionRecipe,
    NativeFeature,
    NativeFeatureManifest,
    NativeMigrationRecipe,
    NativeProvenance,
    NativeTransform,
    RecipeValidationResult,
    dump_native_feature_manifest,
    dump_native_recipe,
    load_native_feature_manifest,
    load_native_recipe,
    verify_native_recipe,
)
from .verify import (
    AntiSqlTransferReport,
    NativeVerificationResult,
    classify_anti_sql_transfer,
    verify_native_record,
)

__all__ = [
    "AntiSqlTransferReport",
    "NativeCollectionRecipe",
    "NativeCoverageSlot",
    "NativeDbArtifacts",
    "NativeExecutionResult",
    "NativeFeature",
    "NativeFeatureManifest",
    "NativeMigrationRecipe",
    "NativeProvenance",
    "NativeTransform",
    "NativeVerificationResult",
    "RecipeValidationResult",
    "audit_database_structure",
    "classify_anti_sql_transfer",
    "dump_native_feature_manifest",
    "dump_native_recipe",
    "execute_native_recipe",
    "load_native_feature_manifest",
    "load_native_recipe",
    "plan_native_slots",
    "run_native_phase_a",
    "run_native_phase_b",
    "validate_structure_gate",
    "verify_native_record",
    "verify_native_recipe",
    "write_catalog",
    "write_native_feature_manifest",
    "write_native_phase_a",
    "write_native_recipe",
    "write_provenance",
    "write_records",
]
