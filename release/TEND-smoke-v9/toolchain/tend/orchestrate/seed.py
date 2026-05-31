"""Deterministic seed dispatch for the TEND pipeline."""

from __future__ import annotations

import hashlib

GLOBAL_SEED = 42
STAGE_OFFSETS = {
    "phase_a": 1_000,
    "phase_b.synth": 2_000,
    "phase_b.valid": 3_000,
    "split": 4_000,
    "publish": 5_000,
    "coverage": 6_000,
}


def _mix(*parts: str | int) -> int:
    payload = "|".join(str(p) for p in parts).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:8], 16)


def global_seed() -> int:
    return GLOBAL_SEED


def db_seed(db_id: str, stage: str = "phase_a") -> int:
    return _mix(GLOBAL_SEED, "db", db_id, stage) & 0x7FFFFFFF


def record_seed(db_id: str, record_id: int | str, stage: str = "phase_b.synth") -> int:
    return _mix(GLOBAL_SEED, "record", db_id, record_id, stage) & 0x7FFFFFFF


def stage_seed(stage: str) -> int:
    base = STAGE_OFFSETS.get(stage, 0)
    return _mix(GLOBAL_SEED, "stage", stage, base) & 0x7FFFFFFF


def split_seed() -> int:
    return stage_seed("split")


def publish_seed() -> int:
    return stage_seed("publish")
