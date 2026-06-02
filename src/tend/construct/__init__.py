"""Deterministic construction tools (no LLM): migration and supply census/coverage.

  * :mod:`tend.construct.migrate` — DM's document-aggregate migration: derive an
    embed/reference plan from real BIRD FK structure and materialize witness documents
    (sparse satellites -> optional embeds; large fact tables -> referenced collections;
    NULL -> missing key; deterministic sampling for huge tables).
"""
from __future__ import annotations

from .migrate import MigrationPlan, build_plan, migrate
from .native_executor import NativeExecutionResult, execute_native_recipe
from .native_recipe import (
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

__all__ = [
    "MigrationPlan",
    "NativeCollectionRecipe",
    "NativeExecutionResult",
    "NativeFeature",
    "NativeFeatureManifest",
    "NativeMigrationRecipe",
    "NativeProvenance",
    "NativeTransform",
    "RecipeValidationResult",
    "build_plan",
    "dump_native_feature_manifest",
    "dump_native_recipe",
    "execute_native_recipe",
    "load_native_feature_manifest",
    "load_native_recipe",
    "migrate",
    "verify_native_recipe",
]
