"""LLM-facing agents that remain active in the native construction stack.

The current construction route is database-design-code-first. These agents provide
native recipe and NL assistance when invoked, while the authoritative dataset build
lives under :mod:`tend.construction`.
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

# Importing concrete modules triggers @register for the active agents.
from . import native_migration, native_nl  # noqa: E402,F401

__all__ = [
    "Agent",
    "LLMAgent",
    "AgentContext",
    "REGISTRY",
    "register",
    "get_agent",
]
