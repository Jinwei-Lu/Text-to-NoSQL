"""Orchestrator: coverage, pipeline, split, publish, seed."""

from tend.orchestrate.coverage import CoverageController, SIX_AXES
from tend.orchestrate.paths import (
    DEFAULT_AUDIT_ROOT,
    DEFAULT_OUT_ROOT,
    audit_root,
    tend_root,
)
from tend.orchestrate.pipeline import PipelineConfig, PipelineResult, build_phase_a, build_phase_b, run_pipeline
from tend.orchestrate.publish import bootstrap_fixtures_snapshot, check_c1_c9, check_h1_h9, publish_dataset
from tend.orchestrate.seed import db_seed, global_seed, publish_seed, record_seed, split_seed
from tend.orchestrate.split import cross_domain_split

__all__ = [
    "CoverageController",
    "SIX_AXES",
    "DEFAULT_AUDIT_ROOT",
    "DEFAULT_OUT_ROOT",
    "PipelineConfig",
    "PipelineResult",
    "audit_root",
    "bootstrap_fixtures_snapshot",
    "build_phase_a",
    "build_phase_b",
    "check_c1_c9",
    "check_h1_h9",
    "cross_domain_split",
    "db_seed",
    "global_seed",
    "publish_dataset",
    "publish_seed",
    "record_seed",
    "run_pipeline",
    "split_seed",
    "tend_root",
]
