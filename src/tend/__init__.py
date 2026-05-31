"""TEND — Text-to-NoSQL benchmark construction pipeline.

A dynamic-workflow orchestrator that spawns LLM sub-agents to construct the TEND
dataset from BIRD mini-dev: Phase A (DataWorld: WP -> SRA -> SC -> DM) and Phase B
(reverse-engineered NL-MQL: QPS -> MS -> MUT -> PV -> NLP -> RTV -> NNC -> RA).

Design pillars (see proposals/ for the methodology SSoT):
  - Structured, machine-greppable logging with first-class anomaly capture
    (every LLM prompt/response is persisted; anomalies are classified and streamed).
  - Live terminal progress so a human sees stalls/failures as they happen.
  - A dynamic workflow engine that fans out sub-agent tasks per-db / per-record.
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
