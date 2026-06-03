from __future__ import annotations

from collections import defaultdict
from typing import Any

from tend.execution import world_signature as compute_world_signature

from ..native_audit import audit_database_structure
from ..native_executor import NativeExecutionResult
from ..native_recipe import NativeFeature, NativeFeatureManifest
from ..native_recipe import NativeMigrationRecipe
from .common import collection, expr, join, recipe, source as field_source, transform

DESIGN_VERSION = 1
MODULE_REF = __name__


def build_native_recipe(source: Any, db_id: str) -> NativeMigrationRecipe:
    source.schema(db_id)
    return recipe(
        db_id,
        version=DESIGN_VERSION,
        design_goal=(
            "Represent student-club events and members with budget categories, "
            "attendance participation, and finance event streams."
        ),
        collections=[
            collection(
                "club_event_plans",
                purpose="Event documents with dynamic budget categories.",
                source_tables=["event", "budget", "attendance"],
                transforms=[
                    transform(
                        "budget_by_category",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="event",
                        child_table="budget",
                        join=join("event.event_id", "budget.link_to_event"),
                        target_field="budget_by_category",
                        key=expr("budget.category", "budget.category"),
                        values={
                            "allocated": expr("sum(budget.amount)", "budget.amount"),
                            "spent": expr("sum(budget.spent)", "budget.spent"),
                            "remaining": expr(
                                "sum(budget.remaining)",
                                "budget.remaining",
                            ),
                            "event_status": expr(
                                "last(budget.event_status)",
                                "budget.event_status",
                            ),
                        },
                    ),
                    transform(
                        "event_status_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="event_tags",
                        tags={
                            "meeting": {
                                "condition": "event.type == 'Meeting'",
                                "provenance": ["event.type"],
                            },
                            "completed": {
                                "condition": "event.status == 'Completed'",
                                "provenance": ["event.status"],
                            },
                            "has_location": {
                                "condition": "event.location is not null",
                                "provenance": ["event.location"],
                            },
                        },
                    ),
                ],
            ),
            collection(
                "club_member_ledgers",
                purpose="Member documents with expense and income timelines.",
                source_tables=["member", "expense", "income", "major"],
                transforms=[
                    transform(
                        "member_expense_events",
                        "nested_event_stream",
                        module_ref=MODULE_REF,
                        parent_table="member",
                        event_source_table="expense",
                        join=join("member.member_id", "expense.link_to_member"),
                        target_field="expense_events",
                        event_type_field="expense.approved",
                        event_time_field="expense.expense_date",
                        event_payload={
                            "description": "expense.expense_description",
                            "cost": "expense.cost",
                            "budget_id": "expense.link_to_budget",
                        },
                    ),
                    transform(
                        "member_profile_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="member_tags",
                        tags={
                            "medium_shirt": {
                                "condition": "member.t_shirt_size == 'Medium'",
                                "provenance": ["member.t_shirt_size"],
                            },
                            "club_officer": {
                                "condition": "member.position is not null",
                                "provenance": ["member.position"],
                            },
                            "major_known": {
                                "condition": "major.major_name is not null",
                                "provenance": ["major.major_name"],
                            },
                        },
                    ),
                ],
            ),
        ],
    )


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    event_hook: Any = None,
) -> NativeExecutionResult:
    """Build student-club event and member documents from the live SQLite schema."""
    if db_id != "student_club":
        raise ValueError(f"student_club materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    events = _read_table(conn, "event", order_by=["event_date", "event_id"])
    budgets = _read_table(conn, "budget", order_by=["link_to_event", "category", "budget_id"])
    attendance = _read_table(conn, "attendance", order_by=["link_to_event", "link_to_member"])
    expenses = _read_table(conn, "expense", order_by=["expense_date", "expense_id"])
    income = _read_table(conn, "income", order_by=["date_received", "income_id"])
    members = _read_table(conn, "member", order_by=["last_name", "first_name", "member_id"])
    majors = _by_id(_read_table(conn, "major", order_by=["major_name", "major_id"]), "major_id")
    zip_codes = _by_id(_read_table(conn, "zip_code", order_by=["zip_code"]), "zip_code")

    members_by_id = _by_id(members, "member_id")
    budgets_by_id = _by_id(budgets, "budget_id")
    budgets_by_event = _group_by(budgets, "link_to_event")
    attendance_by_event = _group_by(attendance, "link_to_event")
    attendance_by_member = _group_by(attendance, "link_to_member")
    expenses_by_budget = _group_by(expenses, "link_to_budget")
    expenses_by_member = _group_by(expenses, "link_to_member")
    income_by_member = _group_by(income, "link_to_member")
    events_by_id = _by_id(events, "event_id")

    event_docs = [
        _event_plan_doc(
            event,
            budgets=budgets_by_event.get(event["event_id"], []),
            attendance=attendance_by_event.get(event["event_id"], []),
            members_by_id=members_by_id,
            majors=majors,
            zip_codes=zip_codes,
            expenses_by_budget=expenses_by_budget,
            expenses_by_member=expenses_by_member,
            income_by_member=income_by_member,
        )
        for event in events
    ]
    member_docs = [
        _member_account_doc(
            member,
            major=majors.get(member.get("link_to_major")),
            zip_code=zip_codes.get(member.get("zip")),
            attendance=attendance_by_member.get(member["member_id"], []),
            events_by_id=events_by_id,
            budgets_by_event=budgets_by_event,
            budgets_by_id=budgets_by_id,
            expenses=expenses_by_member.get(member["member_id"], []),
            income=income_by_member.get(member["member_id"], []),
        )
        for member in members
    ]

    data = {
        "club_event_plans_v2": event_docs,
        "club_member_accounts_v2": member_docs,
    }
    audit = audit_database_structure(db_id, data)
    manifest = _manifest()
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "club_event_plans_v2": {
                "document_count": len(event_docs),
                "root_entity": "student club event",
                "source_tables": ["event", "budget", "attendance", "expense", "member"],
            },
            "club_member_accounts_v2": {
                "document_count": len(member_docs),
                "root_entity": "student club member account",
                "source_tables": ["member", "major", "zip_code", "attendance", "income", "expense"],
            },
        },
        "structure_audit": audit.to_dict(),
    }
    provenance = _provenance(db_id)
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "student_club_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            event_count=len(event_docs),
            member_count=len(member_docs),
            world_signature=signature,
        )
    return NativeExecutionResult(
        data=data,
        schema=native_schema,
        manifest=manifest,
        provenance=provenance,
        world_signature=signature,
        validation=None,
    )


def _read_table(conn: Any, table: str, *, order_by: list[str]) -> list[dict[str, Any]]:
    order_sql = ", ".join(f'"{name}"' for name in order_by)
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_sql}')
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _by_id(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    return {row[key]: row for row in rows if row.get(key) is not None}


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return dict(grouped)


def _event_plan_doc(
    event: dict[str, Any],
    *,
    budgets: list[dict[str, Any]],
    attendance: list[dict[str, Any]],
    members_by_id: dict[Any, dict[str, Any]],
    majors: dict[Any, dict[str, Any]],
    zip_codes: dict[Any, dict[str, Any]],
    expenses_by_budget: dict[Any, list[dict[str, Any]]],
    expenses_by_member: dict[Any, list[dict[str, Any]]],
    income_by_member: dict[Any, list[dict[str, Any]]],
) -> dict[str, Any]:
    member_links = [
        (row.get("link_to_member"), members_by_id.get(row.get("link_to_member")))
        for row in attendance
    ]
    attendees = [
        _event_attendee_doc(
            member,
            major=majors.get(member.get("link_to_major")) if member else None,
            zip_code=zip_codes.get(member.get("zip")) if member else None,
            event_budgets=budgets,
            member_expenses=expenses_by_member.get(member_id, []),
            member_income=income_by_member.get(member_id, []),
        )
        for member_id, member in member_links
    ]
    budget_by_category = _budget_by_category(
        budgets,
        expenses_by_budget=expenses_by_budget,
        members_by_id=members_by_id,
    )
    return {
        "_id": f"event:{event['event_id']}",
        "event": {
            "event_id": event["event_id"],
            "name": event.get("event_name"),
            "type": event.get("type"),
            "status": event.get("status"),
            "schedule": {
                "starts_at": event.get("event_date"),
                "date_presence_state": _presence_state(event.get("event_date")),
            },
            "notes": {
                "value": event.get("notes"),
                "presence_state": _presence_state(event.get("notes")),
            },
        },
        "venue": {
            "location": event.get("location"),
            "presence_state": _presence_state(event.get("location")),
        },
        "budget_by_category": budget_by_category,
        "attendance": {
            "presence_state": _presence_state(attendees),
            "attendee_count": len(attendees),
            "attendees": attendees,
            "participation_by_role": _participation_by_role(attendees),
        },
        "finance_rollup": _event_finance_rollup(budget_by_category),
        "schema_state": {
            "notes": _presence_state(event.get("notes")),
            "location": _presence_state(event.get("location")),
            "budgets": _presence_state(budgets),
            "attendance": _presence_state(attendees),
            "external_rsvp_feed": "missing",
        },
        "_provenance": {
            "source_tables": ["event", "budget", "attendance", "expense", "member", "major", "zip_code"],
            "source_keys": {"event_id": event["event_id"]},
        },
    }


def _event_attendee_doc(
    member: dict[str, Any] | None,
    *,
    major: dict[str, Any] | None,
    zip_code: dict[str, Any] | None,
    event_budgets: list[dict[str, Any]],
    member_expenses: list[dict[str, Any]],
    member_income: list[dict[str, Any]],
) -> dict[str, Any]:
    if member is None:
        return {
            "member_presence_state": "missing",
            "account": {"member_id": None},
            "finance_by_category": {},
        }
    return {
        "member_presence_state": "present",
        "account": _member_identity(member, major=major, zip_code=zip_code),
        "attendance_context": {
            "role": member.get("position"),
            "role_presence_state": _presence_state(member.get("position")),
            "shirt_size": member.get("t_shirt_size"),
        },
        "finance_by_category": _member_finance_for_event(
            event_budgets,
            member_expenses=member_expenses,
            member_income=member_income,
        ),
    }


def _budget_by_category(
    budgets: list[dict[str, Any]],
    *,
    expenses_by_budget: dict[Any, list[dict[str, Any]]],
    members_by_id: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for budget in budgets:
        grouped[_dynamic_key(budget.get("category"), "uncategorized")].append(budget)
    return {
        category: _budget_category_doc(
            category_budgets,
            expenses_by_budget=expenses_by_budget,
            members_by_id=members_by_id,
        )
        for category, category_budgets in sorted(grouped.items())
    }


def _budget_category_doc(
    budgets: list[dict[str, Any]],
    *,
    expenses_by_budget: dict[Any, list[dict[str, Any]]],
    members_by_id: dict[Any, dict[str, Any]],
) -> dict[str, Any]:
    expense_rows = [
        expense
        for budget in budgets
        for expense in expenses_by_budget.get(budget.get("budget_id"), [])
    ]
    return {
        "presence_state": _presence_state(budgets),
        "budget_count": len(budgets),
        "amount_total": _money_sum(budget.get("amount") for budget in budgets),
        "spent_total": _money_sum(budget.get("spent") for budget in budgets),
        "remaining_total": _money_sum(budget.get("remaining") for budget in budgets),
        "status_by_state": _budget_status_by_state(budgets),
        "budgets": [
            _budget_doc(
                budget,
                expenses=expenses_by_budget.get(budget.get("budget_id"), []),
                members_by_id=members_by_id,
            )
            for budget in budgets
        ],
        "expense_chain": {
            "presence_state": _presence_state(expense_rows),
            "expense_count": len(expense_rows),
            "approved_cost_total": _money_sum(
                expense.get("cost") for expense in expense_rows if expense.get("approved") == "true"
            ),
        },
    }


def _budget_doc(
    budget: dict[str, Any],
    *,
    expenses: list[dict[str, Any]],
    members_by_id: dict[Any, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "budget_id": budget.get("budget_id"),
        "category": budget.get("category"),
        "status": budget.get("event_status"),
        "amounts": {
            "allocated": budget.get("amount"),
            "spent": budget.get("spent"),
            "remaining": budget.get("remaining"),
            "overspent": bool((budget.get("remaining") or 0) < 0),
        },
        "expense_events": [
            _expense_doc(expense, member=members_by_id.get(expense.get("link_to_member")))
            for expense in expenses
        ],
        "schema_state": {
            "expenses": _presence_state(expenses),
            "category": _presence_state(budget.get("category")),
        },
    }


def _expense_doc(
    expense: dict[str, Any],
    *,
    member: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "expense_id": expense.get("expense_id"),
        "description": expense.get("expense_description"),
        "expense_date": expense.get("expense_date"),
        "cost": expense.get("cost"),
        "approval": {
            "value": expense.get("approved"),
            "presence_state": _presence_state(expense.get("approved")),
            "is_approved": expense.get("approved") == "true",
        },
        "paid_by": {
            "member_id": expense.get("link_to_member"),
            "member_presence_state": _presence_state(member),
            "name": _member_name(member) if member else None,
        },
    }


def _participation_by_role(attendees: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for attendee in attendees:
        role = attendee.get("attendance_context", {}).get("role")
        grouped[_dynamic_key(role, "unknown_role")].append(attendee)
    return {
        role: {
            "presence_state": _presence_state(group),
            "attendee_count": len(group),
            "members": [
                {
                    "member_id": item["account"].get("member_id"),
                    "name": item["account"].get("profile", {}).get("display_name"),
                }
                for item in group
            ],
        }
        for role, group in sorted(grouped.items())
    }


def _member_finance_for_event(
    event_budgets: list[dict[str, Any]],
    *,
    member_expenses: list[dict[str, Any]],
    member_income: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    category_by_budget = {
        budget.get("budget_id"): _dynamic_key(budget.get("category"), "uncategorized")
        for budget in event_budgets
    }
    grouped_expenses: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for expense in member_expenses:
        category = category_by_budget.get(expense.get("link_to_budget"))
        if category is not None:
            grouped_expenses[category].append(expense)
    income_total = _money_sum(item.get("amount") for item in member_income)
    return {
        category: {
            "presence_state": _presence_state(expenses),
            "expense_count": len(expenses),
            "expense_total": _money_sum(expense.get("cost") for expense in expenses),
            "income_context": {
                "member_income_total": income_total,
                "income_presence_state": _presence_state(member_income),
            },
            "expenses": [
                {
                    "expense_id": expense.get("expense_id"),
                    "description": expense.get("expense_description"),
                    "cost": expense.get("cost"),
                    "approved_presence_state": _presence_state(expense.get("approved")),
                }
                for expense in expenses
            ],
        }
        for category, expenses in sorted(grouped_expenses.items())
    }


def _event_finance_rollup(budget_by_category: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "category_count": len(budget_by_category),
        "allocated_total": _money_sum(item.get("amount_total") for item in budget_by_category.values()),
        "spent_total": _money_sum(item.get("spent_total") for item in budget_by_category.values()),
        "remaining_total": _money_sum(item.get("remaining_total") for item in budget_by_category.values()),
    }


def _member_account_doc(
    member: dict[str, Any],
    *,
    major: dict[str, Any] | None,
    zip_code: dict[str, Any] | None,
    attendance: list[dict[str, Any]],
    events_by_id: dict[Any, dict[str, Any]],
    budgets_by_event: dict[Any, list[dict[str, Any]]],
    budgets_by_id: dict[Any, dict[str, Any]],
    expenses: list[dict[str, Any]],
    income: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "_id": f"member:{member['member_id']}",
        "account": _member_identity(member, major=major, zip_code=zip_code),
        "participation": {
            "attendance_presence_state": _presence_state(attendance),
            "event_count": len(attendance),
            "events": [
                _member_event_doc(
                    row,
                    event=events_by_id.get(row.get("link_to_event")),
                    budgets=budgets_by_event.get(row.get("link_to_event"), []),
                )
                for row in attendance
            ],
        },
        "finance": {
            "income_by_source": _income_by_source(income),
            "expense_by_category": _expense_by_category(expenses, budgets_by_id=budgets_by_id),
            "summary": {
                "income_total": _money_sum(item.get("amount") for item in income),
                "expense_total": _money_sum(item.get("cost") for item in expenses),
            },
        },
        "schema_state": {
            "major": _presence_state(major),
            "zip_code": _presence_state(zip_code),
            "income": _presence_state(income),
            "expenses": _presence_state(expenses),
            "advisor_account": "missing",
        },
        "_provenance": {
            "source_tables": ["member", "major", "zip_code", "attendance", "event", "income", "expense", "budget"],
            "source_keys": {"member_id": member["member_id"]},
        },
    }


def _member_identity(
    member: dict[str, Any],
    *,
    major: dict[str, Any] | None,
    zip_code: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "member_id": member.get("member_id"),
        "profile": {
            "first_name": member.get("first_name"),
            "last_name": member.get("last_name"),
            "display_name": _member_name(member),
            "email": member.get("email"),
            "phone": {
                "value": member.get("phone"),
                "presence_state": _presence_state(member.get("phone")),
            },
            "shirt_size": member.get("t_shirt_size"),
        },
        "club_role": {
            "position": member.get("position"),
            "presence_state": _presence_state(member.get("position")),
            "is_officer": member.get("position") not in {None, "", "Member", "Inactive"},
        },
        "academic_profile": {
            "major_id": member.get("link_to_major"),
            "presence_state": _presence_state(major),
            "major": None if major is None else {
                "name": major.get("major_name"),
                "department": major.get("department"),
                "college": major.get("college"),
            },
        },
        "address_profile": {
            "zip": member.get("zip"),
            "presence_state": _presence_state(zip_code),
            "zip_code": None if zip_code is None else {
                "city": zip_code.get("city"),
                "county": zip_code.get("county"),
                "state": zip_code.get("state"),
                "short_state": zip_code.get("short_state"),
                "zip_type": zip_code.get("type"),
            },
        },
    }


def _member_event_doc(
    attendance: dict[str, Any],
    *,
    event: dict[str, Any] | None,
    budgets: list[dict[str, Any]],
) -> dict[str, Any]:
    if event is None:
        return {
            "event_presence_state": "missing",
            "event_id": attendance.get("link_to_event"),
            "budget_by_category": {},
        }
    return {
        "event_presence_state": "present",
        "event_id": event.get("event_id"),
        "event": {
            "name": event.get("event_name"),
            "type": event.get("type"),
            "status": event.get("status"),
            "date": event.get("event_date"),
            "location": {
                "value": event.get("location"),
                "presence_state": _presence_state(event.get("location")),
            },
        },
        "budget_by_category": {
            category: {
                "budget_count": len(rows),
                "allocated_total": _money_sum(row.get("amount") for row in rows),
                "status_by_state": _budget_status_by_state(rows),
            }
            for category, rows in _group_budgets_by_category(budgets).items()
        },
    }


def _income_by_source(income: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in income:
        grouped[_dynamic_key(row.get("source"), "unknown_source")].append(row)
    return {
        source: {
            "presence_state": _presence_state(rows),
            "income_count": len(rows),
            "amount_total": _money_sum(row.get("amount") for row in rows),
            "payments": [
                {
                    "income_id": row.get("income_id"),
                    "date_received": row.get("date_received"),
                    "amount": row.get("amount"),
                    "notes": {
                        "value": row.get("notes"),
                        "presence_state": _presence_state(row.get("notes")),
                    },
                }
                for row in rows
            ],
        }
        for source, rows in sorted(grouped.items())
    }


def _expense_by_category(
    expenses: list[dict[str, Any]],
    *,
    budgets_by_id: dict[Any, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for expense in expenses:
        budget = budgets_by_id.get(expense.get("link_to_budget"))
        category = budget.get("category") if budget else None
        grouped[_dynamic_key(category, "uncategorized")].append(expense)
    return {
        category: {
            "presence_state": _presence_state(rows),
            "expense_count": len(rows),
            "cost_total": _money_sum(row.get("cost") for row in rows),
            "expenses": [
                {
                    "expense_id": row.get("expense_id"),
                    "description": row.get("expense_description"),
                    "cost": row.get("cost"),
                    "approval_state": _presence_state(row.get("approved")),
                }
                for row in rows
            ],
        }
        for category, rows in sorted(grouped.items())
    }


def _group_budgets_by_category(
    budgets: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for budget in budgets:
        grouped[_dynamic_key(budget.get("category"), "uncategorized")].append(budget)
    return dict(sorted(grouped.items()))


def _budget_status_by_state(budgets: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for budget in budgets:
        grouped[_dynamic_key(budget.get("event_status"), "unknown_status")].append(budget)
    return {
        status: {
            "presence_state": _presence_state(rows),
            "budget_count": len(rows),
            "remaining_total": _money_sum(row.get("remaining") for row in rows),
        }
        for status, rows in sorted(grouped.items())
    }


def _presence_state(value: Any) -> str:
    if value is None:
        return "null"
    if value == "" or value == [] or value == {}:
        return "empty"
    return "present"


def _dynamic_key(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def _money_sum(values: Any) -> float:
    return round(sum(float(value or 0) for value in values), 2)


def _member_name(member: dict[str, Any] | None) -> str:
    if not member:
        return ""
    return " ".join(
        part for part in [member.get("first_name"), member.get("last_name")] if part
    )


def _manifest() -> NativeFeatureManifest:
    return NativeFeatureManifest(
        db_id="student_club",
        features=[
            NativeFeature(
                id="club_event_plans_v2.budget_by_category",
                type="dynamic_key_object",
                collection="club_event_plans_v2",
                field="budget_by_category",
                query_patterns=[
                    "student_club_budget_category_rollup",
                    "dynamic_key_comparison",
                ],
                required_constructs=["$objectToArray", "$unwind", "$group"],
                provenance_refs=["budget.category", "budget.amount", "budget.spent", "expense.cost"],
                coverage={"source_tables": ["event", "budget", "expense"]},
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "student_club_budget_category_rollup",
                            "intent": "compare allocated and spent totals across event budget categories",
                            "pipeline": [
                                {"$project": {"event": "$event.name", "categories": {"$objectToArray": "$budget_by_category"}}},
                                {"$unwind": "$categories"},
                                {
                                    "$group": {
                                        "_id": "$categories.k",
                                        "events": {"$addToSet": "$event"},
                                        "allocated_total": {"$sum": "$categories.v.amount_total"},
                                        "spent_total": {"$sum": "$categories.v.spent_total"},
                                    }
                                },
                                {"$project": {"event_count": {"$size": "$events"}, "allocated_total": 1, "spent_total": 1}},
                                {"$sort": {"spent_total": -1, "_id": 1}},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$size"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="club_event_plans_v2.attendee_finance_by_category",
                type="array_object_dynamic_key",
                collection="club_event_plans_v2",
                field="attendance.attendees.finance_by_category",
                query_patterns=[
                    "student_club_attendee_reimbursement_chain",
                    "array_object_dynamic_key_comparison",
                ],
                required_constructs=["$unwind", "$objectToArray", "$filter"],
                provenance_refs=["attendance.link_to_member", "expense.link_to_member", "budget.category"],
                coverage={"source_tables": ["attendance", "member", "expense", "budget"]},
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "student_club_attendee_reimbursement_chain",
                            "intent": "find attendees whose event participation links to category-specific expenses",
                            "pipeline": [
                                {"$unwind": "$attendance.attendees"},
                                {
                                    "$project": {
                                        "event": "$event.name",
                                        "member": "$attendance.attendees.account.profile.display_name",
                                        "categories": {"$objectToArray": "$attendance.attendees.finance_by_category"},
                                    }
                                },
                                {"$unwind": "$categories"},
                                {
                                    "$match": {
                                        "categories.v.expense_count": {"$gt": 0}
                                    }
                                },
                                {
                                    "$group": {
                                        "_id": {"event": "$event", "category": "$categories.k"},
                                        "members": {"$addToSet": "$member"},
                                        "expense_total": {"$sum": "$categories.v.expense_total"},
                                    }
                                },
                                {"$project": {"member_count": {"$size": "$members"}, "expense_total": 1}},
                            ],
                            "mongo_native_constructs": ["$unwind", "$objectToArray", "$match", "$group", "$size"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="club_member_accounts_v2.member_event_timeline",
                type="nested_event_stream",
                collection="club_member_accounts_v2",
                field="participation.events",
                query_patterns=[
                    "student_club_member_participation_timeline",
                    "nested_event_filter",
                ],
                required_constructs=["$unwind", "$match", "$sort"],
                provenance_refs=["member.member_id", "attendance.link_to_event", "event.event_date"],
                coverage={"source_tables": ["member", "attendance", "event"]},
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "student_club_member_participation_timeline",
                            "intent": "order each member's attended club events with event budget context",
                            "pipeline": [
                                {"$unwind": "$participation.events"},
                                {
                                    "$project": {
                                        "member": "$account.profile.display_name",
                                        "role": "$account.club_role.position",
                                        "event": "$participation.events.event.name",
                                        "event_date": "$participation.events.event.date",
                                        "event_type": "$participation.events.event.type",
                                    }
                                },
                                {"$match": {"event_date": {"$ne": None}}},
                                {"$sort": {"member": 1, "event_date": 1}},
                            ],
                            "mongo_native_constructs": ["$unwind", "$project", "$match", "$sort"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="club_member_accounts_v2.income_by_source",
                type="dynamic_key_object",
                collection="club_member_accounts_v2",
                field="finance.income_by_source",
                query_patterns=[
                    "student_club_income_source_mix",
                    "dynamic_key_comparison",
                ],
                required_constructs=["$objectToArray", "$group"],
                provenance_refs=["income.source", "income.amount", "income.link_to_member"],
                coverage={"source_tables": ["member", "income"]},
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "student_club_income_source_mix",
                            "intent": "compare dues, fundraising, sponsorship, and appropriation income by member",
                            "pipeline": [
                                {"$project": {"member": "$account.profile.display_name", "sources": {"$objectToArray": "$finance.income_by_source"}}},
                                {"$unwind": "$sources"},
                                {
                                    "$group": {
                                        "_id": "$sources.k",
                                        "amount_total": {"$sum": "$sources.v.amount_total"},
                                        "members": {"$addToSet": "$member"},
                                    }
                                },
                                {"$project": {"amount_total": 1, "member_count": {"$size": "$members"}}},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$size"],
                        }
                    ]
                },
            ),
        ],
    )


def _provenance(db_id: str) -> dict[str, Any]:
    return {
        "db_id": db_id,
        "conversion_code_ref": "tend.construct.native_designs.student_club.materialize_native_dataworld",
        "entries": {
            "club_event_plans_v2.budget_by_category": {
                "source_tables": ["event", "budget", "expense"],
                "provenance_refs": ["event.event_id", "budget.category", "budget.amount", "expense.cost"],
            },
            "club_event_plans_v2.attendee_finance_by_category": {
                "source_tables": ["attendance", "member", "expense", "budget", "income"],
                "provenance_refs": [
                    "attendance.link_to_member",
                    "expense.link_to_member",
                    "expense.link_to_budget",
                    "budget.category",
                    "income.link_to_member",
                ],
            },
            "club_member_accounts_v2.member_event_timeline": {
                "source_tables": ["member", "attendance", "event", "budget"],
                "provenance_refs": ["member.member_id", "attendance.link_to_event", "event.event_date"],
            },
            "club_member_accounts_v2.income_by_source": {
                "source_tables": ["member", "income"],
                "provenance_refs": ["income.source", "income.amount"],
            },
        },
    }
