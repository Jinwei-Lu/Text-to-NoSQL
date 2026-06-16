"""LLM access layer — the single choke point through which every model call flows.

All construction sub-agents call the model via :class:`LLMClient`. Centralizing here is
what makes anomaly capture complete: every call is transcripted, every failure mode is
classified into a typed :class:`~tend.errors.LLMError`, and JSON/schema repair is handled
uniformly so agents only deal with validated structured output.
"""
from __future__ import annotations

from .client import LLMClient, LLMResult, Message
from .types import ToolCall, ToolChoice, ToolLLMResult, ToolSchema

__all__ = [
    "LLMClient",
    "LLMResult",
    "Message",
    "ToolCall",
    "ToolChoice",
    "ToolLLMResult",
    "ToolSchema",
]
