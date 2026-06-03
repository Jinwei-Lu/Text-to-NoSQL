"""Provider-native SMART-EG message history."""
from __future__ import annotations

import json
from typing import Any


class SmartEGHistory:
    def __init__(self, *, system_prompt: str | None = None) -> None:
        self.messages: list[dict[str, Any]] = []
        self._pending_tool_calls: list[dict[str, str]] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    @property
    def pending_tool_call_ids(self) -> list[str]:
        return [item["id"] for item in self._pending_tool_calls]

    def add_user(self, content: str) -> None:
        self._require_no_pending()
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, message: dict[str, Any]) -> None:
        self._require_no_pending()
        out = dict(message)
        out.setdefault("role", "assistant")
        calls = list(out.get("tool_calls") or [])
        self.messages.append(out)
        self._pending_tool_calls = [
            {
                "id": str(call.get("id")),
                "name": str((call.get("function") or {}).get("name") or call.get("name") or ""),
            }
            for call in calls
            if call.get("id")
        ]

    def add_tool_result(self, tool_call_id: str, name: str, content: Any) -> None:
        pending = {item["id"]: item for item in self._pending_tool_calls}
        if tool_call_id not in pending:
            raise ValueError(f"tool result has no pending assistant call: {tool_call_id}")
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "name": name,
                "content": self._content_text(content),
            }
        )
        self._pending_tool_calls = [
            item for item in self._pending_tool_calls if item["id"] != tool_call_id
        ]

    def validate_provider_invariants(self) -> bool:
        index = 0
        while index < len(self.messages):
            message = self.messages[index]
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                index += 1
                continue
            calls = list(message.get("tool_calls") or [])
            expected = [str(call.get("id")) for call in calls]
            seen: list[str] = []
            cursor = index + 1
            while cursor < len(self.messages) and self.messages[cursor].get("role") == "tool":
                seen.append(str(self.messages[cursor].get("tool_call_id")))
                cursor += 1
            if expected != seen:
                return False
            index = cursor
        return not self._pending_tool_calls

    def compact(self, *, max_messages: int, state_summary: dict[str, Any]) -> bool:
        if len(self.messages) <= max_messages:
            return False
        self._require_no_pending()
        system = [msg for msg in self.messages[:1] if msg.get("role") == "system"]
        groups = self._complete_groups(self.messages[len(system):])
        tail_budget = max(1, max_messages - len(system) - 1)
        kept: list[dict[str, Any]] = []
        for group in reversed(groups):
            if len(kept) + len(group) > tail_budget:
                break
            kept = [*group, *kept]
        summary = {
            "role": "user",
            "content": "SMART-EG compact state summary:\n"
            + json.dumps(state_summary, ensure_ascii=False, sort_keys=True, default=str),
        }
        self.messages = [*system, summary, *kept]
        return True

    def build_messages(self, state_summary: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if state_summary is None:
            return list(self.messages)
        self._require_no_pending()
        return [
            *self.messages,
            {
                "role": "user",
                "content": "Runtime state:\n"
                + json.dumps(state_summary, ensure_ascii=False, sort_keys=True, default=str),
            },
        ]

    def _require_no_pending(self) -> None:
        if self._pending_tool_calls:
            raise ValueError(f"pending tool calls need tool results: {self.pending_tool_call_ids}")

    @staticmethod
    def _content_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)

    @staticmethod
    def _complete_groups(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        groups: list[list[dict[str, Any]]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.get("role") == "assistant" and message.get("tool_calls"):
                calls = list(message.get("tool_calls") or [])
                group = [message]
                cursor = index + 1
                while cursor < len(messages) and messages[cursor].get("role") == "tool":
                    group.append(messages[cursor])
                    cursor += 1
                if len(group) == len(calls) + 1:
                    groups.append(group)
                index = cursor
                continue
            groups.append([message])
            index += 1
        return groups
