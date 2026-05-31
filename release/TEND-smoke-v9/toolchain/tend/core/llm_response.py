"""Shared utility for parsing LLM responses that may be wrapped in markdown fences."""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

_FENCE_RE = re.compile(r"```([A-Za-z0-9_+-]*)\s*\n(.*?)\n\s*```", re.DOTALL)


def _try_yaml(text: str) -> dict[str, Any] | None:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def parse_llm_json_response(response: Any) -> dict[str, Any] | None:
    """Extract a structured dict from an LLM response.

    Handles:
    - Already-parsed dict (returned directly)
    - Dict with {"text": "..."} wrapper (unwraps and re-parses)
    - Raw string with ```json``` / ```yaml``` / unlabeled fence
    - Raw string that starts with '{' (JSON) or with a YAML mapping key

    Tries JSON first, falls back to YAML (which accepts JSON as a subset).
    Returns None if no dict can be recovered.
    """
    if isinstance(response, dict):
        if set(response.keys()) == {"text"} and isinstance(response.get("text"), str):
            return parse_llm_json_response(response["text"])
        return response if response else None
    if not isinstance(response, str):
        return None
    text = response.strip()
    match = _FENCE_RE.search(text)
    fence_lang = ""
    if match:
        fence_lang = (match.group(1) or "").lower()
        text = match.group(2).strip()
    if not text:
        return None

    # JSON fast path (covers explicit ```json``` and raw `{`-prefixed strings).
    if fence_lang in ("", "json") or text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # YAML fallback (covers ```yaml``` fence and YAML mapping bodies).
    if fence_lang in ("", "yaml", "yml") or ":" in text.split("\n", 1)[0]:
        return _try_yaml(text)

    return None
