"""Deterministic release repair helpers for audited NLQ/MQL/DB defects."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..execution.ast_check import all_ops, derive_canonical_form_set, parse_pipeline
from ..execution.signature import (
    mql_signature,
    mql_skeleton_signature,
    mql_skeleton_summary,
)
from ..release_layout import resolve_release_dataset_layout


@dataclass(frozen=True, slots=True)
class RepairSummary:
    records: int
    mql_changed: int
    cfs_recomputed: int
    nlq_changed: int
    sort_stabilized: int
    output_files: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "records": self.records,
            "mql_changed": self.mql_changed,
            "cfs_recomputed": self.cfs_recomputed,
            "nlq_changed": self.nlq_changed,
            "sort_stabilized": self.sort_stabilized,
            "output_files": self.output_files,
        }


def apply_builtin_quality_repairs(dataset_dir: str | Path) -> RepairSummary:
    """Apply deterministic repairs for known template-level release defects."""

    layout = resolve_release_dataset_layout(dataset_dir)
    records = _load_records(layout.tend_path)
    pairs_by_record = _load_pairs(layout.root)
    mql_changed = 0
    cfs_recomputed = 0
    nlq_changed = 0
    sort_stabilized = 0

    for record in records:
        original_mql = str(record.get("MQL") or "")
        original_nlq = json.dumps(record.get("nl_queries", {}), sort_keys=True)

        collection, pipeline = parse_pipeline(original_mql)
        pipeline, template_repaired = _repair_known_pipeline(record, collection, pipeline)
        pipeline, cleaned = _remove_sort_keys_absent_after_projection(pipeline)
        pipeline, stabilized = _stabilize_sort_limit(pipeline)
        pipeline, cleaned_after_stabilize = _remove_sort_keys_absent_after_projection(pipeline)
        sort_stabilized += int(stabilized)
        changed = stabilized or template_repaired
        changed = changed or cleaned or cleaned_after_stabilize
        if changed:
            record["MQL"] = _format_mql(collection, pipeline)
            _repair_nlq_for_known_pipeline(record, pipeline)
            _refresh_native_metadata_after_mql_change(record)

        _refresh_mql_grounded_nlq(record, collection, pipeline)

        shape_policy = str(record.get("shape_policy") or "reshape")
        new_cfs = derive_canonical_form_set(str(record.get("MQL") or ""), shape_policy)
        if record.get("canonical_form_set") != new_cfs:
            record["canonical_form_set"] = new_cfs
            cfs_recomputed += 1

        _refresh_mql_metadata(record)
        _refresh_native_metadata_after_mql_change(record)
        if str(record.get("MQL") or "") != original_mql:
            mql_changed += 1
        if json.dumps(record.get("nl_queries", {}), sort_keys=True) != original_nlq:
            nlq_changed += 1

    output_files = _write_release_files(layout.root, records, pairs_by_record)
    return RepairSummary(
        records=len(records),
        mql_changed=mql_changed,
        cfs_recomputed=cfs_recomputed,
        nlq_changed=nlq_changed,
        sort_stabilized=sort_stabilized,
        output_files=output_files,
    )


def _load_records(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    records = raw.get("records", []) if isinstance(raw, dict) else raw
    return [record for record in records if isinstance(record, dict)]


def _load_pairs(root: Path) -> dict[Any, dict[str, Any]]:
    path = root / "audits" / "nl_mql" / "post_surgery_nl_mql_pairs.jsonl"
    pairs: dict[Any, dict[str, Any]] = {}
    if not path.exists():
        return pairs
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        pairs[item.get("record_id")] = item
    return pairs


def _repair_known_pipeline(
    record: dict[str, Any],
    collection: str,
    pipeline: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    db_id = str(record.get("db_id") or "")
    pattern = str(record.get("native_query_pattern") or record.get("archetype") or "")
    changed = False

    pipeline, repaired = _repair_dynamic_metric_array_sum(record, pipeline)
    changed = changed or repaired
    pipeline, repaired = _repair_dynamic_array_threshold(record, pipeline)
    changed = changed or repaired
    changed = _repair_grouped_dynamic_sort(pipeline) or changed

    if db_id == "financial" and pattern == "financial.loan_schedule":
        pipeline = _financial_loan_schedule_pipeline(pipeline)
        changed = True

    if db_id == "formula_1" and pattern == "f1.actor_career":
        for stage in pipeline:
            group = stage.get("$group") if isinstance(stage, dict) else None
            if isinstance(group, dict) and "wins_total" in group:
                group.pop("wins_total", None)
                changed = True
            project = stage.get("$project") if isinstance(stage, dict) else None
            if isinstance(project, dict) and "wins_total" in project:
                project.pop("wins_total", None)
                changed = True
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "points_total" in sort:
                _ensure_sort_keys(sort, ["entity_type", "nationality", "year"])
                changed = True

    if db_id == "formula_1":
        changed = _repair_formula_pipeline(record, pipeline) or changed

    if db_id == "student_club" and pattern == "student.officer_budget_attendee":
        pipeline = _student_officer_budget_attendee_pipeline(pipeline)
        changed = True

    if db_id == "student_club":
        changed = _repair_student_pipeline(record, pipeline) or changed

    if db_id == "superhero":
        changed = _repair_superhero_pipeline(record, pipeline) or changed

    if db_id == "toxicology":
        changed = _repair_toxicology_pipeline(record, pipeline) or changed

    if db_id == "thrombosis_prediction":
        changed = _repair_thrombosis_pipeline(record, pipeline) or changed

    if db_id == "european_football_2":
        changed = _repair_european_football_pipeline(record, pipeline) or changed

    if db_id == "debit_card_specializing":
        changed = _repair_debit_card_pipeline(record, pipeline) or changed

    if db_id == "card_games":
        changed = _repair_card_games_pipeline(record, pipeline) or changed

    if db_id == "codebase_community":
        changed = _repair_codebase_community_metadata(record) or changed

    if db_id == "california_schools":
        changed = _repair_california_pipeline(record, pipeline) or changed

    changed = _drop_unsafe_sort_keys(pipeline) or changed
    return pipeline, changed


_DYNAMIC_METRIC_ARRAY_SUM_REPAIRS: dict[int, tuple[str, str, bool]] = {
    # california_schools
    2147241: ("native_dynamic_entries.v.grade_spans", "native_dynamic_entries.v.grade_spans.meal_programs.free_pct", False),
    2468699: ("native_dynamic_entries.v.grade_spans", "native_dynamic_entries.v.grade_spans.meal_programs.frpm_count", False),
    2873655: ("native_dynamic_entries.v.grade_spans", "native_dynamic_entries.v.grade_spans.meal_programs.free_meal_count", False),
    9550575: ("native_dynamic_entries.v.grade_spans", "native_dynamic_entries.v.grade_spans.meal_programs.frpm_pct", False),
    12212912: ("native_dynamic_entries.v.grade_spans", "native_dynamic_entries.v.grade_spans.enrollment", False),
    # financial
    1023512: ("native_dynamic_entries.v.sample_edges", "native_dynamic_entries.v.sample_edges.account_id", False),
    2474334: ("native_dynamic_entries.v.entries", "native_dynamic_entries.v.entries.transaction_id", False),
    2869599: ("native_dynamic_entries.v.sample_edges", "native_dynamic_entries.v.sample_edges.counterparty_account.value", False),
    7555137: ("native_dynamic_entries.v.entries", "native_dynamic_entries.v.entries.transaction_id", False),
    # codebase_community
    4920950: ("native_dynamic_entries.v.2011.threads", "native_dynamic_entries.v.2011.threads.question_id", False),
    5727305: ("native_dynamic_entries.v.2014.threads", "native_dynamic_entries.v.2014.threads.score", False),
    5889793: ("native_dynamic_entries.v.2011.threads", "native_dynamic_entries.v.2011.threads.question_id", False),
    14246120: ("native_dynamic_entries.v.2014.threads", "native_dynamic_entries.v.2014.threads.score", False),
    # superhero
    7557202: ("native_dynamic_entries.v.observations", "native_dynamic_entries.v.observations.score.value", False),
    7796649: ("native_dynamic_entries.v.observations", "native_dynamic_entries.v.observations.score.value", False),
    13329567: ("native_dynamic_entries.v.observations", "native_dynamic_entries.v.observations.score.value", False),
    14377932: ("native_dynamic_entries.v.observations", "native_dynamic_entries.v.observations.score.value", False),
    4168301: ("native_dynamic_entries.v.power_families_by_bucket.energy.members", "native_dynamic_entries.v.power_families_by_bucket.energy.members.power_id", True),
    4565146: ("native_dynamic_entries.v.power_families_by_bucket.energy.members", "native_dynamic_entries.v.power_families_by_bucket.energy.members.power_id", True),
    8150186: ("native_dynamic_entries.v.power_families_by_bucket.energy.members", "native_dynamic_entries.v.power_families_by_bucket.energy.members.power_id", True),
    10211368: ("native_dynamic_entries.v.power_families_by_bucket.energy.members", "native_dynamic_entries.v.power_families_by_bucket.energy.members.power_id", True),
    13899656: ("native_dynamic_entries.v.power_families_by_bucket.energy.members", "native_dynamic_entries.v.power_families_by_bucket.energy.members.power_id", True),
    10419231: ("native_dynamic_entries.v.heroes", "native_dynamic_entries.v.heroes.hero_id", True),
    15401530: ("native_dynamic_entries.v.heroes", "native_dynamic_entries.v.heroes.hero_id", True),
    # thrombosis_prediction
    85245: ("native_dynamic_entries.v.panels", "native_dynamic_entries.v.panels.measurements_by_code.HGB.value", False),
    2857594: ("native_dynamic_entries.v.panels", "native_dynamic_entries.v.panels.measurements_by_code.HGB.value", False),
    14875871: ("native_dynamic_entries.v.panels", "native_dynamic_entries.v.panels.measurements_by_code.HGB.value", False),
    15384177: ("native_dynamic_entries.v.panels", "native_dynamic_entries.v.panels.measurements_by_code.GPT.value", False),
    4052777: ("native_dynamic_entries.v.readings", "native_dynamic_entries.v.readings.value", False),
    15260973: ("native_dynamic_entries.v.readings", "native_dynamic_entries.v.readings.patient_id", True),
    # european_football_2
    5498271: ("native_dynamic_entries.v.fixtures", "native_dynamic_entries.v.fixtures.stage", False),
    6151606: ("native_dynamic_entries.v.snapshots", "native_dynamic_entries.v.snapshots.pace.agility", False),
    11362665: ("native_dynamic_entries.v.snapshots", "native_dynamic_entries.v.snapshots.overall_rating", False),
    12360194: ("native_dynamic_entries.v.teams", "native_dynamic_entries.v.teams.score.home", False),
    13574029: ("native_dynamic_entries.v.snapshots", "native_dynamic_entries.v.snapshots.pace.acceleration", False),
    13586426: ("native_dynamic_entries.v.teams", "native_dynamic_entries.v.teams.match_id", False),
    # debit_card_specializing
    5189720: ("native_dynamic_entries.v.payments", "native_dynamic_entries.v.payments.payment.amount", False),
    7208130: ("native_dynamic_entries.v.station_buckets_by_station_id.448", "native_dynamic_entries.v.station_buckets_by_station_id.448.payment.unit_price", False),
    11794826: ("native_dynamic_entries.v.payments", "native_dynamic_entries.v.payments.payment.unit_price", False),
    12887475: ("native_dynamic_entries.v.periods", "native_dynamic_entries.v.periods.consumption", False),
    13798419: ("native_dynamic_entries.v.payments", "native_dynamic_entries.v.payments.payment.gross_value", False),
    14515733: ("native_dynamic_entries.v.payments", "native_dynamic_entries.v.payments.payment.gross_value", False),
    15119523: ("native_dynamic_entries.v.payments", "native_dynamic_entries.v.payments.payment.gross_value", False),
    # card_games
    1279316: ("native_dynamic_entries.v.events", "native_dynamic_entries.v.events.ruling_id", False),
    5662975: ("native_dynamic_entries.v.events", "native_dynamic_entries.v.events.ruling_id", False),
    7946324: ("native_dynamic_entries.v.events", "native_dynamic_entries.v.events.ruling_id", False),
    11805318: ("native_dynamic_entries.v.events", "native_dynamic_entries.v.events.ruling_id", False),
    3971045: ("native_dynamic_entries.v.events", "native_dynamic_entries.v.events.legality_id", False),
    4254275: ("native_dynamic_entries.v.events", "native_dynamic_entries.v.events.legality_id", False),
    11040212: ("native_dynamic_entries.v.events", "native_dynamic_entries.v.events.legality_id", False),
    14979006: ("native_dynamic_entries.v.events", "native_dynamic_entries.v.events.legality_id", False),
    # formula_1
    6689251: ("native_dynamic_entries.v.running_order", "native_dynamic_entries.v.running_order.constructor.constructor_id", False),
    7839145: ("native_dynamic_entries.v.running_order", "native_dynamic_entries.v.running_order.timing.milliseconds", False),
    10918606: ("native_dynamic_entries.v.running_order", "native_dynamic_entries.v.running_order.driver.number", False),
    16133948: ("native_dynamic_entries.v.running_order", "native_dynamic_entries.v.running_order.driver.driver_id", False),
    14105433: ("native_dynamic_entries.v.entries", "native_dynamic_entries.v.entries.finish.position_order", False),
    16687575: ("native_dynamic_entries.v.entries", "native_dynamic_entries.v.entries.finish.laps_completed", False),
}

_DYNAMIC_ARRAY_THRESHOLD_REPAIRS = {
    # california_schools
    1219683, 2528331, 6077381, 9476086, 13806349,
    # debit_card_specializing
    3021436, 4145056, 5148104, 7041568, 7922693, 11571227, 12174324, 15369043,
    # european_football_2
    11684408, 13212451, 13754006, 13851587, 14047888, 15943783,
    # card_games
    3805913, 5703340, 7386746, 4148176, 6267960, 6565164,
    # formula_1
    5300872, 5324476, 11130947, 16053650, 8168694, 8614423, 9250161,
}


def _record_id(record: dict[str, Any]) -> int | None:
    value = record.get("record_id")
    return value if isinstance(value, int) else None


def _repair_dynamic_metric_array_sum(
    record: dict[str, Any],
    pipeline: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    spec = _DYNAMIC_METRIC_ARRAY_SUM_REPAIRS.get(_record_id(record) or -1)
    if spec is None:
        return pipeline, False
    array_path, scalar_path, count_items = spec
    changed = _insert_unwind_after_dynamic_entry_unwind(pipeline, array_path)
    for stage in pipeline:
        match = stage.get("$match") if isinstance(stage, dict) else None
        if isinstance(match, dict):
            for key in list(match):
                if _field_path_goes_through_array(str(key), array_path):
                    match.pop(key, None)
                    match[scalar_path] = {"$ne": None}
                    changed = True
        group = stage.get("$group") if isinstance(stage, dict) else None
        if isinstance(group, dict) and "metric_total" in group:
            group["metric_total"] = {"$sum": 1 if count_items else f"${scalar_path}"}
            changed = True
    changed = _repair_grouped_dynamic_sort(pipeline) or changed
    return pipeline, changed


def _insert_unwind_after_dynamic_entry_unwind(
    pipeline: list[dict[str, Any]],
    array_path: str,
) -> bool:
    unwind_stage = {"$unwind": f"${array_path}"}
    if any(stage == unwind_stage for stage in pipeline if isinstance(stage, dict)):
        return False
    for index, stage in enumerate(pipeline):
        unwind = stage.get("$unwind") if isinstance(stage, dict) else None
        if unwind == "$native_dynamic_entries":
            pipeline.insert(index + 1, unwind_stage)
            return True
        if isinstance(unwind, dict) and unwind.get("path") == "$native_dynamic_entries":
            pipeline.insert(index + 1, unwind_stage)
            return True
    return False


def _field_path_goes_through_array(field_path: str, array_path: str) -> bool:
    return field_path == array_path or field_path.startswith(f"{array_path}.")


def _repair_dynamic_array_threshold(
    record: dict[str, Any],
    pipeline: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    if (_record_id(record) or -1) not in _DYNAMIC_ARRAY_THRESHOLD_REPAIRS:
        return pipeline, False
    for index, stage in enumerate(pipeline):
        group = stage.get("$group") if isinstance(stage, dict) else None
        if not isinstance(group, dict) or "above_threshold" not in group:
            continue
        extracted = _extract_threshold_condition(group["above_threshold"])
        if extracted is None:
            continue
        op, field_path, threshold = extracted
        array_path = _array_path_from_scalar_path(field_path)
        if array_path is None:
            continue
        relative_path = field_path[len(array_path) + 1:]
        if not _threshold_hit_stage_exists(pipeline[:index]):
            pipeline.insert(index, {
                "$addFields": {
                    "native_threshold_hit": {
                        "$anyElementTrue": {
                            "$map": {
                                "input": {"$ifNull": [f"${array_path}", []]},
                                "as": "item",
                                "in": {op: [f"$$item.{relative_path}", threshold]},
                            }
                        }
                    }
                }
            })
        group["above_threshold"] = {
            "$sum": {"$cond": ["$native_threshold_hit", 1, 0]}
        }
        _repair_grouped_dynamic_sort(pipeline)
        return pipeline, True
    return pipeline, False


def _extract_threshold_condition(value: Any) -> tuple[str, str, int | float] | None:
    if not isinstance(value, dict):
        return None
    cond = value.get("$sum", {}).get("$cond") if isinstance(value.get("$sum"), dict) else None
    if not isinstance(cond, list) or not cond:
        return None
    predicate = cond[0]
    if not isinstance(predicate, dict):
        return None
    for op in ("$gte", "$gt", "$lte", "$lt"):
        args = predicate.get(op)
        if (
            isinstance(args, list)
            and len(args) == 2
            and isinstance(args[0], str)
            and args[0].startswith("$")
            and isinstance(args[1], (int, float))
        ):
            return op, _normalize_sort_field(args[0]), args[1]
    return None


def _array_path_from_scalar_path(field_path: str) -> str | None:
    parts = field_path.split(".")
    for segment in (
        "payments",
        "periods",
        "events",
        "fixtures",
        "snapshots",
        "teams",
        "running_order",
        "entries",
        "readings",
        "panels",
        "grade_spans",
    ):
        if segment in parts:
            index = parts.index(segment)
            return ".".join(parts[:index + 1])
    marker = "station_buckets_by_station_id"
    if marker in parts:
        index = parts.index(marker)
        if len(parts) > index + 1:
            return ".".join(parts[:index + 2])
    return None


def _threshold_hit_stage_exists(pipeline: list[dict[str, Any]]) -> bool:
    return any(
        isinstance(stage, dict)
        and isinstance(stage.get("$addFields"), dict)
        and "native_threshold_hit" in stage["$addFields"]
        for stage in pipeline
    )


def _repair_grouped_dynamic_sort(pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    for index, stage in enumerate(pipeline):
        sort = stage.get("$sort") if isinstance(stage, dict) else None
        if not isinstance(sort, dict):
            continue
        if not any("$group" in prior for prior in pipeline[:index] if isinstance(prior, dict)):
            continue
        stale = {"native_context_bucket", "native_dynamic_entries"}
        if not (set(sort) & stale):
            continue
        group = _nearest_group(pipeline, index)
        if group is None:
            continue
        if "metric_total" in group:
            replacement = {"metric_total": -1, "entry_count": -1, "_id.context": 1, "_id.native_key": 1}
        elif "above_threshold" in group:
            replacement = {"above_threshold": -1, "observed": -1, "_id.context": 1, "_id.native_key": 1}
        elif "document_count" in group:
            replacement = {"document_count": -1, "_id.context": 1, "_id.native_key": 1}
        else:
            replacement = {"_id": 1}
        sort.clear()
        sort.update(replacement)
        changed = True
    return changed


def _nearest_group(pipeline: list[dict[str, Any]], before_index: int) -> dict[str, Any] | None:
    for prior in reversed(pipeline[:before_index]):
        group = prior.get("$group") if isinstance(prior, dict) else None
        if isinstance(group, dict):
            return group
    return None


def _normalize_sort_field(value: str) -> str:
    text = str(value)
    while text.startswith("$"):
        text = text[1:]
    return text


def _financial_loan_schedule_pipeline(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = _first_limit(pipeline) or 50
    return [
        {
            "$project": {
                "account_id": "$identity.account_id",
                "frequency": "$identity.service_plan.frequency_key",
                "district": "$district_context.name",
                "region": "$district_context.region",
                "salary": "$district_context.avg_salary",
                "loan_status": "$loan.contract.status_bucket",
                "dues": {"$objectToArray": {"$ifNull": ["$loan.repayment_schedule.by_due_month", {}]}},
                "observed_months": {
                    "$objectToArray": {
                        "$ifNull": ["$loan.observed_loan_flows.transactions_by_month", {}]
                    }
                },
            }
        },
        {"$unwind": "$dues"},
        {
            "$addFields": {
                "observed_due": {
                    "$arrayElemAt": [
                        {
                            "$filter": {
                                "input": "$observed_months",
                                "as": "obs",
                                "cond": {"$eq": ["$$obs.k", "$dues.k"]},
                            }
                        },
                        0,
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": {
                    "loan_status": "$loan_status",
                    "region": "$region",
                    "year": {"$substr": ["$dues.k", 0, 4]},
                },
                "due_months": {"$sum": 1},
                "scheduled_total": {"$sum": "$dues.v.scheduled_payment"},
                "paid_total": {"$sum": {"$ifNull": ["$observed_due.v.withdrawal_amount", 0]}},
                "avg_salary": {"$avg": "$salary"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "loan_status": "$_id.loan_status",
                "region": "$_id.region",
                "year": "$_id.year",
                "due_months": 1,
                "scheduled_total": 1,
                "paid_total": 1,
                "avg_salary": 1,
            }
        },
        {
            "$sort": {
                "scheduled_total": -1,
                "paid_total": -1,
                "due_months": -1,
                "loan_status": 1,
                "region": 1,
                "year": 1,
            }
        },
        {"$limit": limit},
    ]


def _student_officer_budget_attendee_pipeline(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = _first_limit(pipeline) or 50
    officer_roles = ["President", "Vice President", "Treasurer", "Secretary"]
    return [
        {"$unwind": "$attendance.attendees"},
        {
            "$project": {
                "_id": 0,
                "event_id": "$event.event_id",
                "event_name": "$event.name",
                "event_type": "$event.type",
                "event_status": "$event.status",
                "member_id": "$attendance.attendees.account.member_id",
                "member": "$attendance.attendees.account.profile.display_name",
                "role": "$attendance.attendees.attendance_context.role",
                "finance_entries": {
                    "$objectToArray": {
                        "$ifNull": ["$attendance.attendees.finance_by_category", {}]
                    }
                },
                "budget_entries": {"$objectToArray": {"$ifNull": ["$budget_by_category", {}]}},
            }
        },
        {
            "$match": {
                "role": {"$in": officer_roles},
                "$expr": {
                    "$and": [
                        {"$gt": [{"$size": "$finance_entries"}, 0]},
                        {"$gt": [{"$size": "$budget_entries"}, 0]},
                    ]
                },
            }
        },
        {"$unwind": "$finance_entries"},
        {"$unwind": "$budget_entries"},
        {"$match": {"$expr": {"$eq": ["$budget_entries.k", "$finance_entries.k"]}}},
        {
            "$project": {
                "event_id": 1,
                "event_name": 1,
                "event_type": 1,
                "event_status": 1,
                "member_id": 1,
                "member": 1,
                "role": 1,
                "category": "$finance_entries.k",
                "category_spent": "$finance_entries.v.expense_total",
                "category_remaining": "$budget_entries.v.remaining_total",
                "category_allocated": "$budget_entries.v.amount_total",
                "member_income_total": "$finance_entries.v.income_context.member_income_total",
                "income_presence_state": "$finance_entries.v.income_context.income_presence_state",
            }
        },
        {
            "$sort": {
                "event_id": 1,
                "role": 1,
                "category": 1,
                "member_id": 1,
            }
        },
        {"$limit": limit},
    ]


def _repair_student_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    rid = _record_id(record)
    if str(record.get("native_query_pattern") or "") == "student_club_member_participation_timeline":
        pipeline[:] = _student_member_event_timeline_pipeline(pipeline)
        changed = True
    if str(record.get("native_query_pattern") or "") == "nested_event_filter":
        for stage in pipeline:
            add_fields = stage.get("$addFields") if isinstance(stage, dict) else None
            filtered = add_fields.get("native_filtered_events") if isinstance(add_fields, dict) else None
            if isinstance(filtered, dict) and "$filter" in filtered:
                filtered["$filter"] = {
                    "input": {
                        "$cond": [
                            {"$isArray": "$participation.events"},
                            "$participation.events",
                            [],
                        ]
                    },
                    "as": "event",
                    "cond": {
                        "$and": [
                            {"$ne": ["$$event.event.type", None]},
                            {"$ne": ["$$event.event.date", None]},
                        ]
                    },
                }
                changed = True
    if rid in {10347624, 11134219}:
        changed = _replace_metric_path(
            pipeline,
            "native_dynamic_entries.v.budgets.amounts.allocated",
            "native_dynamic_entries.v.amount_total",
        ) or changed
        changed = _repair_grouped_dynamic_sort(pipeline) or changed
    if rid == 9237680 and _first_limit(pipeline) is None:
        pipeline.append({"$limit": 25})
        changed = True
    if str(record.get("native_query_pattern") or "") == "student.guest_speaker_member_budget_mix":
        for stage in pipeline:
            project = stage.get("$project") if isinstance(stage, dict) else None
            if isinstance(project, dict) and project.get("speaker_events") == 1:
                project["speaker_events"] = {
                    "$sortArray": {"input": "$speaker_events", "sortBy": 1}
                }
                changed = True
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "speaker_events" in sort:
                sort.pop("speaker_events", None)
                changed = True
    return changed


def _student_member_event_timeline_pipeline(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = _first_limit(pipeline) or 25
    return [
        {
            "$addFields": {
                "native_filtered_events": {
                    "$filter": {
                        "input": {
                            "$cond": [
                                {"$isArray": "$participation.events"},
                                "$participation.events",
                                [],
                            ]
                        },
                        "as": "event",
                        "cond": {"$ne": ["$$event.event.date", None]},
                    }
                }
            }
        },
        {"$unwind": "$native_filtered_events"},
        {
            "$project": {
                "_id": 0,
                "event": "$native_filtered_events.event.name",
                "event_date": "$native_filtered_events.event.date",
                "event_type": "$native_filtered_events.event.type",
                "member": "$account.profile.display_name",
                "role": "$account.club_role.position",
            }
        },
        {"$sort": {"event_date": 1, "member": 1, "event_type": 1, "role": 1, "event": 1}},
        {"$limit": limit},
    ]


def _replace_metric_path(
    pipeline: list[dict[str, Any]],
    old_path: str,
    new_path: str,
) -> bool:
    changed = False
    for stage in pipeline:
        match = stage.get("$match") if isinstance(stage, dict) else None
        if isinstance(match, dict) and old_path in match:
            match[new_path] = match.pop(old_path)
            changed = True
        group = stage.get("$group") if isinstance(stage, dict) else None
        if isinstance(group, dict) and group.get("metric_total") == {"$sum": f"${old_path}"}:
            group["metric_total"] = {"$sum": f"${new_path}"}
            changed = True
    return changed


def _repair_superhero_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    rid = _record_id(record)
    if str(record.get("native_query_pattern") or "") == "hero.power_alignment_completeness":
        changed = _replace_expression_string(
            pipeline,
            "$alignments.v.heroes.full_name.presence_state",
            "$alignments.v.heroes.full_name_presence_state",
        ) or changed
    context_replacements = {
        777816: "$power.name.presence_state",
        14153640: "$schema_state.hero_memberships",
        15124155: "$schema_state.hero_memberships",
        16227444: "$power.name.value",
    }
    replacement = context_replacements.get(rid or -1)
    if replacement is not None:
        changed = _replace_native_context_bucket_source(pipeline, replacement) or changed
    return changed


def _replace_expression_string(value: Any, old: str, new: str) -> bool:
    changed = False
    if isinstance(value, dict):
        for key, child in value.items():
            if child == old:
                value[key] = new
                changed = True
            else:
                changed = _replace_expression_string(child, old, new) or changed
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if child == old:
                value[index] = new
                changed = True
            else:
                changed = _replace_expression_string(child, old, new) or changed
    return changed


def _replace_native_context_bucket_source(
    pipeline: list[dict[str, Any]],
    source_path: str,
) -> bool:
    for stage in pipeline:
        add_fields = stage.get("$addFields") if isinstance(stage, dict) else None
        if isinstance(add_fields, dict) and "native_context_bucket" in add_fields:
            add_fields["native_context_bucket"] = {"$ifNull": [source_path, "missing"]}
            return True
    return False


def _repair_toxicology_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    if _record_id(record) in {3401204, 7860224, 8752889, 9547625, 1698876}:
        for stage in pipeline:
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "neighbor_degree" in sort:
                sort.clear()
                sort.update({"neighbor_degree": -1, "molecule_id": 1, "carbon_atom": 1})
                changed = True
    if _record_id(record) == 12354042:
        for stage in pipeline:
            group = stage.get("$group") if isinstance(stage, dict) else None
            if isinstance(group, dict):
                group["_id"] = {
                    "context": "$native_context_bucket",
                    "presence": "$native_presence_state",
                }
                changed = True
    pattern = str(record.get("native_query_pattern") or record.get("archetype") or "")
    if pattern == "thrombosis.diagnosis_risk_mix":
        for stage in pipeline:
            project = stage.get("$project") if isinstance(stage, dict) else None
            if isinstance(project, dict) and project.get("sample_patients") == {"$slice": ["$sample_patients", 5]}:
                project["sample_patients"] = {
                    "$slice": [
                        {"$sortArray": {"input": "$sample_patients", "sortBy": 1}},
                        5,
                    ]
                }
                changed = True
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "sample_patients" in sort:
                sort.pop("sample_patients", None)
                changed = True
    return changed


def _repair_thrombosis_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    if str(record.get("native_query_pattern") or "") == "thrombosis.diagnosis_risk_mix":
        for stage in pipeline:
            project = stage.get("$project") if isinstance(stage, dict) else None
            if isinstance(project, dict) and project.get("sample_patients") == {"$slice": ["$sample_patients", 5]}:
                project["sample_patients"] = {
                    "$slice": [
                        {"$sortArray": {"input": "$sample_patients", "sortBy": 1}},
                        5,
                    ]
                }
                changed = True
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "sample_patients" in sort:
                sort.pop("sample_patients", None)
                changed = True
    return changed


def _repair_european_football_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    if _record_id(record) in {
        3658140, 3893172, 3946405, 5268587, 5346218, 6358764, 6999991,
        8073643, 9261814, 9604733, 10058478, 10254917, 10312211,
        11982713, 14146807, 15222770, 15229851, 15372655, 15827514,
        16767099,
    }:
        for stage in pipeline:
            add_fields = stage.get("$addFields") if isinstance(stage, dict) else None
            if isinstance(add_fields, dict) and "native_filtered_events" in add_fields:
                add_fields["native_filtered_events"] = {
                    "$filter": {
                        "input": {
                            "$cond": [
                                {"$isArray": "$lineups.home.players"},
                                "$lineups.home.players",
                                [],
                            ]
                        },
                        "as": "player",
                        "cond": {
                            "$and": [
                                {"$ne": ["$$player.player.player_api_id", None]},
                                {"$eq": ["$$player.coordinates.state", "present"]},
                            ]
                        },
                    }
                }
                changed = True
            project = stage.get("$project") if isinstance(stage, dict) else None
            if isinstance(project, dict) and "native_event_count" in project:
                project.pop("lineups.home.players", None)
                project["native_filtered_players"] = "$native_filtered_events"
                changed = True
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "lineups.home.players" in sort:
                sort.clear()
                sort.update({"native_event_count": -1, "_id": 1, "native_context_bucket": 1})
                changed = True
    return changed


def _repair_debit_card_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    rid = _record_id(record)
    if str(record.get("native_query_pattern") or "") == "debit.customer_events_segment_gross":
        pipeline[:] = _debit_customer_events_segment_gross_pipeline(pipeline)
        changed = True
    if rid in {87075, 173800, 1153283, 1806736, 2120914, 3648783}:
        for stage in pipeline:
            group = stage.get("$group") if isinstance(stage, dict) else None
            if isinstance(group, dict) and "transaction_gross_total" in group:
                group.pop("transaction_gross_total", None)
                changed = True
            project = stage.get("$project") if isinstance(stage, dict) else None
            if isinstance(project, dict) and "transaction_gross_total" in project:
                project.pop("transaction_gross_total", None)
                changed = True
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "consumption_total" in sort:
                sort.pop("transaction_gross_total", None)
                changed = True
    if rid in {8399871, 11253255}:
        changed = _replace_threshold_operator(pipeline, "$gte", "$gt", only_zero=True) or changed
        changed = _repair_grouped_dynamic_sort(pipeline) or changed
    if rid == 10662101:
        for stage in pipeline:
            match = stage.get("$match") if isinstance(stage, dict) else None
            if isinstance(match, dict):
                spec = match.get("native_dynamic_entries.v.transaction_count")
                if spec in ({"$gte": 0}, {"$gt": 0}):
                    match["native_dynamic_entries.v.transaction_count"] = {"$eq": 0}
                    changed = True
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "transaction_count" in sort:
                sort.clear()
                sort.update({"transaction_count": -1, "_id": 1, "native_context_bucket": 1, "native_key": 1})
                changed = True
        nlq = record.setdefault("nl_queries", {})
        if isinstance(nlq, dict):
            nlq["canonical"] = (
                "Inspect spend.consumption_by_month dynamic key '2012-02' and return up to "
                "25 fuel customer spend rows whose transaction_count equals 0, bucketed by "
                "identity.currency.value."
            )
            nlq["colloquial"] = (
                "Show up to 25 fuel customer rows where the 2012-02 monthly consumption key "
                "has zero transactions, grouped by currency context."
            )
    if rid == 10348001:
        for stage in pipeline:
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict):
                sort.clear()
                sort.update({"gross_total": -1, "_id.category": 1, "_id.fuel_class": 1, "transaction_count": -1})
                changed = True
    return changed


def _debit_customer_events_segment_gross_pipeline(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = _first_limit(pipeline) or 25
    return [
        {
            "$addFields": {
                "native_filtered_events": {
                    "$filter": {
                        "input": {"$ifNull": ["$transactions.events", []]},
                        "as": "event",
                        "cond": {"$gt": ["$$event.payment.gross_value", 0]},
                    }
                }
            }
        },
        {"$unwind": "$native_filtered_events"},
        {
            "$project": {
                "segment": "$identity.segment.value",
                "currency": "$identity.currency.value",
                "country": "$native_filtered_events.merchant_context.country_bucket",
                "station_segment": "$native_filtered_events.merchant_context.segment_bucket",
                "category": "$native_filtered_events.basket.category",
                "gross": "$native_filtered_events.payment.gross_value",
                "amount": "$native_filtered_events.payment.amount",
                "month": "$native_filtered_events.occurred_at.month",
            }
        },
        {
            "$group": {
                "_id": {
                    "segment": "$segment",
                    "country": "$country",
                    "station_segment": "$station_segment",
                    "category": "$category",
                    "month": "$month",
                },
                "transaction_count": {"$sum": 1},
                "gross_total": {"$sum": "$gross"},
                "liters_total": {"$sum": "$amount"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "customer_segment": "$_id.segment",
                "country": "$_id.country",
                "station_segment": "$_id.station_segment",
                "category": "$_id.category",
                "month": "$_id.month",
                "transaction_count": 1,
                "gross_total": 1,
                "liters_total": 1,
            }
        },
        {
            "$sort": {
                "gross_total": -1,
                "customer_segment": 1,
                "country": 1,
                "station_segment": 1,
                "category": 1,
                "month": 1,
                "transaction_count": 1,
                "liters_total": 1,
            }
        },
        {"$limit": limit},
    ]


def _replace_threshold_operator(
    value: Any,
    old_op: str,
    new_op: str,
    *,
    only_zero: bool,
) -> bool:
    changed = False
    if isinstance(value, dict):
        for key, child in list(value.items()):
            if key == old_op and isinstance(child, list) and len(child) == 2:
                if not only_zero or child[1] in (0, 0.0):
                    value[new_op] = value.pop(key)
                    changed = True
                    continue
            changed = _replace_threshold_operator(child, old_op, new_op, only_zero=only_zero) or changed
    elif isinstance(value, list):
        for child in value:
            changed = _replace_threshold_operator(child, old_op, new_op, only_zero=only_zero) or changed
    return changed


def _repair_card_games_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    # Generic array-metric/threshold repair handles the MQL defects. This function
    # keeps a placeholder for DB-specific metadata repairs below.
    return _repair_card_games_metadata(record)


def _repair_california_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    if str(record.get("native_query_pattern") or "") == "california.district_active_charter_spans":
        for stage in pipeline:
            project = stage.get("$project") if isinstance(stage, dict) else None
            if isinstance(project, dict) and project.get("schools") == {"$slice": ["$schools", 5]}:
                project["schools"] = {
                    "$slice": [
                        {"$sortArray": {"input": "$schools", "sortBy": 1}},
                        5,
                    ]
                }
                changed = True
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict) and "schools" in sort:
                sort.pop("schools", None)
                changed = True
    return changed


def _repair_formula_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    rid = _record_id(record)
    pattern = str(record.get("native_query_pattern") or "")
    if pattern == "f1.race_entries_status":
        pipeline[:] = _formula_race_entries_status_pipeline(pipeline)
        changed = True
    if str(record.get("native_query_pattern") or "") == "f1.pit_stop_points_burden":
        pipeline[:] = _formula_pit_stop_points_pipeline(pipeline)
        changed = True
    if rid == 10108714:
        _set_or_append_limit(pipeline, 50)
        changed = _insert_sort_before_limit(pipeline, {"race_id": 1, "race": 1}) or True
    if rid == 14412895:
        changed = _insert_sort_before_limit(pipeline, {"race_id": 1, "race": 1}) or changed
    if rid == 16464910:
        for stage in pipeline:
            sort = stage.get("$sort") if isinstance(stage, dict) else None
            if isinstance(sort, dict):
                sort.clear()
                sort.update({"race_id": 1, "race": 1})
                changed = True
        _set_or_append_limit(pipeline, 50)
        changed = True
    return changed


def _formula_race_entries_status_pipeline(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = _first_limit(pipeline) or 25
    return [
        {
            "$addFields": {
                "native_filtered_events": {
                    "$filter": {
                        "input": {"$ifNull": ["$sessions.race.entries", []]},
                        "as": "entry",
                        "cond": {"$ne": ["$$entry.finish.status", None]},
                    }
                }
            }
        },
        {"$unwind": "$native_filtered_events"},
        {
            "$project": {
                "season": "$calendar.season_year",
                "circuit_country": "$circuit.country",
                "constructor": "$native_filtered_events.constructor.name",
                "driver_nationality": "$native_filtered_events.driver.nationality",
                "status": "$native_filtered_events.finish.status",
                "points": "$native_filtered_events.finish.points",
                "grid": "$native_filtered_events.grid",
                "position_order": "$native_filtered_events.finish.position_order",
            }
        },
        {
            "$group": {
                "_id": {
                    "season": "$season",
                    "country": "$circuit_country",
                    "constructor": "$constructor",
                    "status": "$status",
                },
                "entry_count": {"$sum": 1},
                "points_total": {"$sum": "$points"},
                "avg_grid": {"$avg": "$grid"},
                "avg_finish_order": {"$avg": "$position_order"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "season": "$_id.season",
                "circuit_country": "$_id.country",
                "constructor": "$_id.constructor",
                "finish_status": "$_id.status",
                "entry_count": 1,
                "points_total": 1,
                "avg_grid": 1,
                "avg_finish_order": 1,
            }
        },
        {
            "$sort": {
                "points_total": -1,
                "season": 1,
                "circuit_country": 1,
                "constructor": 1,
                "finish_status": 1,
                "entry_count": 1,
                "avg_grid": 1,
                "avg_finish_order": 1,
            }
        },
        {"$limit": limit},
    ]


def _formula_pit_stop_points_pipeline(pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    limit = _first_limit(pipeline) or 25
    return [
        {
            "$addFields": {
                "native_filtered_events": {
                    "$filter": {
                        "input": {"$ifNull": ["$sessions.race.entries", []]},
                        "as": "entry",
                        "cond": {
                            "$and": [
                                {
                                    "$gte": [
                                        {"$size": {"$ifNull": ["$$entry.pit_stops", []]}},
                                        2,
                                    ]
                                },
                                {"$gt": ["$$entry.finish.points", 0]},
                            ]
                        },
                    }
                }
            }
        },
        {"$unwind": "$native_filtered_events"},
        {
            "$addFields": {
                "pit_count": {
                    "$size": {"$ifNull": ["$native_filtered_events.pit_stops", []]}
                },
                "pit_total_ms": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$native_filtered_events.pit_stops", []]},
                            "as": "p",
                            "in": {"$ifNull": ["$$p.duration.milliseconds", 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "season": "$calendar.season_year",
                "race": "$calendar.race_name",
                "driver": "$native_filtered_events.driver.name.display",
                "constructor": "$native_filtered_events.constructor.name",
                "finish_position": "$native_filtered_events.finish.position_order",
                "points": "$native_filtered_events.finish.points",
                "pit_count": 1,
                "pit_total_ms": 1,
                "result_id": "$native_filtered_events.result_id",
            }
        },
        {
            "$sort": {
                "pit_count": -1,
                "points": -1,
                "pit_total_ms": -1,
                "season": 1,
                "race": 1,
                "driver": 1,
                "result_id": 1,
                "constructor": 1,
                "finish_position": 1,
            }
        },
        {"$limit": limit},
    ]


def _insert_sort_before_limit(pipeline: list[dict[str, Any]], sort_spec: dict[str, Any]) -> bool:
    if any("$sort" in stage for stage in pipeline if isinstance(stage, dict)):
        return False
    for index, stage in enumerate(pipeline):
        if isinstance(stage, dict) and "$limit" in stage:
            pipeline.insert(index, {"$sort": dict(sort_spec)})
            return True
    pipeline.append({"$sort": dict(sort_spec)})
    return True


def _set_or_append_limit(pipeline: list[dict[str, Any]], limit: int) -> None:
    for stage in pipeline:
        if isinstance(stage, dict) and "$limit" in stage:
            stage["$limit"] = limit
            return
    pipeline.append({"$limit": limit})


def _repair_codebase_community_metadata(record: dict[str, Any]) -> bool:
    rid = _record_id(record)
    if rid in {564928, 1286333, 2061444, 2724444, 2959894, 8329834}:
        return _set_feature_metadata(
            record,
            "community_threads.thread_tags_votes",
            "taxonomy.tags_by_name",
        )
    if rid in {1337298, 1563731, 3410185, 3885973, 4346931, 4698937, 7402873}:
        return _set_feature_metadata(
            record,
            "community_threads.answer_vote_buckets",
            "answers.items.votes_by_type",
        )
    if rid == 1653653:
        return _set_feature_metadata(
            record,
            "tag_topic_ecosystems.topic_status_year_buckets",
            "threads_by_status_by_year",
        )
    return False


def _repair_card_games_metadata(record: dict[str, Any]) -> bool:
    rid = _record_id(record)
    if rid in {1643740, 2149731, 5578440, 6777963, 8453470}:
        return _set_feature_metadata(
            record,
            "set_release_ecosystems.rarity_translation_balance",
            "cards_by_rarity",
        )
    if rid in {1412245, 6815229, 1844689, 3611659, 2562431, 6572748}:
        return _set_feature_metadata(
            record,
            "card_print_dossiers.materialized_legality_translation_views",
            "views",
        )
    if rid in {1907119, 3819592}:
        return _set_feature_metadata(
            record,
            "card_print_dossiers.legality_language",
            "legality.by_format",
        )
    return False


def _set_feature_metadata(
    record: dict[str, Any],
    feature_id: str,
    feature_field: str,
) -> bool:
    changed = False
    for key in ("schema_feature", "native_feature_id"):
        if record.get(key) != feature_id:
            record[key] = feature_id
            changed = True
    metadata = record.get("native_metadata")
    if isinstance(metadata, dict):
        if metadata.get("feature_id") != feature_id:
            metadata["feature_id"] = feature_id
            changed = True
        if metadata.get("feature_field") != feature_field:
            metadata["feature_field"] = feature_field
            changed = True
    return changed


def _drop_unsafe_sort_keys(pipeline: list[dict[str, Any]]) -> bool:
    changed = False
    for stage in pipeline:
        sort = stage.get("$sort") if isinstance(stage, dict) else None
        if not isinstance(sort, dict):
            continue
        for key in list(sort):
            if _sort_key_is_known_array_or_object(str(key)):
                sort.pop(key, None)
                changed = True
        if not sort:
            sort["_id"] = 1
            changed = True
    return changed


def _sort_key_is_known_array_or_object(key: str) -> bool:
    if key == "native_dynamic_entries":
        return True
    if key.startswith("native_filtered"):
        return True
    return key in {
        "feature_path",
        "lineups.home.players",
        "participation.events",
        "timeline.events",
        "category_buckets",
        "event",
        "speaker_events",
        "schools",
        "sample_patients",
    }


def _repair_nlq_for_known_pipeline(record: dict[str, Any], pipeline: list[dict[str, Any]]) -> None:
    db_id = str(record.get("db_id") or "")
    pattern = str(record.get("native_query_pattern") or record.get("archetype") or "")
    limit = _first_limit(pipeline)
    if limit is None:
        return
    nlq = record.setdefault("nl_queries", {})
    if not isinstance(nlq, dict):
        return
    if db_id == "financial" and pattern == "financial.loan_schedule":
        nlq["canonical"] = (
            "Summarize loan repayment schedule due months by loan status, region, and "
            f"year; return the top {limit} groups by scheduled payment total, breaking "
            "ties by observed paid total and due-month count, including scheduled total, "
            "paid total, due-month count, and average district salary."
        )
        nlq["colloquial"] = (
            f"Show the top {limit} loan schedule status-region-year groups by scheduled "
            "payment total, with paid total and due-month count included."
        )
    elif db_id == "formula_1" and pattern == "f1.actor_career":
        nlq["canonical"] = (
            "Summarize Formula 1 actor career years by entity type, nationality, and "
            f"year; return the top {limit} groups by points total with actor-year counts."
        )
        nlq["colloquial"] = (
            f"Show the top {limit} F1 career-year groups by total points."
        )
    elif db_id == "student_club" and pattern == "student.officer_budget_attendee":
        nlq["canonical"] = (
            "Find officer attendees with per-attendee finance category records matched "
            f"to event budget categories; return up to {limit} event-member-category rows "
            "with category spent, remaining budget, allocated budget, member income total, "
            "and income presence state."
        )
        nlq["colloquial"] = (
            f"Show up to {limit} officer attendance finance-category rows with spending, "
            "remaining budget, allocated budget, and member income."
        )


def _refresh_mql_grounded_nlq(
    record: dict[str, Any],
    collection: str,
    pipeline: list[dict[str, Any]],
) -> None:
    nlq = record.setdefault("nl_queries", {})
    if not isinstance(nlq, dict):
        return
    limit = _first_limit(pipeline)
    constants = _literal_constants_for_nlq(pipeline)
    thresholds = _threshold_numbers_for_nlq(pipeline)
    predicate_parts = _predicate_parts_for_nlq(pipeline)
    dynamic_sources = _dynamic_sources_for_nlq(pipeline)
    group_keys = _group_keys_for_nlq(pipeline)
    context_sources = _context_sources_for_nlq(pipeline)
    field_refs = _field_refs_for_nlq(pipeline)
    sort_parts = _sort_parts_for_nlq(pipeline)
    output_fields = _output_fields_for_nlq(pipeline)
    pattern = str(record.get("native_query_pattern") or record.get("archetype") or "aggregation")
    grouped = any("$group" in stage for stage in pipeline if isinstance(stage, dict))
    row_kind = "aggregate groups" if grouped else "documents"

    limit_text = f"return the top {limit} {row_kind}" if limit is not None else f"return {row_kind}"
    sort_text = "; order by " + ", ".join(sort_parts) if sort_parts else ""
    constants_text = "; require constants " + ", ".join(constants) if constants else ""
    thresholds_text = "; apply numeric thresholds " + ", ".join(thresholds) if thresholds else ""
    predicates_text = "; predicate fields " + ", ".join(predicate_parts[:10]) if predicate_parts else ""
    dynamic_text = "; expand dynamic sources " + ", ".join(dynamic_sources[:8]) if dynamic_sources else ""
    group_text = "; group by " + ", ".join(group_keys[:8]) if group_keys else ""
    context_text = "; context source " + ", ".join(context_sources[:4]) if context_sources else ""
    refs_text = "; reference fields " + ", ".join(field_refs[:16]) if field_refs else ""
    output_text = "; output fields " + ", ".join(output_fields[:12]) if output_fields else ""

    nlq["canonical"] = (
        f"On `{collection}` for `{pattern}`, {limit_text}{sort_text}"
        f"{constants_text}{thresholds_text}{predicates_text}{dynamic_text}{group_text}"
        f"{context_text}{refs_text}{output_text}."
    )

    colloquial_limit = f"Show the top {limit} {row_kind}" if limit is not None else f"Show {row_kind}"
    colloquial_sort = " sorted by " + ", ".join(sort_parts) if sort_parts else ""
    colloquial_filters: list[str] = []
    if constants:
        colloquial_filters.append("constants " + ", ".join(constants))
    if thresholds:
        colloquial_filters.append("thresholds " + ", ".join(thresholds))
    if predicate_parts:
        colloquial_filters.append("predicates " + ", ".join(predicate_parts[:6]))
    if dynamic_sources:
        colloquial_filters.append("dynamic sources " + ", ".join(dynamic_sources[:4]))
    if context_sources:
        colloquial_filters.append("context source " + ", ".join(context_sources[:2]))
    filter_text = " using " + " and ".join(colloquial_filters) if colloquial_filters else ""
    ref_text = " referencing " + ", ".join(field_refs[:8]) if field_refs else ""
    field_text = " with fields " + ", ".join(output_fields[:8]) if output_fields else ""
    nlq["colloquial"] = (
        f"{colloquial_limit} from `{collection}`{colloquial_sort}{filter_text}{ref_text}{field_text}."
    )


def _dynamic_sources_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    sources: set[str] = set()
    _collect_dynamic_sources_for_nlq(pipeline, sources)
    return sorted(sources)


def _collect_dynamic_sources_for_nlq(value: Any, sources: set[str]) -> None:
    if isinstance(value, dict):
        if "$objectToArray" in value:
            source = _source_path_for_nlq(value["$objectToArray"])
            if source:
                sources.add(source)
        for child in value.values():
            _collect_dynamic_sources_for_nlq(child, sources)
    elif isinstance(value, list):
        for item in value:
            _collect_dynamic_sources_for_nlq(item, sources)


def _source_path_for_nlq(value: Any) -> str | None:
    if isinstance(value, str):
        return _trim_mql_var_prefix(value)
    if isinstance(value, dict):
        if "$ifNull" in value and isinstance(value["$ifNull"], list) and value["$ifNull"]:
            return _source_path_for_nlq(value["$ifNull"][0])
    return None


def _group_keys_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for stage in pipeline:
        group = stage.get("$group") if isinstance(stage, dict) else None
        if not isinstance(group, dict):
            continue
        group_id = group.get("_id")
        if isinstance(group_id, dict):
            for key, expression in group_id.items():
                keys.append(_field_binding_for_nlq(str(key), expression))
        elif group_id is not None:
            keys.append(_source_path_for_nlq(group_id) or str(group_id))
    return _dedupe_keep_order(keys)


def _field_binding_for_nlq(label: str, expression: Any) -> str:
    source = _source_path_for_nlq(expression)
    return f"{label}:{source}" if source and source != label else label


def _predicate_parts_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    parts: list[str] = []
    for stage in pipeline:
        match = stage.get("$match") if isinstance(stage, dict) else None
        if isinstance(match, dict):
            _collect_predicate_parts_for_nlq(match, parts, parent_path=None)
    return _dedupe_keep_order(parts)


def _context_sources_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    sources: list[str] = []
    for stage in pipeline:
        for op in ("$addFields", "$set", "$project"):
            spec = stage.get(op) if isinstance(stage, dict) else None
            if not isinstance(spec, dict) or "native_context_bucket" not in spec:
                continue
            _collect_field_refs(spec["native_context_bucket"], sources)
    return _dedupe_keep_order(sources)


def _field_refs_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for stage in pipeline:
        _collect_field_refs(stage, refs)
    generated_prefixes = (
        "native_dynamic_entries",
        "native_filtered_events",
        "native_matching_dynamic_entries",
        "native_context_bucket",
        "native_dynamic_key_count",
        "native_event_count",
    )
    return [
        ref
        for ref in _dedupe_keep_order(refs)
        if not any(ref == prefix or ref.startswith(prefix + ".") for prefix in generated_prefixes)
    ]


def _collect_field_refs(value: Any, refs: list[str]) -> None:
    if isinstance(value, str):
        if value.startswith("$"):
            ref = _trim_mql_var_prefix(value)
            if ref:
                refs.append(ref)
        return
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if not key_text.startswith("$") and "." in key_text:
                refs.append(key_text)
            _collect_field_refs(child, refs)
    elif isinstance(value, list):
        for item in value:
            _collect_field_refs(item, refs)


def _collect_predicate_parts_for_nlq(
    value: Any,
    parts: list[str],
    *,
    parent_path: str | None,
) -> None:
    if not isinstance(value, dict):
        if parent_path is not None and not isinstance(value, (dict, list)):
            parts.append(f"{parent_path}={_literal_for_nlq(value)}")
        return
    for key, child in value.items():
        key_text = str(key)
        if key_text in {"$and", "$or", "$nor"} and isinstance(child, list):
            for item in child:
                _collect_predicate_parts_for_nlq(item, parts, parent_path=parent_path)
            continue
        if key_text == "$expr":
            expr = _expr_summary_for_nlq(child)
            if expr:
                parts.append(expr)
            continue
        if key_text.startswith("$"):
            if parent_path is not None:
                parts.append(f"{parent_path}{key_text}{_literal_for_nlq(child)}")
            continue
        if isinstance(child, dict):
            _collect_predicate_parts_for_nlq(child, parts, parent_path=key_text)
        else:
            parts.append(f"{key_text}={_literal_for_nlq(child)}")


def _expr_summary_for_nlq(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    for op, args in value.items():
        if not str(op).startswith("$"):
            continue
        refs: list[str] = []
        literals: list[str] = []
        _collect_expr_refs_and_literals(args, refs, literals)
        refs_text = "|".join(_dedupe_keep_order(refs)[:4])
        literals_text = "|".join(_dedupe_keep_order(literals)[:4])
        if refs_text and literals_text:
            return f"expr{op}({refs_text};{literals_text})"
        if refs_text:
            return f"expr{op}({refs_text})"
    return None


def _collect_expr_refs_and_literals(value: Any, refs: list[str], literals: list[str]) -> None:
    if isinstance(value, str):
        if value.startswith("$"):
            refs.append(_trim_mql_var_prefix(value))
        else:
            literals.append(_literal_for_nlq(value))
    elif isinstance(value, (int, float, bool)) or value is None:
        literals.append(_literal_for_nlq(value))
    elif isinstance(value, dict):
        for child in value.values():
            _collect_expr_refs_and_literals(child, refs, literals)
    elif isinstance(value, list):
        for item in value:
            _collect_expr_refs_and_literals(item, refs, literals)


def _trim_mql_var_prefix(value: str) -> str:
    text = str(value)
    while text.startswith("$"):
        text = text[1:]
    return text


def _literal_for_nlq(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    return str(value)


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _literal_constants_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    constants: set[str] = set()
    _collect_literal_constants(pipeline, constants)
    return sorted(constants)


def _collect_literal_constants(value: Any, constants: set[str]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key) in {"$gte", "$gt", "$lte", "$lt"}:
                continue
            _collect_literal_constants(child, constants)
    elif isinstance(value, list):
        for item in value:
            _collect_literal_constants(item, constants)
    elif isinstance(value, str):
        if not value.startswith("$") and not value.startswith("$$") and value != "missing":
            constants.add(value)


def _threshold_numbers_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    numbers: set[float] = set()
    _collect_threshold_numbers_for_nlq(pipeline, numbers, parent_key=None)
    return [_format_nlq_number(value) for value in sorted(numbers)]


def _collect_threshold_numbers_for_nlq(
    value: Any,
    numbers: set[float],
    *,
    parent_key: str | None,
) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _collect_threshold_numbers_for_nlq(child, numbers, parent_key=str(key))
    elif isinstance(value, list):
        for item in value:
            _collect_threshold_numbers_for_nlq(item, numbers, parent_key=parent_key)
    elif parent_key in {"$gte", "$gt", "$lte", "$lt"} and isinstance(value, (int, float)):
        numbers.add(float(value))


def _sort_parts_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    parts: list[str] = []
    for stage in pipeline:
        sort = stage.get("$sort") if isinstance(stage, dict) else None
        if not isinstance(sort, dict):
            continue
        for field, direction in sort.items():
            try:
                direction_number = int(direction)
            except (TypeError, ValueError):
                continue
            direction_text = "descending" if direction_number < 0 else "ascending"
            field_words = str(field).replace(".", " ")
            if field_words != str(field):
                parts.append(f"{field} {field_words} {direction_text}")
            else:
                parts.append(f"{field} {direction_text}")
    return parts


def _output_fields_for_nlq(pipeline: list[dict[str, Any]]) -> list[str]:
    for stage in reversed(pipeline):
        project = stage.get("$project") if isinstance(stage, dict) else None
        if isinstance(project, dict):
            fields = []
            if project.get("_id", 1) not in (0, False):
                fields.append("_id")
            fields.extend(
                str(key)
                for key, value in project.items()
                if key != "_id" and value not in (0, False)
            )
            return fields
        group = stage.get("$group") if isinstance(stage, dict) else None
        if isinstance(group, dict):
            return [str(key) for key in group]
    return []


def _format_nlq_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return str(value)


def _stabilize_sort_limit(pipeline: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    changed = False
    for index, stage in enumerate(pipeline):
        sort = stage.get("$sort") if isinstance(stage, dict) else None
        if not isinstance(sort, dict):
            continue
        if not any("$limit" in later for later in pipeline[index + 1:] if isinstance(later, dict)):
            continue
        candidates = _tie_breaker_candidates(pipeline, index)
        changed = _ensure_sort_keys(sort, candidates) or changed
    return pipeline, changed


def _remove_sort_keys_absent_after_projection(
    pipeline: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    changed = False
    for index, stage in enumerate(pipeline):
        sort = stage.get("$sort") if isinstance(stage, dict) else None
        if not isinstance(sort, dict):
            continue
        projected = _nearest_projected_fields(pipeline, index)
        if projected is None:
            continue
        for field in list(sort):
            root = str(field).split(".", 1)[0]
            if field not in projected and root not in projected:
                sort.pop(field, None)
                changed = True
    return pipeline, changed


def _nearest_projected_fields(
    pipeline: list[dict[str, Any]],
    before_index: int,
) -> set[str] | None:
    for prior in reversed(pipeline[:before_index]):
        group = prior.get("$group") if isinstance(prior, dict) else None
        if isinstance(group, dict):
            return {str(key) for key in group}
        project = prior.get("$project") if isinstance(prior, dict) else None
        if not isinstance(project, dict):
            continue
        fields = {
            str(key)
            for key, value in project.items()
            if key != "_id" and value not in (0, False)
        }
        if "_id" in project and project.get("_id") not in (0, False):
            fields.add("_id")
        return fields
    return None


def _tie_breaker_candidates(pipeline: list[dict[str, Any]], sort_index: int) -> list[str]:
    for prior in reversed(pipeline[:sort_index]):
        group = prior.get("$group") if isinstance(prior, dict) else None
        if isinstance(group, dict):
            return ["_id"]
        project = prior.get("$project") if isinstance(prior, dict) else None
        if isinstance(project, dict):
            if "_id" in project and project.get("_id") not in (0, False):
                return ["_id"]
            fields = [
                str(key)
                for key, value in project.items()
                if key != "_id" and value not in (0, False)
                and _is_safe_sort_tie_breaker(str(key), value)
            ]
            if fields:
                return fields[:8]
    if any("$group" in stage for stage in pipeline[:sort_index] if isinstance(stage, dict)):
        return ["_id"]
    return ["_id"]


def _is_safe_sort_tie_breaker(field: str, expression: Any) -> bool:
    lowered = field.lower()
    unsafe_fragments = (
        "entries",
        "events",
        "players",
        "feature_path",
        "filtered",
        "dynamic_entries",
        "category_buckets",
        "finance_entries",
        "budget_entries",
        "lineups.",
        "participation.",
    )
    if any(fragment in lowered for fragment in unsafe_fragments):
        return False
    if isinstance(expression, dict):
        return False
    return True


def _ensure_sort_keys(sort: dict[str, Any], fields: list[str]) -> bool:
    changed = False
    for field in fields:
        if field and field not in sort:
            sort[field] = 1
            changed = True
    return changed


def _first_limit(pipeline: list[dict[str, Any]]) -> int | None:
    for stage in pipeline:
        limit = stage.get("$limit") if isinstance(stage, dict) else None
        if isinstance(limit, int):
            return limit
    return None


def _refresh_mql_metadata(record: dict[str, Any]) -> None:
    mql = str(record.get("MQL") or "")
    record["mql_signature"] = mql_signature(mql)
    record["mql_skeleton_signature"] = mql_skeleton_signature(mql)
    record["mql_skeleton_summary"] = mql_skeleton_summary(mql)


def _refresh_native_metadata_after_mql_change(record: dict[str, Any]) -> None:
    ops: list[str] | None = None
    metadata = record.get("native_metadata")
    try:
        _, pipeline = parse_pipeline(str(record.get("MQL") or ""))
        ops = sorted(all_ops(pipeline))
    except Exception:
        ops = None
    if ops is not None:
        record["mongo_native_constructs"] = ops
        if isinstance(metadata, dict):
            metadata["mongo_native_constructs"] = ops
    verification = record.get("native_verification")
    if isinstance(verification, dict):
        evidence = verification.setdefault("evidence", [])
        if isinstance(evidence, list) and "mql_signature_recomputed" not in evidence:
            evidence.append("mql_signature_recomputed")


def _format_mql(collection: str, pipeline: list[dict[str, Any]]) -> str:
    return f"db.{collection}.aggregate({json.dumps(pipeline, ensure_ascii=False, separators=(',', ':'))})"


def _write_release_files(root: Path, records: list[dict[str, Any]], pairs_by_record: dict[Any, dict[str, Any]]) -> list[str]:
    data_dir = root / "data"
    tend_path = data_dir / "TEND.json"
    test_path = data_dir / "test.json"
    lean_path = data_dir / "TEND_lean.json"
    test_lean_path = data_dir / "test_lean.json"
    lean_jsonl_path = data_dir / "TEND_lean.jsonl"
    pairs_jsonl_path = root / "audits" / "nl_mql" / "post_surgery_nl_mql_pairs.jsonl"
    pairs_csv_path = root / "audits" / "nl_mql" / "post_surgery_nl_mql_pairs.csv"

    lean_records = [_lean_record(record) for record in records]
    tend_text = json.dumps(records, ensure_ascii=False, indent=2) + "\n"
    lean_text = json.dumps(lean_records, ensure_ascii=False, indent=2) + "\n"
    tend_path.write_text(tend_text, encoding="utf-8")
    test_path.write_text(tend_text, encoding="utf-8")
    lean_path.write_text(lean_text, encoding="utf-8")
    test_lean_path.write_text(lean_text, encoding="utf-8")
    lean_jsonl_path.write_text(
        "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n" for record in lean_records),
        encoding="utf-8",
    )

    pair_rows = [_pair_record(record, pairs_by_record.get(record.get("record_id"), {})) for record in records]
    pairs_jsonl_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in pair_rows),
        encoding="utf-8",
    )
    with pairs_csv_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "record_id",
                "db_id",
                "NLQ",
                "NLQ_colloquial",
                "MQL",
                "mql_signature",
                "mql_skeleton_signature",
                "mql_skeleton_summary",
                "stage_count",
                "operator_count",
                "complexity_score",
            ],
        )
        writer.writeheader()
        writer.writerows(pair_rows)

    return [
        str(tend_path),
        str(test_path),
        str(lean_path),
        str(test_lean_path),
        str(lean_jsonl_path),
        str(pairs_jsonl_path),
        str(pairs_csv_path),
    ]


def _lean_record(record: dict[str, Any]) -> dict[str, Any]:
    nlq = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
    return {
        "record_id": record.get("record_id"),
        "db_id": record.get("db_id"),
        "NLQ": nlq.get("canonical", ""),
        "NLQ_colloquial": nlq.get("colloquial", ""),
        "MQL": record.get("MQL", ""),
    }


def _pair_record(record: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    nlq = record.get("nl_queries") if isinstance(record.get("nl_queries"), dict) else {}
    mql = str(record.get("MQL") or "")
    try:
        _, pipeline = parse_pipeline(mql)
        stage_count = len(pipeline)
        operator_count = len(all_ops(pipeline))
    except Exception:
        stage_count = previous.get("stage_count", 0)
        operator_count = previous.get("operator_count", 0)
    return {
        "record_id": record.get("record_id"),
        "db_id": record.get("db_id"),
        "NLQ": nlq.get("canonical", ""),
        "NLQ_colloquial": nlq.get("colloquial", ""),
        "MQL": mql,
        "mql_signature": record.get("mql_signature", ""),
        "mql_skeleton_signature": record.get("mql_skeleton_signature", ""),
        "mql_skeleton_summary": record.get("mql_skeleton_summary", ""),
        "stage_count": stage_count,
        "operator_count": operator_count,
        "complexity_score": previous.get("complexity_score", 0),
    }


__all__ = ["RepairSummary", "apply_builtin_quality_repairs"]
