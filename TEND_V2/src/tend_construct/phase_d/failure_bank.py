from __future__ import annotations

from pathlib import Path
from typing import Any

from tend_core import load_json


def default_failure_bank_root() -> Path:
    return Path(__file__).resolve().parents[3] / "assets" / "failure_mode_bank"


def load_failure_bank(root: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    bank_root = root or default_failure_bank_root()
    bank: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(bank_root.glob("*.json")):
        bank[path.stem] = load_json(path)
    return bank


def instantiate_failure_modes(
    pattern_family: str,
    bank: dict[str, list[dict[str, Any]]],
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    templates = bank.get(pattern_family, [])
    instantiated: list[dict[str, Any]] = []
    for item in templates:
        query_template = item.get("query_template", "")
        try:
            query = query_template.format(**context)
        except KeyError:
            continue
        instantiated.append(
            {
                "mutation_id": item["mutation_id"],
                "query": query,
                "failure_type": item.get("failure_type", "generic"),
                "source": "failure_mode_bank",
            }
        )
    return instantiated
