"""TEND construction DAGs expressed with the workflow primitives.

These are the *dynamic* graphs: the work-list (which dbs, which coverage slots) is
discovered at runtime, and feedback edges (SC->SRA, PV->MS, RTV->NLP, NNC->QPS, RA->MS)
are bounded retry loops. Inter-agent data is passed as plain dicts keyed by the agent I/O
contracts in proposals/04; verdict fields (``verdict``, ``pv_pass``, ``gate_pass``, ...)
drive the loops. Heavy deterministic checks (NormExec, gold-lock) run *inside* the agents
that own them, so the flow stays a readable coordinator.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from ..agents import AgentContext
from ..agents.phase_b import _canonical_reference_mql
from ..execution import (
    derive_canonical_form_set,
    mql_signature,
    mql_skeleton_signature,
    mql_skeleton_summary,
    parse_pipeline,
    scan_disabled,
)
from ..errors import TendError, wrap_unexpected
from .engine import Workflow

SC_MAX_ROUNDS = 2          # SC reject -> SRA revise, at most twice (04-1-2 / 03-II-4)
RTV_MAX_ROUNDS = 2         # RTV canonical fail -> NLP rewrite (04-1-2-3)
MS_MAX_ROUNDS = 2          # gold-lock fail -> re-synthesize / re-sample intent
MUT_MAX_ROUNDS = 2         # PV reject -> MUT regenerate discriminating mutations
RA_MAX_ROUNDS = 2          # P4 augment loop
MQL_SKELETON_FAMILY_CAP = 16


def _task_failed(task: "asyncio.Task[Any]") -> bool:
    """True if a *done* task ended in cancellation or an exception (never raises)."""
    if not task.done() or task.cancelled():
        return True
    return task.exception() is not None


def _drop(ctx, stage: str, reason: str, **detail: Any) -> None:
    """Log a (expected) record drop with its cause so the reason is never silent."""
    ctx.log.warning("record_dropped", stage=stage, reason=reason,
                    record_id=ctx.record_id, **detail)
    return None


def _log_branch_exception(ctx: AgentContext, exc: BaseException, *, stage: str) -> None:
    if isinstance(exc, TendError) and exc.logged:
        ctx.log.warning(
            "branch_failed",
            stage=stage,
            already_logged=True,
            error_type=type(exc).__name__,
            message=exc.message,
            anomaly=exc.anomaly.value if exc.anomaly else None,
            context=exc.context,
            transcript_ref=exc.context.get("transcript_ref"),
            diagnostics_ref=exc.context.get("diagnostics_ref"),
        )
        return
    ctx.log.anomaly(wrap_unexpected(exc, stage=stage))


@dataclass
class DbArtifacts:
    """Phase A output for one db (the Tier-1 library assets + WP context)."""

    db_id: str
    mongodb_schema: dict[str, Any]
    mongodb_data: dict[str, Any]
    rationale: dict[str, Any]
    world_signature: str
    scenario_summary: str
    query_bearing: bool
    domain_id: str = "unknown"
    sqlite_path: str = ""
    table_count: int = 0
    query_count: int = 0
    wp_output: dict[str, Any] = field(default_factory=dict)


@dataclass
class CoverageSlot:
    """One (db, mechanism, archetype) cell the coverage controller wants filled."""

    db_id: str
    mechanism: str
    archetype: str
    record_id: int
    target_difficulty: str = "L4"
    target_sql_infeasibility_class: str = "structural_schema_flex"
    target_schema_flex: str = "polymorphic"
    slot_index: int = 0
    diversity_key: str = ""
    diversity_hint: str = ""
    schema_feature: str = ""
    reference_oracle_seed: dict[str, Any] | None = None
    intent_seed: dict[str, Any] | None = None


@dataclass
class DiversityLedger:
    """Shared per-Phase-B portfolio state for concurrent construction.

    The ledger gives QPS a global view before it spends an LLM call. Exact MQL, skeleton,
    and NL identities are reserved only after the artifacts that define them exist.
    """

    seen_mql: dict[tuple[str, str], int]
    seen_skeleton: dict[tuple[str, str], list[int]]
    lock: asyncio.Lock
    skeleton_cap: int = MQL_SKELETON_FAMILY_CAP
    slot_counts: Counter[tuple[str, str, str]] = field(default_factory=Counter)
    slot_first_record: dict[tuple[str, str], int] = field(default_factory=dict)
    seen_canonical_nl: dict[tuple[str, str], int] = field(default_factory=dict)
    seen_nl_mql_pair: dict[tuple[str, str, str], int] = field(default_factory=dict)

    async def reserve_slot(self, ctx: AgentContext, slot: CoverageSlot) -> dict[str, Any]:
        axes = _slot_diversity_axes(slot)
        async with self.lock:
            duplicate_of: int | None = None
            if slot.diversity_key:
                key = (slot.db_id, slot.diversity_key)
                duplicate_of = self.slot_first_record.get(key)
                if duplicate_of is None:
                    self.slot_first_record[key] = slot.record_id
            before = {
                axis: self.slot_counts[(slot.db_id, axis, value)]
                for axis, value in axes.items()
                if value
            }
            if duplicate_of is None:
                for axis, value in axes.items():
                    if value:
                        self.slot_counts[(slot.db_id, axis, value)] += 1
            after = {
                axis: self.slot_counts[(slot.db_id, axis, value)]
                for axis, value in axes.items()
                if value
            }
        context = {
            "slot_axes": axes,
            "same_axis_counts_before": before,
            "same_axis_counts_after_reservation": after,
            "duplicate_of_record_id": duplicate_of,
            "instruction": (
                "Use the slot axes as portfolio pressure. When counts are already non-zero, "
                "avoid a near-copy: change the business grain, branch semantics, grouping unit, "
                "or multi-stage structure instead of only swapping a field or accumulator."
            ),
        }
        ctx.log.info(
            "diversity_slot_reserved",
            record_id=slot.record_id,
            duplicate_of_record_id=duplicate_of,
            axes=axes,
            same_axis_counts_before=before,
        )
        return context

    async def reserve_mql_identity(self, ctx: AgentContext, slot: CoverageSlot, mql: str) -> dict[str, Any]:
        return await _reserve_mql_identity(
            ctx,
            slot,
            mql,
            seen_mql=self.seen_mql,
            seen_skeleton=self.seen_skeleton,
            mql_lock=self.lock,
            skeleton_cap=self.skeleton_cap,
        )

    async def reserve_nl_identity(
        self,
        ctx: AgentContext,
        slot: CoverageSlot,
        nl_queries: dict[str, Any],
        mql_sig: str,
    ) -> dict[str, Any]:
        return await _reserve_nl_identity(
            ctx,
            slot,
            nl_queries,
            mql_sig,
            seen_canonical_nl=self.seen_canonical_nl,
            seen_nl_mql_pair=self.seen_nl_mql_pair,
            nl_lock=self.lock,
        )


# --------------------------------------------------------------------------- #
# Phase A - DataWorld construction (per db: WP || DM -> SRA -> SC*)
# --------------------------------------------------------------------------- #
async def run_phase_a(wf: Workflow, db_ids: list[str]) -> dict[str, DbArtifacts]:
    """Build Tier-1 assets for each db, in parallel across dbs."""
    wf.phase("A")
    for db_id in db_ids:
        if wf.ctx.progress:
            wf.ctx.progress.add_group(db_id, db_id, phase="A", total=4)

    results = await wf.parallel(
        [lambda db=db: _phase_a_one_db(wf, db) for db in db_ids], isolate=True
    )
    return {a.db_id: a for a in results if isinstance(a, DbArtifacts)}


async def _phase_a_one_db(wf: Workflow, db_id: str) -> DbArtifacts:
    ctx = wf.context(db_id=db_id, group=db_id)
    ctx.log.info("phase_a_db_start", db_id=db_id)

    wp_task = asyncio.create_task(wf.agent("wp", {"db_id": db_id}, ctx=ctx))
    dm_task = asyncio.create_task(wf.agent("dm", {"db_id": db_id}, ctx=ctx))
    try:
        wp = await wp_task
        assert wp is not None  # non-isolated within the db chain; failure aborts this db

        sra = await wf.agent("sra", {"wp_output": wp, "db_id": db_id}, ctx=ctx)
        assert sra is not None

        dm = await dm_task
        assert dm is not None
    except Exception:
        for task in (wp_task, dm_task):
            if not task.done():
                task.cancel()
        results = await asyncio.gather(wp_task, dm_task, return_exceptions=True)
        for exc in results:
            if isinstance(exc, Exception) and not isinstance(exc, asyncio.CancelledError):
                _log_branch_exception(ctx, exc, stage="phase_a_branch")
        raise

    # DM's schema is authoritative (consistent with the migrated data by construction, Gate-SD)
    source_schema = ctx.source.schema(db_id) if ctx.source else None
    workload = ctx.source.workload(db_id) if ctx.source else []
    schema = dm.get("mongodb_schema", {})
    sc_inputs = {
        "db_id": db_id,
        "mongodb_schema": schema,
        "mongodb_data": dm.get("mongodb_data", {}),
        "migration_log": dm.get("migration_log", {}),
        "sra_rationale": sra.get("agent_design_rationale", {}),
        "wp_output": wp,
        "query_evidence": _workload_evidence(workload),
    }
    # SC reviews DM's materialized artifacts. A reject can ask SRA to clarify rationale,
    # but SRA's proposed schema is no longer a source of truth for downstream construction.
    sc = await wf.agent("sc", sc_inputs, ctx=ctx)
    rounds = 0
    while sc and sc.get("verdict") == "reject" and rounds < SC_MAX_ROUNDS:
        rounds += 1
        ctx.log.warning("sc_reject", round=rounds, issues=sc.get("issues"))
        sra = await wf.agent(
            "sra",
            {
                "wp_output": wp,
                "db_id": db_id,
                "sc_fixes": sc.get("suggested_fixes"),
                "dm_review_context": {
                    "mongodb_schema": schema,
                    "migration_log": dm.get("migration_log", {}),
                },
            },
            ctx=ctx,
        )
        sc_inputs["sra_rationale"] = (sra or {}).get("agent_design_rationale", {})
        sc = await wf.agent("sc", sc_inputs, ctx=ctx)

    art = DbArtifacts(
        db_id=db_id,
        mongodb_schema=schema,
        mongodb_data=dm.get("mongodb_data", {}),
        rationale=_complete_rationale(
            db_id=db_id,
            rationale=(sra or {}).get("agent_design_rationale", {}),
            schema=schema,
            migration_log=dm.get("migration_log", {}),
            source_schema=source_schema,
            sc=sc or {},
        ),
        world_signature=dm.get("world_signature", ""),
        scenario_summary=wp.get("scenario_summary", ""),
        query_bearing=bool(sc and sc.get("query_bearing")),
        domain_id=source_schema.domain if source_schema else "unknown",
        sqlite_path=str(source_schema.sqlite_path) if source_schema else "",
        table_count=source_schema.table_count if source_schema else 0,
        query_count=len(workload),
        wp_output=wp,
    )
    ctx.log.info("phase_a_db_done", db_id=db_id, world_signature=art.world_signature,
                 query_bearing=art.query_bearing)
    return art


# --------------------------------------------------------------------------- #
# Phase B — reverse-engineered NL-MQL (per record: QPS -> MS -> ... -> RA)
# --------------------------------------------------------------------------- #
async def run_phase_b(
    wf: Workflow,
    artifacts: dict[str, DbArtifacts],
    slots: list[CoverageSlot],
    *,
    seen_mql: dict[tuple[str, str], int] | None = None,
) -> list[dict[str, Any]]:
    """Construct one record per coverage slot, pipelined across slots."""
    wf.phase("B")
    if wf.ctx.progress:
        wf.ctx.progress.add_group("phaseB", "records", phase="B", total=len(slots))

    ledger = DiversityLedger(
        seen_mql=seen_mql if seen_mql is not None else {},
        seen_skeleton={},
        lock=asyncio.Lock(),
    )
    records = await wf.pipeline(
        slots,
        lambda slot: _build_record(
            wf,
            artifacts,
            slot,
            diversity_ledger=ledger,
        ),
        isolate=True,
    )
    return [r for r in records if isinstance(r, dict)]


async def _build_record(
    wf: Workflow,
    artifacts: dict[str, DbArtifacts],
    slot: CoverageSlot,
    *,
    seen_mql: dict[tuple[str, str], int] | None = None,
    seen_skeleton: dict[tuple[str, str], list[int]] | None = None,
    mql_lock: asyncio.Lock | None = None,
    diversity_ledger: DiversityLedger | None = None,
) -> dict[str, Any] | None:
    art = artifacts.get(slot.db_id)
    if art is None:
        return None
    ctx = wf.context(db_id=slot.db_id, record_id=slot.record_id, group="phaseB", phase="B")
    ledger = diversity_ledger or DiversityLedger(
        seen_mql=seen_mql if seen_mql is not None else {},
        seen_skeleton=seen_skeleton if seen_skeleton is not None else {},
        lock=mql_lock or asyncio.Lock(),
    )
    diversity_context = await ledger.reserve_slot(ctx, slot)
    duplicate_slot = diversity_context.get("duplicate_of_record_id")
    if duplicate_slot is not None:
        return _drop(
            ctx,
            "slot",
            "duplicate diversity slot rejected",
            duplicate_of_record_id=duplicate_slot,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
        )

    # QPS: enumerate one intent for this (mechanism, archetype) cell
    qps = await wf.agent("qps", {
        "mechanism": slot.mechanism, "archetype": slot.archetype,
        "target_difficulty": slot.target_difficulty,
        "target_sql_infeasibility_class": slot.target_sql_infeasibility_class,
        "target_schema_flex": slot.target_schema_flex,
        "record_id": slot.record_id,
        "slot_index": slot.slot_index,
        "diversity_key": slot.diversity_key,
        "diversity_hint": slot.diversity_hint,
        "schema_feature": slot.schema_feature,
        "reference_oracle_seed": None,
        "intent_seed": slot.intent_seed,
        "llm_design_mode": True,
        "diversity_context": diversity_context,
        "scenario_summary": art.scenario_summary, "schema": art.mongodb_schema,
    }, ctx=ctx)
    if not qps:
        return _drop(ctx, "qps", "intent enumeration failed")
    intent = _intent_with_reference_oracle(qps)
    reference = intent.get("reference_oracle")
    if not isinstance(reference, dict) and isinstance(slot.reference_oracle_seed, dict):
        reference = slot.reference_oracle_seed
        intent = {**intent, "reference_oracle": reference}
        ctx.log.info(
            "reference_oracle_certification_backfilled",
            template=reference.get("template"),
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
        )

    # MS: when the slot has a hidden deterministic oracle, compile the gold MQL first.
    # This keeps live throughput stable: LLMs still design intents/NL, but correctness of the
    # executable gold is no longer gated on the MS agent producing parseable bespoke JSON.
    ms = await _compile_reference_oracle_ms(ctx, art, slot, intent, reference)
    ms_feedback = None
    if ms is None:
        for r in range(MS_MAX_ROUNDS + 1):
            ms = await wf.agent("ms", {"intent": intent, "reference_oracle": reference,
                                       "schema": art.mongodb_schema,
                                       "mongodb_data": art.mongodb_data,
                                       "target_difficulty": slot.target_difficulty,
                                       "target_sql_infeasibility_class": slot.target_sql_infeasibility_class,
                                       "target_schema_flex": slot.target_schema_flex,
                                       "allow_reference_oracle_canonicalization": True,
                                       "llm_design_mode": True,
                                       "ms_feedback": ms_feedback}, ctx=ctx)
            if ms and ms.get("gold_locked"):
                break
            ms_feedback = (ms or {}).get("gold_lock_reason")
            ctx.log.warning("ms_gold_lock_retry", round=r, reason=ms_feedback)
    if not ms or not ms.get("gold_locked"):
        return _drop(ctx, "ms", "gold not locked", detail=(ms or {}).get("gold_lock_reason"))
    identity = await ledger.reserve_mql_identity(ctx, slot, ms["MQL"])
    mql_sig = identity["mql_signature"]
    skeleton_sig = identity["mql_skeleton_signature"]
    skeleton_summary = identity["mql_skeleton_summary"]
    duplicate_of = identity["duplicate_of"]
    if duplicate_of is not None:
        ctx.log.warning(
            "duplicate_mql_rejected",
            record_id=slot.record_id,
            duplicate_of_record_id=duplicate_of,
            mql_signature=mql_sig,
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
            mql_preview=str(ms["MQL"])[:240],
        )
        return _drop(
            ctx,
            "ms",
            "duplicate MQL rejected",
            duplicate_of_record_id=duplicate_of,
            mql_signature=mql_sig,
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
        )
    skeleton_family = identity["skeleton_family_record_ids"]
    if identity["skeleton_over_cap"]:
        ctx.log.warning(
            "mql_skeleton_family_rejected",
            record_id=slot.record_id,
            mql_skeleton_signature=skeleton_sig,
            mql_skeleton_summary=skeleton_summary,
            skeleton_family_record_ids=skeleton_family,
            cap=MQL_SKELETON_FAMILY_CAP,
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
            mql_preview=str(ms["MQL"])[:240],
        )
        return _drop(
            ctx,
            "ms",
            "MQL skeleton family over diversity cap",
            mql_skeleton_signature=skeleton_sig,
            mql_skeleton_summary=skeleton_summary,
            skeleton_family_record_ids=skeleton_family,
            cap=MQL_SKELETON_FAMILY_CAP,
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
        )

    async def mutation_branch() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        ctx.log.info("phase_b_branch_start", branch="mut_pv")
        if ctx.progress:
            ctx.progress.start_task(f"{slot.record_id}:mut", "MUT/PV", group="phaseB")
        try:
            mut = await wf.agent("mut", {
                "intent": intent, "MQL": ms["MQL"], "canonical_form_set": ms["canonical_form_set"],
            }, ctx=ctx)
            pv = await wf.agent(
                "pv",
                {"MQL": ms["MQL"], "mutations": (mut or {}).get("mutations", []), "intent": intent},
                ctx=ctx,
            )
            mut_rounds = 0
            while (not pv or not pv.get("pv_pass")) and mut_rounds < MUT_MAX_ROUNDS:
                mut_rounds += 1
                pv_detail = (pv or {}).get("property_verification", {})
                ctx.log.warning("pv_reject", branch="mut_pv", round=mut_rounds, detail=pv_detail)
                mut = await wf.agent("mut", {
                    "intent": intent, "MQL": ms["MQL"],
                    "canonical_form_set": ms["canonical_form_set"], "pv_feedback": pv_detail,
                }, ctx=ctx)
                pv = await wf.agent(
                    "pv",
                    {
                        "MQL": ms["MQL"],
                        "mutations": (mut or {}).get("mutations", []),
                        "intent": intent,
                    },
                    ctx=ctx,
                )
            return mut, pv
        finally:
            if ctx.progress:
                ctx.progress.finish_task(f"{slot.record_id}:mut")

    async def nl_branch() -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        ctx.log.info("phase_b_branch_start", branch="nlp_rtv")
        if ctx.progress:
            ctx.progress.start_task(f"{slot.record_id}:nl", "NLP/RTV", group="phaseB")
        try:
            nlp_inputs = {
                "intent": intent,
                "scenario_summary": art.scenario_summary,
                "MQL": ms["MQL"],
                "result_fields": ms.get("result_fields"),
            }
            nlp = await wf.agent("nlp", nlp_inputs, ctx=ctx)
            rtv = await wf.agent("rtv", {"nl_queries": (nlp or {}).get("nl_queries"),
                                         "MQL": ms["MQL"], "schema": art.mongodb_schema}, ctx=ctx)
            rounds = 0
            while (not rtv or not rtv.get("rtv_pass")) and rounds < RTV_MAX_ROUNDS:
                rounds += 1
                ctx.log.warning("rtv_reject", branch="nlp_rtv", round=rounds,
                                reason=(rtv or {}).get("rtv_reason"))
                nlp = await wf.agent("nlp", {**nlp_inputs, "rtv_feedback": rtv}, ctx=ctx)
                rtv = await wf.agent("rtv", {"nl_queries": (nlp or {}).get("nl_queries"),
                                             "MQL": ms["MQL"], "schema": art.mongodb_schema}, ctx=ctx)
            return nlp, rtv
        finally:
            if ctx.progress:
                ctx.progress.finish_task(f"{slot.record_id}:nl")

    async def ra_branch() -> dict[str, Any] | None:
        ctx.log.info("phase_b_branch_start", branch="ra")
        if ctx.progress:
            ctx.progress.start_task(f"{slot.record_id}:ra", "RA", group="phaseB")
        try:
            return await wf.agent("ra", {"MQL": ms["MQL"], "intent": intent}, ctx=ctx)
        finally:
            if ctx.progress:
                ctx.progress.finish_task(f"{slot.record_id}:ra")

    mutation_task = asyncio.create_task(mutation_branch())
    nl_task = asyncio.create_task(nl_branch())
    ra_task = asyncio.create_task(ra_branch())
    try:
        (_mut, pv), (nlp, rtv), ra = await asyncio.gather(mutation_task, nl_task, ra_task)
    except Exception:
        for task in (mutation_task, nl_task, ra_task):
            if not task.done():
                task.cancel()
        results = await asyncio.gather(mutation_task, nl_task, ra_task, return_exceptions=True)
        for exc in results:
            if isinstance(exc, Exception) and not isinstance(exc, asyncio.CancelledError):
                _log_branch_exception(ctx, exc, stage="phase_b_branch")
        raise

    if not pv or not pv.get("pv_pass"):
        return _drop(ctx, "pv", "mutations not discriminative / gold trivial",
                     detail=(pv or {}).get("property_verification"))
    if not rtv or not rtv.get("rtv_pass"):
        return _drop(ctx, "rtv", "canonical NLQ does not round-trip to gold",
                     detail=(rtv or {}).get("rtv_reason"))
    if not ra or not ra.get("ra_pass"):
        return _drop(ctx, "ra", "realism / P4 non-triviality failed")

    # NNC: difficulty + dual-bridge gate
    nnc = await wf.agent("nnc", {
        "MQL": ms["MQL"], "nl_queries": nlp["nl_queries"],
        "canonical_form_set": ms["canonical_form_set"], "intent": intent,
        "shape_policy": ms.get("shape_policy"),
        "target_difficulty": slot.target_difficulty,
        "target_sql_infeasibility_class": slot.target_sql_infeasibility_class,
        "target_schema_flex": slot.target_schema_flex,
    }, ctx=ctx)
    if not nnc or not nnc.get("gate_pass"):
        return _drop(ctx, "nnc", "dual-bridge gate failed", detail=(nnc or {}).get("nnc_verdict"))
    # A record is valid even when NNC's labels differ from the slot's requested cell, so keep
    # it under NNC's actual (difficulty, class, schema_flex) rather than discarding a good
    # record for not matching the exact requested cell — composition then follows what the
    # data+model actually produced (maximizing diverse yield), while the complex/structural
    # cells still build under their own labels.
    target_violations = _target_violations(slot, nnc, ms)
    if target_violations:
        ctx.log.warning("coverage_target_relabeled", record_id=slot.record_id,
                        detail=target_violations)

    # Keep labels self-consistent (validation C9): a record is only structural_schema_flex
    # when its MQL actually carries schema-flex dispatch. After the coverage relabel a record
    # can be NNC-labeled ssf yet have schema_flex none — relabel those to semantic so the
    # published (class, schema_flex, difficulty) stay coherent.
    has_flex = bool(ms.get("schema_flex") and ms["schema_flex"] != "none")
    sql_class = nnc["sql_infeasibility_class"]
    if sql_class == "structural_schema_flex" and not has_flex:
        sql_class = "semantic"
    nl_identity = await ledger.reserve_nl_identity(ctx, slot, nlp["nl_queries"], mql_sig)
    duplicate_pair_of = nl_identity["duplicate_pair_of"]
    if duplicate_pair_of is not None:
        ctx.log.warning(
            "duplicate_nl_mql_pair_rejected",
            record_id=slot.record_id,
            duplicate_of_record_id=duplicate_pair_of,
            nl_signature=nl_identity["nl_signature"],
            nl_mql_pair_signature=nl_identity["nl_mql_pair_signature"],
            mql_signature=mql_sig,
            canonical_preview=nl_identity["canonical_preview"],
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
        )
        return _drop(
            ctx,
            "nlp",
            "duplicate NL-MQL pair rejected",
            duplicate_of_record_id=duplicate_pair_of,
            nl_signature=nl_identity["nl_signature"],
            nl_mql_pair_signature=nl_identity["nl_mql_pair_signature"],
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
        )
    duplicate_nl_of = nl_identity["duplicate_nl_of"]
    if duplicate_nl_of is not None:
        ctx.log.warning(
            "duplicate_canonical_nl_rejected",
            record_id=slot.record_id,
            duplicate_of_record_id=duplicate_nl_of,
            nl_signature=nl_identity["nl_signature"],
            nl_mql_pair_signature=nl_identity["nl_mql_pair_signature"],
            mql_signature=mql_sig,
            canonical_preview=nl_identity["canonical_preview"],
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
        )
        return _drop(
            ctx,
            "nlp",
            "duplicate canonical NL rejected",
            duplicate_of_record_id=duplicate_nl_of,
            nl_signature=nl_identity["nl_signature"],
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            diversity_key=slot.diversity_key,
            schema_feature=slot.schema_feature,
        )
    record = {
        "record_id": slot.record_id,
        "db_id": slot.db_id,
        "mechanism": slot.mechanism,
        "archetype": slot.archetype,
        "diversity_key": slot.diversity_key,
        "schema_feature": slot.schema_feature,
        "nl_queries": nlp["nl_queries"],
        "MQL": ms["MQL"],
        "mql_signature": mql_sig,
        "mql_skeleton_signature": skeleton_sig,
        "mql_skeleton_summary": skeleton_summary,
        "canonical_form_set": ms["canonical_form_set"],
        "difficulty": nnc["difficulty"],
        "sql_infeasibility_class": sql_class,
        "shape_policy": ms.get("shape_policy", "preserve"),
        "world_signature": art.world_signature,
    }
    if has_flex:
        record["schema_flex"] = _publish_schema_flex(ms["schema_flex"])
    ctx.log.info("record_built", record_id=slot.record_id, difficulty=record["difficulty"],
                 sql_infeasibility_class=record["sql_infeasibility_class"],
                 mechanism=slot.mechanism, archetype=slot.archetype,
                 diversity_key=slot.diversity_key,
                 schema_feature=slot.schema_feature,
                 mql_signature=mql_sig,
                 mql_skeleton_signature=skeleton_sig,
                 mql_skeleton_summary=skeleton_summary)
    return record


async def _compile_reference_oracle_ms(
    ctx: AgentContext,
    art: DbArtifacts,
    slot: CoverageSlot,
    intent: dict[str, Any],
    reference: Any,
) -> dict[str, Any] | None:
    if ctx.settings.stub:
        return None
    if not isinstance(reference, dict) or not isinstance(slot.reference_oracle_seed, dict):
        return None
    if reference != slot.reference_oracle_seed:
        return None
    try:
        compiled = _canonical_reference_mql({
            "intent": intent,
            "reference_oracle": reference,
            "schema": art.mongodb_schema,
        })
        if compiled is None:
            return None
        mql, shape = compiled
        hits = scan_disabled(mql)
        if hits:
            ctx.log.warning(
                "ms_reference_oracle_compile_failed",
                template=reference.get("template"),
                reason="banned operators",
                hits=hits,
            )
            return None
        collection, _pipeline = parse_pipeline(mql)
        if ctx.mongo is None or not ctx.mongo.available():
            ctx.log.warning(
                "ms_reference_oracle_compile_failed",
                template=reference.get("template"),
                reason="MongoDB executor unavailable for compiled gold-lock",
            )
            return None
        rows = await asyncio.to_thread(ctx.mongo.norm_exec, ctx.db_id, mql)
        if not rows:
            ctx.log.warning(
                "ms_reference_oracle_compile_failed",
                template=reference.get("template"),
                reason="compiled gold result is empty",
            )
            return None
        if shape == "preserve":
            n_in = await asyncio.to_thread(ctx.mongo.count, ctx.db_id, collection)
            if len(rows) != n_in:
                ctx.log.warning(
                    "ms_reference_oracle_compile_failed",
                    template=reference.get("template"),
                    reason="compiled preserve cardinality mismatch",
                    output_rows=len(rows),
                    input_rows=n_in,
                )
                return None
        schema_flex = (
            slot.target_schema_flex
            if slot.target_schema_flex and slot.target_schema_flex != "none"
            else "none"
        )
        ctx.log.info(
            "ms_reference_oracle_compiled",
            template=reference.get("template"),
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            shape_policy=shape,
            schema_flex=schema_flex,
        )
        return {
            "gold_locked": True,
            "MQL": mql,
            "canonical_form_set": derive_canonical_form_set(mql, shape),
            "shape_policy": shape,
            "schema_flex": schema_flex,
            "result_fields": sorted({k for row in rows for k in row}),
            "reference_oracle_verified": True,
            "reference_oracle_canonicalized": True,
            "compiled_reference_oracle": True,
        }
    except TendError as exc:
        ctx.log.warning(
            "ms_reference_oracle_compile_failed",
            template=reference.get("template"),
            error_type=type(exc).__name__,
            reason=exc.message,
            context=exc.context,
        )
        return None


async def _reserve_mql_identity(
    ctx: AgentContext,
    slot: CoverageSlot,
    mql: str,
    *,
    seen_mql: dict[tuple[str, str], int] | None,
    seen_skeleton: dict[tuple[str, str], list[int]] | None,
    mql_lock: asyncio.Lock | None,
    skeleton_cap: int,
) -> dict[str, Any]:
    sig = mql_signature(mql)
    skeleton_sig = mql_skeleton_signature(mql)
    skeleton_summary = mql_skeleton_summary(mql)
    if seen_mql is None:
        return {
            "mql_signature": sig,
            "mql_skeleton_signature": skeleton_sig,
            "mql_skeleton_summary": skeleton_summary,
            "duplicate_of": None,
            "skeleton_family_record_ids": [slot.record_id],
            "skeleton_over_cap": False,
        }

    key = (slot.db_id, sig)
    skeleton_key = (slot.db_id, skeleton_sig)

    async def reserve() -> tuple[int | None, list[int], bool]:
        previous = seen_mql.get(key)
        if previous is not None:
            family = list((seen_skeleton or {}).get(skeleton_key, []))
            return previous, family, False
        if seen_skeleton is not None:
            current_family = list(seen_skeleton.get(skeleton_key, []))
            if len(current_family) >= skeleton_cap:
                return None, current_family + [slot.record_id], True
        seen_mql[key] = slot.record_id
        if seen_skeleton is None:
            return None, [slot.record_id], False
        family = seen_skeleton.setdefault(skeleton_key, [])
        family.append(slot.record_id)
        return None, list(family), False

    if mql_lock is not None:
        async with mql_lock:
            duplicate_of, family, skeleton_over_cap = await reserve()
    else:
        duplicate_of, family, skeleton_over_cap = await reserve()
    if duplicate_of is None and not skeleton_over_cap:
        ctx.log.info(
            "mql_signature_reserved",
            record_id=slot.record_id,
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            mql_signature=sig,
            mql_skeleton_signature=skeleton_sig,
            mql_skeleton_summary=skeleton_summary,
            skeleton_family_count=len(family),
        )
    return {
        "mql_signature": sig,
        "mql_skeleton_signature": skeleton_sig,
        "mql_skeleton_summary": skeleton_summary,
        "duplicate_of": duplicate_of,
        "skeleton_family_record_ids": family,
        "skeleton_over_cap": skeleton_over_cap,
    }


async def _reserve_nl_identity(
    ctx: AgentContext,
    slot: CoverageSlot,
    nl_queries: dict[str, Any],
    mql_sig: str,
    *,
    seen_canonical_nl: dict[tuple[str, str], int] | None,
    seen_nl_mql_pair: dict[tuple[str, str, str], int] | None,
    nl_lock: asyncio.Lock | None,
) -> dict[str, Any]:
    canonical = _normalized_nl_text(nl_queries.get("canonical") if isinstance(nl_queries, dict) else "")
    nl_sig = _sha256_text(canonical)
    pair_sig = _sha256_text(f"{nl_sig}\n{mql_sig}")
    canonical_key = (slot.db_id, nl_sig)
    pair_key = (slot.db_id, nl_sig, mql_sig)

    async def reserve() -> tuple[int | None, int | None]:
        duplicate_pair_of = (
            seen_nl_mql_pair.get(pair_key) if seen_nl_mql_pair is not None else None
        )
        duplicate_nl_of = (
            seen_canonical_nl.get(canonical_key) if seen_canonical_nl is not None else None
        )
        if duplicate_pair_of is None and duplicate_nl_of is None:
            if seen_nl_mql_pair is not None:
                seen_nl_mql_pair[pair_key] = slot.record_id
            if seen_canonical_nl is not None:
                seen_canonical_nl[canonical_key] = slot.record_id
        return duplicate_pair_of, duplicate_nl_of

    if nl_lock is not None:
        async with nl_lock:
            duplicate_pair_of, duplicate_nl_of = await reserve()
    else:
        duplicate_pair_of, duplicate_nl_of = await reserve()
    if duplicate_pair_of is None and duplicate_nl_of is None:
        ctx.log.info(
            "nl_signature_reserved",
            record_id=slot.record_id,
            mechanism=slot.mechanism,
            archetype=slot.archetype,
            nl_signature=nl_sig,
            nl_mql_pair_signature=pair_sig,
            mql_signature=mql_sig,
            canonical_preview=canonical[:180],
        )
    return {
        "nl_signature": nl_sig,
        "nl_mql_pair_signature": pair_sig,
        "duplicate_pair_of": duplicate_pair_of,
        "duplicate_nl_of": duplicate_nl_of,
        "canonical_preview": canonical[:180],
    }


def _normalized_nl_text(text: Any) -> str:
    return " ".join(str(text or "").strip().lower().split())


def _sha256_text(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _publish_schema_flex(value: str) -> str:
    """Map internal mechanism labels to the public record schema enum."""
    return {
        "optional_embed": "polymorphic",
        "optional": "polymorphic",
    }.get(value, value)


def _target_violations(
    slot: CoverageSlot, nnc: dict[str, Any], ms: dict[str, Any]
) -> list[str]:
    """Deterministically enforce the composition cell requested by the scheduler."""
    violations: list[str] = []
    if slot.target_difficulty and nnc.get("difficulty") != slot.target_difficulty:
        violations.append(
            f"difficulty {nnc.get('difficulty')!r} != target {slot.target_difficulty!r}"
        )
    if slot.target_sql_infeasibility_class \
            and nnc.get("sql_infeasibility_class") != slot.target_sql_infeasibility_class:
        violations.append(
            "sql_infeasibility_class "
            f"{nnc.get('sql_infeasibility_class')!r} != target "
            f"{slot.target_sql_infeasibility_class!r}"
        )
    if slot.target_schema_flex and slot.target_schema_flex != "none":
        flex = _publish_schema_flex(str(ms.get("schema_flex") or "none"))
        if flex != slot.target_schema_flex:
            violations.append(f"schema_flex {flex!r} != target {slot.target_schema_flex!r}")
    return violations


def _intent_with_reference_oracle(qps: dict[str, Any]) -> dict[str, Any]:
    """Extract QPS intent without losing the top-level oracle payload MS must verify."""
    intent = qps.get("intent", qps)
    if not isinstance(intent, dict):
        return {}
    out = dict(intent)
    reference = qps.get("reference_oracle")
    if reference is not None and "reference_oracle" not in out:
        out["reference_oracle"] = reference
    return out


def _slot_diversity_axes(slot: CoverageSlot) -> dict[str, str]:
    card = slot.intent_seed if isinstance(slot.intent_seed, dict) else {}
    feature_family = str(card.get("feature_family") or "")
    complexity_score = card.get("complexity_score")
    try:
        score = int(complexity_score)
    except (TypeError, ValueError):
        score = 0
    if score >= 7:
        complexity = "high"
    elif score >= 4:
        complexity = "medium"
    elif score > 0:
        complexity = "low"
    else:
        complexity = ""
    return {
        "mechanism": str(slot.mechanism or ""),
        "archetype": str(slot.archetype or ""),
        "schema_feature": str(slot.schema_feature or ""),
        "feature_family": feature_family,
        "complexity": complexity,
    }


def _workload_evidence(workload: list[Any], limit: int = 12) -> list[dict[str, Any]]:
    """Compact real query-bearing evidence for SC's artifact review."""
    evidence = []
    for q in workload[:limit]:
        evidence.append({
            "question_id": getattr(q, "question_id", None),
            "difficulty": getattr(q, "difficulty", ""),
            "question": getattr(q, "question", ""),
            "evidence": getattr(q, "evidence", ""),
            "sql": getattr(q, "sql", ""),
        })
    return evidence


def _complete_rationale(
    *,
    db_id: str,
    rationale: dict[str, Any],
    schema: dict[str, Any],
    migration_log: dict[str, Any],
    source_schema: Any,
    sc: dict[str, Any],
) -> dict[str, Any]:
    """Normalize SRA/DM rationale to the release ADR schema without hiding DM facts."""
    out = dict(rationale or {})
    out["db_id"] = db_id
    if source_schema is not None:
        out.setdefault("source_spider_tables", list(source_schema.tables))
    out.setdefault("collections", {name: {"fields": [k for k in node if not k.startswith("__")]}
                                   for name, node in schema.items()
                                   if isinstance(node, dict)})

    decisions = [
        _release_decision(d) for d in out.get("decisions", [])
        if isinstance(d, dict) and d.get("id") and d.get("type") and d.get("rationale")
    ]
    next_id = len(decisions) + 1
    for parent, children in sorted((migration_log.get("embeds") or {}).items()):
        for child in children:
            decisions.append({
                "id": f"D{next_id:02d}",
                "type": "embed",
                "parent": parent,
                "child": child,
                "rationale": (
                    f"DM embedded sparse satellite table {child} under {parent} from "
                    "the BIRD foreign-key/cardinality evidence."
                ),
                "reference": "migration_log.embeds",
            })
            next_id += 1
    if not decisions:
        decisions.append({
            "id": "D01",
            "type": "reference",
            "rationale": "DM preserved source tables as referenced MongoDB collections.",
            "reference": "migration_log.references",
        })
    out["decisions"] = decisions

    has_variants = any(isinstance(node, dict) and node.get("__variants")
                       for node in schema.values())
    patterns = ["embed"] if any(d.get("type") == "embed" for d in decisions) else ["mixed"]
    if has_variants:
        patterns.append("polymorphic")
    out["patterns_applied"] = _release_patterns_applied(
        out.get("patterns_applied") or patterns,
        fallback=patterns,
    )
    out.setdefault("rationale_summary",
                   "DM produced a deterministic document-aggregate layout from BIRD FKs.")
    out.setdefault("anti_pattern_checks", {
        "pass": sc.get("verdict") != "reject",
        "issues": [str(i) for i in sc.get("issues", [])],
    })
    if not isinstance(out.get("heterogenization"), dict):
        out["heterogenization"] = {
            "schema_flex": "polymorphic" if has_variants else "none",
            "triggers": [
                {
                    "mechanism": "sparse",
                    "fired": bool(has_variants),
                    "evidence": "sparse optional embeds create present/missing document variants",
                }
            ],
        }
    else:
        hetero = out["heterogenization"]
        if isinstance(hetero, dict):
            hetero["schema_flex"] = _release_schema_flex_value(
                hetero.get("schema_flex"),
                has_variants=has_variants,
            )
    return out


def _release_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Coerce SRA wording into the ADR schema's closed decision type enum."""
    out = dict(decision)
    allowed = {
        "embed",
        "reference",
        "extended_reference",
        "polymorphic_collapse",
        "bucket",
        "computed",
        "attribute",
        "subset",
        "tree",
        "outlier",
        "schema_versioning",
    }
    mapping = {
        "mixed": "attribute",
        "denormalize": "attribute",
        "denormalized": "attribute",
        "denormalization": "attribute",
        "polymorphic": "polymorphic_collapse",
        "polymorphic collapse": "polymorphic_collapse",
        "extended reference": "extended_reference",
        "schema versioning": "schema_versioning",
    }
    raw_type = str(out.get("type", "")).strip()
    normalized = mapping.get(raw_type.lower(), raw_type)
    out["type"] = normalized if normalized in allowed else "attribute"
    return out


def _release_patterns_applied(values: Any, *, fallback: list[str]) -> list[str]:
    allowed = {
        "embed",
        "extended_reference",
        "polymorphic",
        "attribute",
        "bucket",
        "computed",
        "subset",
        "tree",
        "outlier",
        "schema_versioning",
        "mixed",
    }
    mapping = {
        "reference": "extended_reference",
        "extended reference": "extended_reference",
        "denormalize": "attribute",
        "denormalized": "attribute",
        "denormalization": "attribute",
        "sparse": "polymorphic",
        "optional": "polymorphic",
        "optional_embed": "polymorphic",
        "sparse_embed": "polymorphic",
        "sparse_scalar": "polymorphic",
        "version": "schema_versioning",
        "versioning": "schema_versioning",
        "schema versioning": "schema_versioning",
    }
    raw_values = values if isinstance(values, list) else [values]
    normalized: list[str] = []
    for value in raw_values:
        raw = str(value or "").strip()
        if not raw:
            continue
        mapped = mapping.get(raw.lower(), raw)
        if mapped in allowed:
            normalized.append(mapped)
    if not normalized:
        normalized = [p for p in fallback if p in allowed]
    return list(dict.fromkeys(normalized or ["mixed"]))


def _release_schema_flex_value(value: Any, *, has_variants: bool) -> str:
    allowed = {"none", "polymorphic", "attribute_bag", "schema_versioning", "dynamic_key"}
    mapping = {
        "sparse": "polymorphic",
        "optional": "polymorphic",
        "optional_embed": "polymorphic",
        "version": "schema_versioning",
        "versioning": "schema_versioning",
    }
    normalized = mapping.get(str(value), str(value)) if value is not None else ""
    if normalized in allowed:
        return normalized
    return "polymorphic" if has_variants else "none"
