from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


Message = dict[str, Any]
ToolSchema = dict[str, Any]
ToolChoice = str | dict[str, Any]


def diagnostics_ref_from_transcript(transcript_ref: str) -> str:
    if transcript_ref.endswith(".md"):
        return f"{transcript_ref[:-3]}.diagnostics.json"
    return transcript_ref


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str = ""

    def to_provider(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.raw_arguments,
            },
        }


@dataclass
class ToolLLMResult:
    """A provider-native tool-call completion."""

    agent: str
    call_id: str
    model: str
    assistant_message: Message
    tool_calls: list[ToolCall]
    finish_reason: str | None
    usage: dict[str, int]
    cost: dict[str, Any]
    latency_s: float
    attempts: int
    transcript_ref: str
    diagnostics_ref: str
    provider_metadata: dict[str, Any]
    text: str = ""
    tool_choice_fallback: bool = False

    @property
    def cost_source(self) -> str:
        return str(self.cost.get("source") or "unavailable")

    @property
    def provider_tool_calls(self) -> list[dict[str, Any]]:
        return [call.to_provider() for call in self.tool_calls]


def parse_tool_calls(raw_calls: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if not isinstance(raw_calls, list):
        return calls
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        name = str(function.get("name") or raw.get("name") or "").strip()
        if not name:
            continue
        raw_arguments = function.get("arguments") or raw.get("arguments") or "{}"
        if not isinstance(raw_arguments, str):
            raw_arguments = "{}"
        try:
            arguments = json.loads(raw_arguments or "{}")
        except (json.JSONDecodeError, TypeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=name,
                arguments=arguments,
                raw_arguments=raw_arguments,
            )
        )
    return calls
