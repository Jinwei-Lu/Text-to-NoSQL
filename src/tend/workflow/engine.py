"""The orchestration engine — concurrency-limited primitives for spawning sub-agents.

Mirrors the agent/parallel/pipeline model: every ``agent(...)`` call dynamically spawns a
sub-agent task (one LLM/deterministic Agent invocation), bounded by a global semaphore.
``parallel`` is a barrier with failure isolation (a failed thunk -> ``None``); ``pipeline``
runs each item through stages independently (no barrier) so a slow item never blocks the
fast ones. Expected agent failures are already logged by the Agent lifecycle; raw
isolated primitive failures are logged here before they become ``None``.
"""
from __future__ import annotations

import asyncio
import traceback
from dataclasses import replace
from typing import Any, Awaitable, Callable

from ..agents import AgentContext, get_agent
from ..errors import TendError, WorkflowError, wrap_unexpected

Thunk = Callable[[], Awaitable[Any]]
Stage = Callable[[Any], Awaitable[Any]]


class Workflow:
    """Stateful orchestrator bound to a base :class:`AgentContext`."""

    def __init__(self, ctx: AgentContext, *, max_concurrency: int | None = None,
                 name: str = "tend") -> None:
        self.ctx = ctx
        self.name = name
        cap = max_concurrency or ctx.settings.llm.max_concurrency
        self._sem = asyncio.Semaphore(max(1, cap))
        self._spawned = 0

    # ------------------------------------------------------------------ #
    @property
    def spawned(self) -> int:
        return self._spawned

    def phase(self, name: str) -> None:
        self.ctx.phase = name
        if self.ctx.progress:
            self.ctx.progress.phase(name)

    def context(self, **fields: Any) -> AgentContext:
        """Derive a bound context (db_id/record_id/group/...) from the base context."""
        return self.ctx.bind(**fields)

    # ------------------------------------------------------------------ #
    async def agent(
        self,
        agent_id: str,
        inputs: dict[str, Any],
        *,
        ctx: AgentContext | None = None,
        group: str | None = None,
        work_item_id: str | None = None,
        isolate: bool = False,
    ) -> dict[str, Any] | None:
        """Spawn one sub-agent. Returns its validated output, or ``None`` if ``isolate``
        and it failed. Concurrency is bounded by the engine semaphore."""
        actx = ctx or self.ctx
        if group is not None or work_item_id is not None:
            actx = replace(
                actx,
                group=group if group is not None else actx.group,
                work_item_id=work_item_id if work_item_id is not None else actx.work_item_id,
            )
        agent = get_agent(agent_id)
        self._spawned += 1
        async with self._sem:
            try:
                return await agent(actx, inputs)
            except TendError:
                if isolate:
                    return None
                raise

    async def parallel(self, thunks: list[Thunk], *, isolate: bool = True) -> list[Any]:
        """Barrier fan-out. With ``isolate`` (default), a thunk that raises yields ``None``
        in its slot rather than failing the whole batch."""
        async def guard(index: int, thunk: Thunk) -> Any:
            try:
                return await thunk()
            except TendError as err:
                if not isolate:
                    raise
                if err.logged:
                    return None
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

        return await asyncio.gather(*(guard(i, t) for i, t in enumerate(thunks)))

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
                    if err.logged:
                        return None
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

        return await asyncio.gather(*(chain(i, item) for i, item in enumerate(items)))

    async def map_agent(
        self,
        agent_id: str,
        work: list[tuple[AgentContext, dict[str, Any]]],
        *,
        isolate: bool = True,
    ) -> list[dict[str, Any] | None]:
        """Fan the same agent across many (ctx, inputs) pairs concurrently."""
        return await self.parallel(
            [lambda c=c, i=i: self.agent(agent_id, i, ctx=c, isolate=isolate) for c, i in work],
            isolate=isolate,
        )

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
