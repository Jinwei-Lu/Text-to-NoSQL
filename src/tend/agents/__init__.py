"""Construction sub-agents and the contract they share.

Every agent — Phase A (WP/SRA/SC/DM) and Phase B (QPS/MS/MUT/PV/NLP/RTV/NNC/RA) —
subclasses :class:`~tend.agents.base.Agent`. LLM-backed agents subclass
:class:`~tend.agents.base.LLMAgent`, which loads the methodology prompt, calls the model
through :class:`~tend.llm.LLMClient` with the output JSON Schema, and runs a semantic
``check_contract`` repair loop. Deterministic agents (DM, detectors) subclass ``Agent``
directly. The :data:`REGISTRY` maps agent id -> class for the workflow engine.
"""
from __future__ import annotations

from .base import (
    REGISTRY,
    Agent,
    AgentContext,
    LLMAgent,
    get_agent,
    register,
)

# importing the concrete agent modules triggers @register for all agents
from . import dm, native_migration, native_nl, phase_a, phase_b  # noqa: E402,F401

__all__ = [
    "Agent",
    "LLMAgent",
    "AgentContext",
    "REGISTRY",
    "register",
    "get_agent",
]
