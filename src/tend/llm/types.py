from __future__ import annotations

from typing import Any


Message = dict[str, Any]


def diagnostics_ref_from_transcript(transcript_ref: str) -> str:
    if transcript_ref.endswith(".md"):
        return f"{transcript_ref[:-3]}.diagnostics.json"
    return transcript_ref
