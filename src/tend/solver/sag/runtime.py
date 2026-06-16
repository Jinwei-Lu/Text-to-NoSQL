"""SAG solve loop: gated decode → execution-grounded repair → result-consistency vote.

Per record: (1) the per-db :class:`GroundingIndex` (built once, cached for the run)
renders the complete hypothesis space into the system prompt; (2) each of k attempts
runs a bounded repair loop — decode strict JSON, gate with A_path ∧ A_value + the
limit contract, execute when clean, feed empty-result bisection / synthetic-_id
findings back; (3) the best candidate per attempt is the (violations, empty, -round)
minimum; (4) with k>1 the final answers are clustered by RESULT equivalence
(order-insensitive) and the largest cluster wins.

The solver sees ONLY ``NLQ + read-only world``: gold MQL, canonical_form_set,
shape_policy, and difficulty never enter this module (release-record mode extracts
just db_id/record_id/NLQ). Concurrency is governed solely by the central LLM client
semaphore; every pymongo call is dispatched through ``asyncio.to_thread``.

Observability is DynaDB-style (``tend.utils.logging``): the k-attempt solve for one
record is ONE agent session in a :class:`TaskLogger` under
``<stage>/<db_id>/<record_id>`` — every decode/repair round is one agent turn, and
anomalies land in ``errors.jsonl`` via ``LogManager.log_exception_event``.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ...errors import ConfigError, ExecutionError, LLMError, TendError
from ...execution.ast_check import render_mql, scan_disabled
from ...execution.mongo import equiv_rec_values
from ...utils.logging import AgentTurnLogPayload, LogManager, TaskLogger
from ..inputs import _canonical_nlq
from .gates import ProbeCache, a_path, a_value, limit_contract
from .induction import GroundingIndex, build_grounding_index
from .prompt import response_schema, system_prompt, witness_block
from .repair import bisect_empty, run_pipeline, synthetic_id_violation
from .witness import EnforcedLiteral, witnesses
from .world import LocalWorld, MongoWorld, WorldAccess

_ARMS = ("v3", "v2", "gate", "card1")
_CARD_MODES = ("lattice", "toplevel", "nocollapse")


@dataclass(frozen=True)
class SAGPolicy:
    """Mechanism configuration. ``arm`` selects the cumulative mechanism ladder:

    - ``card1``: path card only, single shot (no gate, no repair, k=1)
    - ``gate``: + A_path gate + execution repair (plain empty feedback, k=1)
    - ``v2``:   + value witnesses, A_value, limit contract, bisection (k=1)
    - ``v3``:   + k-sample result-consistency clustering (the full solver)

    The ``*_override`` knobs and ``card_mode`` back the extended component-knockout
    ablations (docs/experiment_design_2026-06.md §4.2): each knockout is v3 minus
    exactly one component, decoupled from the cumulative ladder order. ``None``
    means "derived from ``arm``" — the four canonical arms never set them.
    """

    arm: str = "v3"
    k_consistency: int = 3
    max_repair_rounds: int = 6
    sample_docs: int = 400
    card_cap: int = 400
    exec_timeout_ms: int = 20_000
    stage_count_timeout_ms: int = 15_000
    edge_probe_timeout_ms: int = 4_000
    distinct_sample_k: int = 8
    # --- extended-ablation surface (None / defaults = arm-derived behavior) --- #
    gate_override: bool | None = None  # A_path admissibility gate
    value_grounding_override: bool | None = None  # witnesses + A_value + limit contract
    bisection_override: bool | None = None  # rich empty feedback (prefix bisection)
    card_mode: str = "lattice"  # "lattice" | "toplevel" | "nocollapse" (card TEXT only)
    variant_label: str = ""  # distinguishes knockout arms in variants/transcripts

    def validate(self) -> None:
        if self.arm not in _ARMS:
            raise ConfigError(
                f"unknown SAG arm {self.arm!r}; valid arms: {sorted(_ARMS)}",
                context={"arm": self.arm},
            )
        if self.card_mode not in _CARD_MODES:
            raise ConfigError(
                f"unknown card_mode {self.card_mode!r}; valid modes: {sorted(_CARD_MODES)}",
                context={"card_mode": self.card_mode},
            )
        if self.k_consistency < 1 or self.max_repair_rounds < 1:
            raise ConfigError(
                "k_consistency and max_repair_rounds must be >= 1",
                context={
                    "k_consistency": self.k_consistency,
                    "max_repair_rounds": self.max_repair_rounds,
                },
            )
        if self.sample_docs < 1 or self.card_cap < 1:
            raise ConfigError(
                "sample_docs and card_cap must be >= 1",
                context={"sample_docs": self.sample_docs, "card_cap": self.card_cap},
            )

    @property
    def use_gate(self) -> bool:
        if self.gate_override is not None:
            return self.gate_override
        return self.arm != "card1"

    @property
    def use_repair(self) -> bool:
        return self.arm != "card1"

    @property
    def use_value_witnesses(self) -> bool:
        """Drives the 'smart' feedback set: A_value, limit contract, synthetic-_id
        (the prototype keyed them on the same arm membership)."""
        if self.value_grounding_override is not None:
            return self.value_grounding_override
        return self.arm in ("v2", "v3")

    @property
    def use_bisection(self) -> bool:
        """Rich empty-result feedback content (prefix bisection + distinct values);
        when off, an empty result feeds back as a plain 'returns 0 rows'."""
        if self.bisection_override is not None:
            return self.bisection_override
        return self.use_value_witnesses

    @property
    def effective_k(self) -> int:
        return self.k_consistency if self.arm == "v3" else 1

    @property
    def solver_variant(self) -> str:
        base = f"sag_{self.arm}"
        return f"{base}_{self.variant_label}" if self.variant_label else base


@dataclass
class SAGPrediction:
    db_id: str
    record_id: str | int | None
    nlq: str
    collection: str
    pipeline: list[dict[str, Any]]
    MQL: str
    solver_variant: str
    rounds: int
    samples: int
    cluster_size: int
    violations_final: int
    empty_final: bool
    exec_status: str  # "ok" | "skipped" (offline world)
    disclosure: dict[str, Any]
    agent_session_ref: str = ""
    result_type: str = "solver_prediction"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SAGFailure:
    db_id: str
    record_id: str | int | None
    nlq: str
    error_code: str
    message: str
    solver_variant: str
    MQL: str = ""
    rounds: int = 0
    samples: int = 0
    disclosure: dict[str, Any] = field(default_factory=dict)
    agent_session_ref: str = ""
    result_type: str = "solver_failure"

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    collection: str
    pipeline: list[dict[str, Any]]
    violations: int
    empty: int  # 0/1
    round: int


@dataclass
class AttemptOutcome:
    candidate: Candidate
    rounds: int
    result: list[dict[str, Any]] | None  # normalized final execution
    exec_status: str  # "ok" | "error" | "skipped"
    error_code: str | None = None
    error_message: str | None = None
    feedback_log: list[list[str]] = field(default_factory=list)


def select_best(cands: list[Candidate]) -> Candidate:
    """Fewest violations, then non-empty, then the LATEST round (ties impossible:
    one candidate per round). Key-function min — Candidate objects are unorderable."""
    return min(cands, key=lambda c: (c.violations, c.empty, -c.round))


def cluster_attempts(outs: list[AttemptOutcome]) -> tuple[AttemptOutcome, int]:
    """Cluster final answers by RESULT equivalence (order-insensitive), pick the
    largest cluster; representative = fewest violations, non-empty, earliest."""
    clusters: list[list[int]] = []
    for i, o in enumerate(outs):
        placed = False
        for cl in clusters:
            ref = outs[cl[0]]
            if (
                o.result is not None
                and ref.result is not None
                and equiv_rec_values(o.result, ref.result, order_sensitive=False)
            ):
                cl.append(i)
                placed = True
                break
        if not placed:
            clusters.append([i])
    clusters.sort(
        key=lambda cl: (
            -len(cl),
            min((outs[i].candidate.violations, outs[i].candidate.empty, i) for i in cl),
        )
    )
    members = clusters[0]
    best_i = min(
        members, key=lambda i: (outs[i].candidate.violations, outs[i].candidate.empty, i)
    )
    return outs[best_i], len(members)


# --------------------------------------------------------------------------- #
# DynaDB-style logging plumbing
# --------------------------------------------------------------------------- #
_RESERVED_EXCEPTION_KW = {
    "stage",
    "task_id",
    "anomaly",
    "session_path",
    "log_path",
    "recoverable",
    "cached",
    "acceptance_phase",
    "_level",
    "error",
    "error_type",
    "error_message",
    "event",
    "error_index",
}


def log_sag_anomaly(
    log_mgr: Any,
    event_name: str,
    exc: BaseException,
    *,
    stage: str,
    task_id: str,
    session_path: Any = None,
) -> None:
    """Record a typed solver exception in ``errors.jsonl`` (DynaDB error index).

    ``TendError`` context and anomaly classification are flattened into the
    payload; reserved payload keys are filtered so the structured summary wins.
    """
    if log_mgr is None:
        return
    extra = {
        k: v
        for k, v in getattr(exc, "context", {}).items()
        if k not in _RESERVED_EXCEPTION_KW
    }
    anomaly = getattr(exc, "anomaly", None)
    log_mgr.log_exception_event(
        event_name,
        exc,
        stage=stage,
        task_id=task_id,
        anomaly=(anomaly.value if anomaly is not None else None),
        session_path=session_path,
        **extra,
    )
    if isinstance(exc, TendError):
        exc.logged = True


class _SessionState:
    """Shared turn/token tally for the ONE agent session covering all k attempts."""

    __slots__ = ("turns", "total_tokens", "max_turns")

    def __init__(self, max_turns: int) -> None:
        self.turns = 0
        self.total_tokens = 0
        self.max_turns = max_turns


def _session_ref(task_log: TaskLogger | None, log_mgr: Any) -> str:
    """Relative path of the record's agent session file under the run dir."""
    path = getattr(task_log, "agent_session_path", None)
    root = getattr(log_mgr, "root", None)
    if path is None or root is None:
        return ""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _session_outcome(outs: list[AttemptOutcome], best: AttemptOutcome | None) -> tuple[str, bool]:
    if not outs or best is None:
        return "error", False
    if best.exec_status == "error":
        return "error", False
    if best.candidate.violations:
        return "gate_failed", True
    if best.candidate.empty:
        return "empty", True
    return "submitted_clean", True


# --------------------------------------------------------------------------- #
# per-db index cache
# --------------------------------------------------------------------------- #
class GroundingIndexCache:
    """One immutable :class:`GroundingIndex` + probe cache per db, per run.

    World selection: stub mode never touches Mongo (offline induction from the
    in-memory witness data); otherwise Mongo wins whenever reachable — with
    ``TEND_USE_EXISTING_MONGO_DBS=1`` the on-disk witness may be a stub shadow,
    so the physical database is authoritative.
    """

    def __init__(self, mongo: Any, settings: Any, log: Any) -> None:
        self._mongo = mongo
        self._settings = settings
        self._log = log  # StageLogger (or anything with .info) for index-build events
        # Keyed by (db_id, card-affecting policy params): an ablation suite shares one
        # cache across arms, and card-mode/sample/cap variants must not poison each other.
        self._entries: dict[
            tuple[str, int, int, str], tuple[GroundingIndex, ProbeCache, WorldAccess]
        ] = {}
        self._locks: dict[tuple[str, int, int, str], asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    @staticmethod
    def _key(db_id: str, policy: SAGPolicy) -> tuple[str, int, int, str]:
        return (db_id, policy.sample_docs, policy.card_cap, policy.card_mode)

    async def _lock_for(self, key: tuple[str, int, int, str]) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(key, asyncio.Lock())

    def _select_world(
        self, db_id: str, local_data: dict[str, list[dict[str, Any]]] | None
    ) -> WorldAccess:
        stub = bool(getattr(self._settings, "stub", False))
        if not stub and self._mongo is not None:
            available = getattr(self._mongo, "available", None)
            if not callable(available) or available():
                return MongoWorld(self._mongo, db_id)
        if local_data:
            return LocalWorld(db_id, local_data)
        raise ExecutionError(
            "no data source for the grounding index (Mongo unavailable and no witness data)",
            context={"db_id": db_id, "stub": stub, "sag_error_code": "EXECUTION_UNAVAILABLE"},
        )

    async def get(
        self,
        db_id: str,
        *,
        policy: SAGPolicy,
        local_data: dict[str, list[dict[str, Any]]] | None = None,
    ) -> tuple[GroundingIndex, ProbeCache, WorldAccess]:
        key = self._key(db_id, policy)
        lock = await self._lock_for(key)
        async with lock:
            if key in self._entries:
                return self._entries[key]
            world = await asyncio.to_thread(self._select_world, db_id, local_data)
            t0 = time.monotonic()
            try:
                index = await asyncio.to_thread(
                    build_grounding_index,
                    world,
                    sample_docs=policy.sample_docs,
                    card_cap=policy.card_cap,
                    card_mode=policy.card_mode,
                )
            except ExecutionError as err:
                raise err.with_context(db_id=db_id, sag_error_code="INDEX_BUILD_FAILED")
            info = getattr(self._log, "info", None)
            if callable(info):
                info(
                    "sag_index_built",
                    db_id=db_id,
                    source=index.source,
                    card_mode=policy.card_mode,
                    elapsed_s=round(time.monotonic() - t0, 3),
                    **index.stats,
                )
            self._entries[key] = (index, ProbeCache(), world)
            return self._entries[key]


# --------------------------------------------------------------------------- #
# one attempt = one full repair-loop decode
# --------------------------------------------------------------------------- #
async def _run_attempt(
    llm: Any,
    world: WorldAccess,
    index: GroundingIndex,
    cache: ProbeCache,
    policy: SAGPolicy,
    *,
    sys_text: str,
    user_text: str,
    nlq: str,
    enforce: dict[str, EnforcedLiteral],
    agent: str,
    attempt: int,
    task_log: TaskLogger | None = None,
    session: _SessionState | None = None,
) -> AttemptOutcome | None:
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": sys_text},
        {"role": "user", "content": user_text},
    ]
    schema = response_schema(index)
    cands: list[Candidate] = []
    feedback_log: list[list[str]] = []
    rounds = 0
    max_rounds = policy.max_repair_rounds if (policy.use_repair and world.can_execute) else 1
    for rounds in range(1, max_rounds + 1):
        try:
            res = await llm.complete(
                agent=agent,
                messages=msgs,
                schema=schema,
                temperature=0.0,
                omit_max_tokens=True,
                task_logger=task_log,
            )
        except LLMError:
            # already logged as an anomaly by the LLM client
            break
        coll = str(res.data["collection"])
        pipe = list(res.data["pipeline"])

        def log_turn(fb_lines: list[str], *, _res: Any = res, _round: int = rounds) -> None:
            """One decode/repair round = one agent turn in the record's session."""
            if task_log is None or session is None:
                return
            usage = dict(getattr(_res, "usage", None) or {})
            session.turns += 1
            session.total_tokens += int(
                usage.get("total_tokens")
                or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
            )
            tool_results = (
                [
                    {
                        "tool_call_id": f"attempt{attempt}_round{_round}",
                        "content": "gate/execution feedback:\n- " + "\n- ".join(fb_lines),
                    }
                ]
                if fb_lines
                else None
            )
            task_log.log_agent_turn(
                AgentTurnLogPayload(
                    turn=session.turns,
                    max_turns=session.max_turns,
                    reasoning=None,
                    assistant_content=(str(getattr(_res, "text", "") or "").strip() or None),
                    tool_calls=None,
                    tool_results=tool_results,
                    usage=usage or None,
                    cost_usd=0.0,
                    cost_source=None,
                )
            )

        fb: list[str] = []
        if policy.use_gate:
            fb += a_path(
                world,
                index,
                cache,
                coll,
                pipe,
                edge_probe_timeout_ms=policy.edge_probe_timeout_ms,
            )
        if policy.use_value_witnesses and not fb:
            fb += a_value(index, coll, pipe, enforce)
            fb += limit_contract(nlq, pipe)
        emptied = 0
        if not policy.use_repair:  # card1: accept the single shot, no in-loop execution
            cands.append(Candidate(coll, pipe, 0, 0, rounds))
            log_turn([])
            break
        if not fb:
            banned = scan_disabled(render_mql(coll, pipe))
            if banned:
                fb.append(
                    f"the pipeline uses disabled operator(s) {sorted(banned)} — these are "
                    f"banned in this environment; rewrite without them."
                )
            elif world.can_execute:
                try:
                    r = await asyncio.to_thread(
                        run_pipeline, world, coll, pipe, timeout_ms=policy.exec_timeout_ms
                    )
                    if not r:
                        emptied = 1
                        bs = (
                            await asyncio.to_thread(
                                bisect_empty,
                                world,
                                index,
                                coll,
                                pipe,
                                stage_timeout_ms=policy.stage_count_timeout_ms,
                                distinct_k=policy.distinct_sample_k,
                            )
                            if policy.use_bisection
                            else None
                        )
                        fb.append(
                            "the query executes but returns 0 rows." + (f" {bs}" if bs else "")
                        )
                    elif policy.use_value_witnesses:
                        sid = synthetic_id_violation(r)
                        if sid:
                            fb.append(sid)
                except ExecutionError as exc:
                    emptied = 1
                    detail = str(exc.context.get("error") or exc.message)
                    fb.append(f"execution error: {detail[:140]}")
        cands.append(Candidate(coll, pipe, len(fb), emptied, rounds))
        feedback_log.append(list(fb))
        log_turn(fb)
        if not fb:
            break
        msgs += [
            {
                "role": "assistant",
                "content": json.dumps({"collection": coll, "pipeline": pipe}),
            },
            {
                "role": "user",
                "content": "Revise. Problems:\n- "
                + "\n- ".join(fb)
                + "\nReturn corrected JSON (same task).",
            },
        ]
    if not cands:
        return None
    best = select_best(cands)
    exec_status, result = "skipped", None
    error_code: str | None = None
    error_message: str | None = None
    if world.can_execute:
        banned = scan_disabled(render_mql(best.collection, best.pipeline))
        if banned:
            exec_status = "error"
            error_code = "DISABLED_OPERATOR"
            error_message = f"final pipeline uses disabled operator(s) {sorted(banned)}"
        else:
            try:
                result = await asyncio.to_thread(
                    run_pipeline,
                    world,
                    best.collection,
                    best.pipeline,
                    timeout_ms=policy.exec_timeout_ms,
                )
                exec_status = "ok"
            except ExecutionError as exc:
                exec_status = "error"
                error_code = "PRED_EXEC_ERROR"
                error_message = str(exc.context.get("error") or exc.message)[:300]
    return AttemptOutcome(
        candidate=best,
        rounds=rounds,
        result=result,
        exec_status=exec_status,
        error_code=error_code,
        error_message=error_message,
        feedback_log=feedback_log,
    )


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #
async def sag_solve_nlq_db(
    wf: Any | None = None,
    *,
    llm: Any | None = None,
    world: WorldAccess | None = None,
    db_id: str,
    nlq: str,
    record_id: str | int | None = None,
    policy: SAGPolicy | None = None,
    index_cache: GroundingIndexCache | None = None,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    stage: str = "solve",
    task_log: TaskLogger | None = None,
) -> SAGPrediction | SAGFailure:
    """Solve from NLQ + read-only database world.

    ``wf`` supplies llm/mongo/log_mgr/settings via ``wf.ctx`` (the CLI path); tests
    may inject ``llm`` and either a prebuilt ``index_cache`` or a raw ``world``
    directly. When ``wf.ctx.log_mgr`` is a :class:`LogManager`, the record's whole
    k-attempt solve is recorded as ONE agent session under
    ``<stage>/llm/`` with a per-record task log ``<stage>/<db_id>/<record_id>.log``;
    callers (e.g. the ablation workflow) may pass a prebuilt ``task_log``.
    """
    ctx = wf.ctx if wf is not None else None
    llm = llm or getattr(ctx, "llm", None)
    log = getattr(ctx, "log", None)  # StageLogger on solve/ablation runs
    log_mgr: LogManager | None = getattr(ctx, "log_mgr", None)
    settings = getattr(ctx, "settings", None)
    mongo = getattr(ctx, "mongo", None)
    if llm is None:
        raise TypeError("sag_solve_nlq_db requires llm or wf.ctx.llm")
    policy = policy or SAGPolicy()
    policy.validate()
    db_id = str(db_id)
    task_id = f"{db_id}/{record_id if record_id is not None else 'na'}"
    if task_log is None and log_mgr is not None:
        task_log = log_mgr.get_task_logger(stage, task_id)
    if task_log is not None and not task_log.step_label:
        task_log.set_step_label(f"{db_id}_{record_id}_{policy.solver_variant}")

    def lifecycle(event: str, **kw: Any) -> None:
        sink = task_log if task_log is not None else log
        info = getattr(sink, "info", None)
        if callable(info):
            info(event, db_id=db_id, record_id=record_id, **kw)

    # ---- grounding index ------------------------------------------------- #
    try:
        if world is not None:
            index = await asyncio.to_thread(
                build_grounding_index,
                world,
                sample_docs=policy.sample_docs,
                card_cap=policy.card_cap,
                card_mode=policy.card_mode,
            )
            probe_cache = ProbeCache()
        else:
            if index_cache is None:
                index_cache = GroundingIndexCache(mongo, settings, log)
            index, probe_cache, world = await index_cache.get(
                db_id, policy=policy, local_data=local_data
            )
    except ExecutionError as err:
        if not err.logged:
            log_sag_anomaly(
                log_mgr, "sag_index_build_failed", err, stage=stage, task_id=task_id
            )
        return SAGFailure(
            db_id=db_id,
            record_id=record_id,
            nlq=nlq,
            error_code=str(err.context.get("sag_error_code") or "INDEX_BUILD_FAILED"),
            message=err.message,
            solver_variant=policy.solver_variant,
            disclosure=_disclosure(policy, None, 0),
        )

    # ---- witnesses + attempts -------------------------------------------- #
    ev_lines, enforce = witnesses(nlq, index) if policy.use_value_witnesses else ([], {})
    evidence_text = witness_block(ev_lines)
    sys_text = system_prompt(index)
    user_text = f"Question: {nlq}{evidence_text}\n\nReturn the JSON object."
    k = policy.effective_k if world.can_execute else 1
    max_rounds = policy.max_repair_rounds if policy.use_repair else 1
    session = _SessionState(max_turns=k * max_rounds)
    if task_log is not None:
        task_log.open_agent_session(
            model=str(getattr(getattr(settings, "llm", None), "model", "") or "stub"),
            system_prompt=sys_text,
            user_message=user_text,
            tools=None,
        )
    outs_raw = await asyncio.gather(
        *[
            _run_attempt(
                llm,
                world,
                index,
                probe_cache,
                policy,
                sys_text=sys_text,
                user_text=user_text,
                nlq=nlq,
                enforce=enforce,
                agent=f"{policy.solver_variant}_{i}",
                attempt=i,
                task_log=task_log,
                session=session,
            )
            for i in range(k)
        ]
    )
    outs = [o for o in outs_raw if o is not None]
    disclosure = _disclosure(policy, index, len(enforce))

    if not outs:
        best, cluster_size = None, 0
    elif len(outs) == 1:
        best, cluster_size = outs[0], 1
    else:
        best, cluster_size = cluster_attempts(outs)

    outcome, completed = _session_outcome(outs, best)
    if task_log is not None:
        task_log.close_agent_session(
            turns=session.turns,
            tool_calls_made=0,
            total_tokens=session.total_tokens,
            completed=completed,
            # non-submitted outcomes are outside the renderer's token set; pass
            # them through ``reason`` so the literal token survives in the footer
            reason=None if outcome == "submitted_clean" else outcome,
            outcome=outcome,
        )
    session_ref = _session_ref(task_log, log_mgr)

    if best is None:
        lifecycle("sag_no_candidate", arm=policy.arm, samples_requested=k, stage=stage)
        return SAGFailure(
            db_id=db_id,
            record_id=record_id,
            nlq=nlq,
            error_code="LLM_ERROR",
            message="all attempts failed before producing a candidate (see anomaly stream)",
            solver_variant=policy.solver_variant,
            samples=0,
            disclosure=disclosure,
            agent_session_ref=session_ref,
        )
    mql = render_mql(best.candidate.collection, best.candidate.pipeline)
    if best.exec_status == "error":
        return SAGFailure(
            db_id=db_id,
            record_id=record_id,
            nlq=nlq,
            error_code=best.error_code or "PRED_EXEC_ERROR",
            message=best.error_message or "final pipeline execution failed",
            solver_variant=policy.solver_variant,
            MQL=mql,
            rounds=best.rounds,
            samples=len(outs),
            disclosure=disclosure,
            agent_session_ref=session_ref,
        )
    prediction = SAGPrediction(
        db_id=db_id,
        record_id=record_id,
        nlq=nlq,
        collection=best.candidate.collection,
        pipeline=best.candidate.pipeline,
        MQL=mql,
        solver_variant=policy.solver_variant,
        rounds=best.rounds,
        samples=len(outs),
        cluster_size=cluster_size,
        violations_final=best.candidate.violations,
        empty_final=bool(best.candidate.empty),
        exec_status=best.exec_status,
        disclosure=disclosure,
        agent_session_ref=session_ref,
    )
    lifecycle(
        "sag_solved",
        arm=policy.arm,
        rounds=best.rounds,
        samples=len(outs),
        cluster_size=cluster_size,
        violations_final=best.candidate.violations,
        empty_final=bool(best.candidate.empty),
        exec_status=best.exec_status,
        collection=best.candidate.collection,
        stage=stage,
        outcome=outcome,
        agent_session_ref=session_ref,
    )
    return prediction


async def sag_solve_record(
    wf: Any,
    record: dict[str, Any],
    schema: dict[str, Any] | None = None,
    *,
    local_data: dict[str, list[dict[str, Any]]] | None = None,
    policy: SAGPolicy | None = None,
    index_cache: GroundingIndexCache | None = None,
    witness_preloaded: bool = False,
    stage: str = "solve",
    task_log: TaskLogger | None = None,
) -> SAGPrediction | SAGFailure:
    """Release-record shim: ONLY NLQ/db_id/record_id cross the solver boundary.

    Gold MQL, canonical_form_set, shape_policy, and difficulty are never read.
    ``schema`` is accepted for call-site parity and ignored.
    """
    del schema
    db_id = str(record.get("db_id") or "")
    ctx = getattr(wf, "ctx", None)
    settings = getattr(ctx, "settings", None)
    mongo = getattr(ctx, "mongo", None)
    stub = bool(getattr(settings, "stub", False))
    if local_data and not witness_preloaded and mongo is not None and not stub:
        load_witness = getattr(mongo, "load_witness", None)
        if callable(load_witness):
            available = getattr(mongo, "available", None)
            reachable = await asyncio.to_thread(available) if callable(available) else True
            if reachable:
                await asyncio.to_thread(load_witness, db_id, local_data)
    return await sag_solve_nlq_db(
        wf,
        db_id=db_id,
        nlq=_canonical_nlq(record),
        record_id=record.get("record_id"),
        policy=policy,
        index_cache=index_cache,
        local_data=local_data,
        stage=stage,
        task_log=task_log,
    )


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _disclosure(policy: SAGPolicy, index: GroundingIndex | None, witnessed: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "uses_gold_mql": False,
        "arm": policy.arm,
        "solver_variant": policy.solver_variant,
        "k_consistency": policy.effective_k,
        "max_repair_rounds": policy.max_repair_rounds if policy.use_repair else 1,
        "uses_path_card": True,
        "card_mode": policy.card_mode,
        "uses_a_path_gate": policy.use_gate,
        "uses_value_witnesses": policy.use_value_witnesses,
        "uses_bisection_feedback": policy.use_bisection and policy.use_repair,
        "uses_consistency": policy.arm == "v3" and policy.effective_k > 1,
        "witnessed_literals": witnessed,
    }
    if index is not None:
        out["index_source"] = index.source
        out.update({f"index_{k}": v for k, v in index.stats.items()})
    return out
