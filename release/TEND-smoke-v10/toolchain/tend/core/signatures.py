from __future__ import annotations

import hashlib
import json
import math
import struct
from typing import Any

from .io import stable_json_dumps


def schema_signature(schema_payload: dict[str, Any]) -> str:
    return _jcs_sha256(schema_payload)


def world_signature(data_payload: dict[str, Any]) -> str:
    return _jcs_sha256(data_payload)


def si_hash(si_payload: dict[str, Any]) -> str:
    return _jcs_sha256(si_payload)


def detector_signature(detector_name: str, detector_version: str, config: dict[str, Any] | None = None) -> str:
    return _jcs_sha256(
        {
            "detector_name": detector_name,
            "detector_version": detector_version,
            "config": config or {},
        }
    )


def _jcs_sha256(payload: Any) -> str:
    canonical = _jcs_serialize(payload)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _jcs_serialize(value: Any) -> str:
    """RFC 8785 JSON Canonicalization Scheme serialization."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, (list, tuple)):
        items = ",".join(_jcs_serialize(item) for item in value)
        return f"[{items}]"
    if isinstance(value, dict):
        sorted_keys = sorted(value.keys(), key=_utf16_sort_key)
        pairs = ",".join(
            f"{_jcs_string(k)}:{_jcs_serialize(value[k])}" for k in sorted_keys
        )
        return f"{{{pairs}}}"
    return _jcs_serialize(str(value))


def _jcs_number(value: float) -> str:
    """Serialize a float per RFC 8785 §3.2.2.3 (ES6 Number serialization)."""
    if math.isnan(value) or math.isinf(value):
        return "null"
    if value == 0.0:
        return "0"
    return json.dumps(value)


def _jcs_string(value: str) -> str:
    """Serialize a string per RFC 8785 §3.2.2.2."""
    out: list[str] = ['"']
    for ch in value:
        cp = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == '\\':
            out.append('\\\\')
        elif ch == '\b':
            out.append('\\b')
        elif ch == '\f':
            out.append('\\f')
        elif ch == '\n':
            out.append('\\n')
        elif ch == '\r':
            out.append('\\r')
        elif ch == '\t':
            out.append('\\t')
        elif cp < 0x20:
            out.append(f'\\u{cp:04x}')
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _utf16_sort_key(key: str) -> list[int]:
    """Sort keys by UTF-16 code units per RFC 8785."""
    return list(key.encode("utf-16-le"))
