"""Shared context for workflow stages: services plus the identifiers a stage is scoped to."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from ..config import Settings
from ..llm import LLMClient
from ..observability import ProgressReporter, RunLogger


@dataclass
class AgentContext:
    """Shared services + the identifiers a stage is currently scoped to.

    Cheap to :meth:`bind` per db/record; the bound ``log`` carries those identifiers so
    every event/anomaly is attributable without the stage threading them by hand.
    """

    settings: Settings
    llm: LLMClient
    log: RunLogger
    progress: ProgressReporter | None = None
    source: Any = None                      # BirdSource (Phase A); avoids an import cycle
    mongo: Any = None                       # MongoExecutor
    db_id: str | None = None
    record_id: int | None = None
    phase: str = "A"
    group: str | None = None                # progress group id (defaults to db_id)
    work_item_id: str | None = None         # caller-supplied progress task discriminator
    log_mgr: Any = None                     # tend.utils.logging.LogManager (solver/baseline/ablation runs)
    extra: dict[str, Any] = field(default_factory=dict)

    def bind(self, **fields: Any) -> "AgentContext":
        log = self.log
        binders = {k: v for k, v in fields.items()
                   if k in ("db_id", "record_id", "phase") and v is not None}
        if binders:
            log = log.bind(**binders)
        return replace(self, log=log, **{k: v for k, v in fields.items()
                                         if k in {f.name for f in self.__dataclass_fields__.values()}})
