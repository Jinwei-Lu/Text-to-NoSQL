"""Observability: structured logging with anomaly capture, and live terminal progress.

Two cooperating subsystems:
  - :mod:`tend.observability._runtime` — DynaDB-style run files: ``run.log``,
    ``milestones.jsonl`` (run timeline), ``errors.jsonl``, ``cost_summary.jsonl``
    (LLM usage), stage-local DynaDB-style markdown LLM sessions, and
    ``*.diagnostics.json`` machine sidecars.
    Anomalies fire subscriber callbacks so the UI can surface them the instant they happen.
  - :mod:`tend.observability.progress` — a rich live tree showing phase/db/record/agent
    state plus a rolling anomaly ticker, so a human catches stalls and failures in time.
"""
from __future__ import annotations

from .progress import ProgressReporter, make_reporter
from ._runtime import RunFinalizer, RunLogger, RunOutcome, new_run_id, setup_logging

__all__ = [
    "RunLogger",
    "RunFinalizer",
    "RunOutcome",
    "setup_logging",
    "new_run_id",
    "ProgressReporter",
    "make_reporter",
]
