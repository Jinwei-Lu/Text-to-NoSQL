"""TEND - Text-to-NoSQL benchmark construction and evaluation package.

The active dataset builder is a MongoDB-native construction stack over BIRD
mini-dev. It materializes database-specific native DataWorlds, builds
manifest-driven NL-MQL records, validates release artifacts, and runs solver,
baseline, ablation, and evaluation workflows.

Design pillars (see proposals/ for the methodology SSoT):
  - Structured, machine-greppable logging with first-class anomaly capture
    (every LLM prompt/response is persisted; anomalies are classified and streamed).
  - Live terminal progress so a human sees stalls/failures as they happen.
  - A dynamic workflow engine for bounded parallel work.
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
