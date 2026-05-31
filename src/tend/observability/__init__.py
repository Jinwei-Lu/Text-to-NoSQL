"""Observability: structured logging with anomaly capture, and live terminal progress.

Two cooperating subsystems:
  - :mod:`tend.observability.logging` — file-first JSONL logs. ``events.jsonl`` (all),
    ``anomalies.jsonl`` (the subset an operator/Claude-Code greps first), and
    ``llm/<agent>/<call_id>.md`` plus ``.diagnostics.json`` sidecars for LLM calls.
    Anomalies fire subscriber callbacks so the UI can surface them the instant they happen.
  - :mod:`tend.observability.progress` — a rich live tree showing phase/db/record/agent
    state plus a rolling anomaly ticker, so a human catches stalls and failures in time.
"""
from __future__ import annotations

from .logging import RunLogger, new_run_id, setup_logging
from .progress import ProgressReporter, make_reporter

__all__ = [
    "RunLogger",
    "setup_logging",
    "new_run_id",
    "ProgressReporter",
    "make_reporter",
]
