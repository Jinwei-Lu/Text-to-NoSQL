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
from .native import (
    NativeExecutionResult,
    NativeFeature,
    NativeFeatureManifest,
    load_native_feature_manifest,
)
from .phase_a import NativeDbArtifacts, run_native_phase_a
from .phase_b import NativeCoverageSlot, plan_native_slots, run_native_phase_b
from .verify import (
    AntiSqlTransferReport,
    NativeVerificationResult,
    classify_anti_sql_transfer,
    verify_native_record,
)

__all__ = [
    "AntiSqlTransferReport",
    "NativeCoverageSlot",
    "NativeDbArtifacts",
    "NativeExecutionResult",
    "NativeFeature",
    "NativeFeatureManifest",
    "NativeVerificationResult",
    "audit_database_structure",
    "classify_anti_sql_transfer",
    "load_native_feature_manifest",
    "plan_native_slots",
    "run_native_phase_a",
    "run_native_phase_b",
    "validate_structure_gate",
    "verify_native_record",
    "write_catalog",
    "write_native_feature_manifest",
    "write_native_phase_a",
    "write_native_recipe",
    "write_provenance",
    "write_records",
]
