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
from dataclasses import dataclass, field
from typing import Any

from ..agents import AgentContext
from ..errors import Anomaly, wrap_unexpected
from .engine import Workflow

SC_MAX_ROUNDS = 2          # SC reject -> SRA revise, at most twice (04-1-2 / 03-II-4)
RTV_MAX_ROUNDS = 2         # RTV canonical fail -> NLP rewrite (04-1-2-3)
MS_MAX_ROUNDS = 2          # gold-lock fail -> re-synthesize / re-sample intent
MUT_MAX_ROUNDS = 2         # PV reject -> MUT regenerate discriminating mutations
RA_MAX_ROUNDS = 2          # P4 augment loop


def _task_failed(task: "asyncio.Task[Any]") -> bool:
    """True if a *done* task ended in cancellation or an exception (never raises)."""
    if not task.done() or task.cancelled():
        return True
    return task.exception() is not None


def _drop(ctx, stage: str, reason: str, **detail: Any) -> None:
    """Log a (expected) record drop with its cause so the reason is never silent."""
    ctx.log.warning("record_dropped", stage=stage, reason=reason,
                    record_id=ctx.record_id, **detail)
    ctx.log.anomaly(kind=Anomaly.SUPPLY_EXHAUSTED, message="record dropped",
                    stage=stage, reason=reason, record_id=ctx.record_id, **detail)
    return None


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

    if ctx.progress:
        ctx.progress.start_task(f"{db_id}:wp", "WP", group=db_id)
        ctx.progress.start_task(f"{db_id}:dm", "DM", group=db_id)
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
                ctx.log.anomaly(wrap_unexpected(exc, stage="phase_a_branch"))
        raise
    finally:
        if ctx.progress:
            ctx.progress.finish_task(
                f"{db_id}:wp", ok=wp_task.done() and not _task_failed(wp_task))
            ctx.progress.finish_task(
                f"{db_id}:dm", ok=dm_task.done() and not _task_failed(dm_task))

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
    wf: Workflow, artifacts: dict[str, DbArtifacts], slots: list[CoverageSlot]
) -> list[dict[str, Any]]:
    """Construct one record per coverage slot, pipelined across slots."""
    wf.phase("B")
    if wf.ctx.progress:
        wf.ctx.progress.add_group("phaseB", "records", phase="B", total=len(slots))

    records = await wf.pipeline(
        slots,
        lambda slot: _build_record(wf, artifacts, slot),
        isolate=True,
    )
    return [r for r in records if isinstance(r, dict)]


async def _build_record(
    wf: Workflow, artifacts: dict[str, DbArtifacts], slot: CoverageSlot
) -> dict[str, Any] | None:
    art = artifacts.get(slot.db_id)
    if art is None:
        return None
    ctx = wf.context(db_id=slot.db_id, record_id=slot.record_id, group="phaseB", phase="B")

    # QPS: enumerate one intent for this (mechanism, archetype) cell
    qps = await wf.agent("qps", {
        "mechanism": slot.mechanism, "archetype": slot.archetype,
        "target_difficulty": slot.target_difficulty,
        "target_sql_infeasibility_class": slot.target_sql_infeasibility_class,
        "target_schema_flex": slot.target_schema_flex,
        "scenario_summary": art.scenario_summary, "schema": art.mongodb_schema,
    }, ctx=ctx)
    if not qps:
        return _drop(ctx, "qps", "intent enumeration failed")
    intent = _intent_with_reference_oracle(qps)
    reference = intent.get("reference_oracle")

    # MS: synthesize gold + deterministic gold-lock (executes + preserve cardinality)
    ms = None
    ms_feedback = None
    for r in range(MS_MAX_ROUNDS + 1):
        ms = await wf.agent("ms", {"intent": intent, "reference_oracle": reference,
                                   "schema": art.mongodb_schema,
                                   "mongodb_data": art.mongodb_data,
                                   "target_difficulty": slot.target_difficulty,
                                   "target_sql_infeasibility_class": slot.target_sql_infeasibility_class,
                                   "target_schema_flex": slot.target_schema_flex,
                                   "ms_feedback": ms_feedback}, ctx=ctx)
        if ms and ms.get("gold_locked"):
            break
        ms_feedback = (ms or {}).get("gold_lock_reason")
        ctx.log.warning("ms_gold_lock_retry", round=r, reason=ms_feedback)
    if not ms or not ms.get("gold_locked"):
        return _drop(ctx, "ms", "gold not locked", detail=(ms or {}).get("gold_lock_reason"))

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
                ctx.log.anomaly(wrap_unexpected(exc, stage="phase_b_branch"))
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
    record = {
        "record_id": slot.record_id,
        "db_id": slot.db_id,
        "nl_queries": nlp["nl_queries"],
        "MQL": ms["MQL"],
        "canonical_form_set": ms["canonical_form_set"],
        "difficulty": nnc["difficulty"],
        "sql_infeasibility_class": sql_class,
        "shape_policy": ms.get("shape_policy", "preserve"),
        "world_signature": art.world_signature,
    }
    if has_flex:
        record["schema_flex"] = _publish_schema_flex(ms["schema_flex"])
    ctx.log.info("record_built", record_id=slot.record_id, difficulty=record["difficulty"],
                 sql_infeasibility_class=record["sql_infeasibility_class"])
    return record


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
        d for d in out.get("decisions", [])
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
    out["patterns_applied"] = list(dict.fromkeys(out.get("patterns_applied") or patterns))
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
