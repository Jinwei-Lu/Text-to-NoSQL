"""Dynamic workflow engine primitives.

:class:`~tend.workflow.engine.Workflow` provides the orchestration primitives —
``agent`` (spawn one sub-agent), ``parallel`` (barrier fan-out with failure isolation),
and ``pipeline`` (per-item independent staging) — all concurrency-limited and wired to
logging + progress. Dataset construction lives in :mod:`tend.construction`.
"""
from __future__ import annotations

from .engine import Workflow

__all__ = ["Workflow"]
