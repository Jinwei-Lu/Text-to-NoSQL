"""Deterministic construction tools (no LLM): migration and supply census/coverage.

  * :mod:`tend.construct.migrate` — DM's document-aggregate migration: derive an
    embed/reference plan from real BIRD FK structure and materialize witness documents
    (sparse satellites -> optional embeds; large fact tables -> referenced collections;
    NULL -> missing key; deterministic sampling for huge tables).
"""
from __future__ import annotations

from .migrate import MigrationPlan, build_plan, migrate

__all__ = ["MigrationPlan", "build_plan", "migrate"]
