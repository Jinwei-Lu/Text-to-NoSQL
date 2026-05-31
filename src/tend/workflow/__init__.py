"""Dynamic workflow engine: spawn construction sub-agents over a runtime work-list.

:class:`~tend.workflow.engine.Workflow` provides the orchestration primitives —
``agent`` (spawn one sub-agent), ``parallel`` (barrier fan-out with failure isolation),
and ``pipeline`` (per-item independent staging) — all concurrency-limited and wired to
logging + progress. The TEND-specific DAGs live in :mod:`tend.workflow.flows`:
``run_phase_a`` (per-db WP->SRA->SC-loop->DM) and ``run_phase_b`` (per-record
QPS->MS->MUT->PV->NLP->RTV->NNC->RA with feedback).
"""
from __future__ import annotations

from .engine import Workflow
from .flows import run_phase_a, run_phase_b

__all__ = ["Workflow", "run_phase_a", "run_phase_b"]
