"""The orchestration engine — concurrency-limited primitives for spawning sub-agents.

Mirrors the agent/parallel/pipeline model: every ``agent(...)`` call dynamically spawns a
sub-agent task (one LLM/deterministic Agent invocation), bounded by a global semaphore.
``parallel`` is a barrier with failure isolation (a failed thunk -> ``None``); ``pipeline``
runs each item through stages independently (no barrier) so a slow item never blocks the
fast ones. All failures are already logged as anomalies by the Agent lifecycle, so the
engine only decides propagate-vs-isolate.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any, Awaitable, Callable

from ..agents import AgentContext, get_agent
from ..errors import TendError, WorkflowError

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
        isolate: bool = False,
    ) -> dict[str, Any] | None:
        """Spawn one sub-agent. Returns its validated output, or ``None`` if ``isolate``
        and it failed. Concurrency is bounded by the engine semaphore."""
        actx = ctx or self.ctx
        if group is not None:
            actx = replace(actx, group=group)
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
        in its slot rather than failing the whole batch — filter with ``[x for x in ... if x]``."""
        async def guard(thunk: Thunk) -> Any:
            try:
                return await thunk()
            except Exception:  # noqa: BLE001 - already logged by the Agent lifecycle
                if isolate:
                    return None
                raise

        return await asyncio.gather(*(guard(t) for t in thunks))

    async def pipeline(self, items: list[Any], *stages: Stage,
                       isolate: bool = True) -> list[Any]:
        """Run each item through ``stages`` independently (no inter-stage barrier).

        A stage returning/raising drops that item to ``None`` and skips its remaining
        stages, so one bad item never stalls the batch."""
        if not stages:
            raise WorkflowError("pipeline requires at least one stage")

        async def chain(item: Any) -> Any:
            cur = item
            for stage in stages:
                try:
                    cur = await stage(cur)
                except Exception:  # noqa: BLE001 - logged upstream; isolate the item
                    if isolate:
                        return None
                    raise
                if cur is None:
                    return None
            return cur

        return await asyncio.gather(*(chain(i) for i in items))

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
