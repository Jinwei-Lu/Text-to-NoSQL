"""Wire LLM client usage/retry callbacks into the live progress panel."""
from __future__ import annotations

from typing import Any, Protocol


class LLMUsageProgressCallback(Protocol):
    def __call__(
        self,
        cost: float | None,
        prompt_tokens: int,
        completion_tokens: int,
        cost_source: str,
        call_id: str | None = None,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None: ...


def wire_llm_progress_callbacks(llm: Any, progress: Any) -> None:
    """Attach DynaDB-style progress callbacks to a TEND LLM client."""

    def _on_usage(
        cost: float | None,
        prompt_tokens: int,
        completion_tokens: int,
        cost_source: str,
        call_id: str | None = None,
        cache_hit_tokens: int = 0,
        cache_miss_tokens: int = 0,
    ) -> None:
        progress.update_cost(
            cost,
            prompt_tokens,
            completion_tokens,
            cost_source=cost_source,
            call_id=call_id,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
        )
        progress.note_llm_ok()

    def _on_retry(
        reason: str,
        attempt: int,
        wait_s: float,
        exc: str,
        max_attempts: int | None = None,
    ) -> None:
        progress.note_llm_retry(
            reason,
            attempt,
            wait_s,
            exc,
            max_attempts=max_attempts,
        )

    llm.on_usage = _on_usage
    llm.on_retry = _on_retry
    llm.on_provider_wait = (
        lambda provider, next_provider, wait_s, reason: progress.note_llm_provider_wait(
            provider,
            next_provider,
            wait_s,
            reason,
        )
    )
    llm.on_provider_ok = progress.note_llm_provider_ok


__all__ = ["LLMUsageProgressCallback", "wire_llm_progress_callbacks"]
