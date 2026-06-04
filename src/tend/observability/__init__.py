"""Observability: structured logging with anomaly capture, and live terminal progress.

Two cooperating subsystems:
  - :mod:`tend.observability._runtime` — file-first JSONL logs. ``events.jsonl`` (all),
    ``milestones.jsonl`` (run timeline), ``errors.jsonl`` / ``anomalies.jsonl`` (the
    subset an operator greps first), ``cost_summary.jsonl`` (LLM usage), DynaDB-style
    markdown LLM call/session logs, and ``*.diagnostics.json`` machine sidecars.
    Anomalies fire subscriber callbacks so the UI can surface them the instant they happen.
  - :mod:`tend.observability.progress` — a rich live tree showing phase/db/record/agent
    state plus a rolling anomaly ticker, so a human catches stalls and failures in time.
"""
from __future__ import annotations

from .progress import ProgressReporter, make_reporter
from ._runtime import RunLogger, new_run_id, setup_logging

__all__ = [
    "RunLogger",
    "setup_logging",
    "new_run_id",
    "ProgressReporter",
    "make_reporter",
]
