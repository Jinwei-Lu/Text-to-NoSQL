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
            "Represent California schools with academic-year FRPM buckets, SAT "
            "performance facts, and operational school tags."
        ),
        collections=[
            collection(
                "school_profiles",
                purpose="School documents with per-year meal-program metrics.",
                source_tables=["schools", "frpm", "satscores"],
                transforms=[
                    transform(
                        "frpm_by_academic_year",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="schools",
                        child_table="frpm",
                        join=join("schools.CDSCode", "frpm.CDSCode"),
                        target_field="frpm_by_academic_year",
                        key=expr("frpm.Academic Year", "frpm.Academic Year"),
                        values={
                            "enrollment_k12": expr(
                                "sum(frpm.Enrollment (K-12))",
                                "frpm.Enrollment (K-12)",
                            ),
                            "free_meal_count_k12": expr(
                                "sum(frpm.Free Meal Count (K-12))",
                                "frpm.Free Meal Count (K-12)",
                            ),
                            "frpm_pct_k12": expr(
                                "avg(frpm.Percent (%) Eligible FRPM (K-12))",
                                "frpm.Percent (%) Eligible FRPM (K-12)",
                            ),
                        },
                    ),
                    transform(
                        "school_operational_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="school_tags",
                        tags={
                            "charter": {
                                "condition": "schools.Charter == 1",
                                "provenance": ["schools.Charter"],
                            },
                            "magnet": {
                                "condition": "schools.Magnet == 1",
                                "provenance": ["schools.Magnet"],
                            },
                            "virtual_delivery": {
                                "condition": "schools.Virtual is not null",
                                "provenance": ["schools.Virtual"],
                            },
                            "closed_school": {
                                "condition": "schools.ClosedDate is not null",
                                "provenance": ["schools.ClosedDate"],
                            },
                        },
                    ),
                    transform(
                        "sat_score_projection",
                        "shape_preserving_projection",
                        module_ref=MODULE_REF,
                        source_table="satscores",
                        fields={
                            "cds": field_source("satscores.cds"),
                            "record_type": field_source("satscores.rtype"),
                            "avg_math": field_source("satscores.AvgScrMath"),
                            "avg_read": field_source("satscores.AvgScrRead"),
                            "num_ge_1500": field_source("satscores.NumGE1500"),
                        },
                    ),
                ],
            )
        ],
    )
