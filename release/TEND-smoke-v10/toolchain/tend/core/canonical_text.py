from __future__ import annotations

from typing import Any

from tend.errors import BOT_EXEC

from .mql import canonical_text as _canonical_text


def canonical_text(obj: Any) -> str:
    if isinstance(obj, str):
        return _canonical_text(obj)
    if isinstance(obj, (dict, list)):
        import json

        return json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return str(obj)
