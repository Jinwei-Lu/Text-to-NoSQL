"""Load 4-piece agent prompts from proposals/agent_prompts."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tend.config import PROMPTS_ROOT

SECTION_RE = re.compile(r"^##\s+(system|user|few-shot|output_schema)\s*$", re.MULTILINE | re.IGNORECASE)
VAR_RE = re.compile(r"\{\{(\w+)\}\}")


def _parse_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(SECTION_RE.finditer(text))
    for idx, match in enumerate(matches):
        name = match.group(1).lower().replace("-", "_")
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        sections[name] = text[start:end].strip()
    return sections


def load(name: str) -> dict[str, Any]:
    path = PROMPTS_ROOT / f"{name}.md"
    if not path.exists():
        alt = PROMPTS_ROOT / name
        if alt.exists():
            path = alt
        else:
            raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8")
    sections = _parse_sections(text)
    output_schema = {}
    if "output_schema" in sections:
        import json

        try:
            output_schema = json.loads(sections["output_schema"])
        except json.JSONDecodeError:
            output_schema = {"raw": sections["output_schema"]}
    return {
        "name": name,
        "system": sections.get("system", ""),
        "user": sections.get("user", ""),
        "few_shot": sections.get("few_shot", sections.get("few-shot", "")),
        "output_schema": output_schema,
    }


def render(template: str, variables: dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return variables.get(key, match.group(0))

    return VAR_RE.sub(repl, template)
