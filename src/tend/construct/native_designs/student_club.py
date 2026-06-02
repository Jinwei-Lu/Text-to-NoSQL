from __future__ import annotations

from typing import Any

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
