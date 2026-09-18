"""The orchestration engine: structural fan-out primitives.

The engine is purely structural and does not bound throughput; live LLM throughput is
limited at its single canonical chokepoint (``LLMClient``'s semaphore gate).

``parallel`` is a barrier with failure isolation (a failed thunk -> ``None``); ``pipeline``
runs each item through stages independently (no barrier) so a slow item never blocks the
fast ones. Isolated failures are logged here before they become ``None``.
"""
from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from typing import Any

from ..agents import AgentContext
from ..errors import TendError, WorkflowError, wrap_unexpected

Thunk = Callable[[], Awaitable[Any]]
Stage = Callable[[Any], Awaitable[Any]]


class Workflow:
    """Stateful orchestrator bound to a base :class:`AgentContext`."""

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------ #
    def phase(self, name: str) -> None:
        self.ctx.phase = name
        if self.ctx.progress:
            self.ctx.progress.phase(name)

    def context(self, **fields: Any) -> AgentContext:
        """Derive a bound context (db_id/record_id/group/...) from the base context."""
        return self.ctx.bind(**fields)

    # ------------------------------------------------------------------ #
    async def parallel(self, thunks: list[Thunk], *, isolate: bool = True) -> list[Any]:
        """Barrier fan-out. With ``isolate`` (default), a thunk that raises yields ``None``
        in its slot rather than failing the whole batch."""
        async def guard(index: int, thunk: Thunk) -> Any:
            try:
                return await thunk()
            except TendError as err:
                if not isolate:
                    raise
                if not err.logged:
                    self._log_isolated_failure(
                        err,
                        primitive="parallel",
                        index=index,
                        item_repr=_short_repr(thunk),
                    )
                return None
            except Exception as exc:  # noqa: BLE001 - isolate must not hide raw faults
                if isolate:
                    self._log_isolated_failure(
                        exc,
                        primitive="parallel",
                        index=index,
                        item_repr=_short_repr(thunk),
                    )
                    return None
                raise

        return list(await asyncio.gather(*(guard(i, t) for i, t in enumerate(thunks))))

    async def pipeline(self, items: list[Any], *stages: Stage,
                       isolate: bool = True) -> list[Any]:
        """Run each item through ``stages`` independently (no inter-stage barrier).

        A stage returning/raising drops that item to ``None`` and skips its remaining
        stages, so one bad item never stalls the batch."""
        if not stages:
            raise WorkflowError("pipeline requires at least one stage")

        async def chain(index: int, item: Any) -> Any:
            cur = item
            for stage_index, stage in enumerate(stages):
                try:
                    cur = await stage(cur)
                except TendError as err:
                    if not isolate:
                        raise
                    if not err.logged:
                        self._log_isolated_failure(
                            err,
                            primitive="pipeline",
                            index=index,
                            stage_index=stage_index,
                            stage_repr=_short_repr(stage),
                            item_repr=_short_repr(item),
                            current_repr=_short_repr(cur),
                        )
                    return None
                except Exception as exc:  # noqa: BLE001 - isolate must not hide raw faults
                    if isolate:
                        self._log_isolated_failure(
                            exc,
                            primitive="pipeline",
                            index=index,
                            stage_index=stage_index,
                            stage_repr=_short_repr(stage),
                            item_repr=_short_repr(item),
                            current_repr=_short_repr(cur),
                        )
                        return None
                    raise
                if cur is None:
                    return None
            return cur

        return list(await asyncio.gather(*(chain(i, item) for i, item in enumerate(items))))

    def _log_isolated_failure(self, exc: BaseException, **context: Any) -> None:
        context = {
            **context,
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        }
        err = exc.with_context(**context) if isinstance(exc, TendError) else wrap_unexpected(
            exc, **context
        )
        self.ctx.log.anomaly(err)


def _short_repr(value: Any, limit: int = 500) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit - 3]}..."
