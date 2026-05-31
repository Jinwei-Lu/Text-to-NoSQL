"""Verify real LLM API connectivity (one minimal billed call)."""

from __future__ import annotations

import sys

from tend.config import OPENAI_BASE_URL, llm_enabled
from tend.core.llm_client import LLMClient


def main() -> int:
    if not llm_enabled():
        print(
            "LLM disabled. Set OPENAI_API_KEY in tend/config.py and ensure TEND_LLM_STUB is not 1.",
            file=sys.stderr,
        )
        return 1
    client = LLMClient(stub=False, use_cache=False)
    result = client.call("B_rtv", 'Reply with exactly: {"ok": true}', seed=42)
    print(f"base_url={OPENAI_BASE_URL}")
    print(f"response={result!r}")
    print("Real LLM probe OK — check your provider dashboard for this request.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
