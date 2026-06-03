from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..native_audit import audit_database_structure
from ..native_executor import NativeExecutionResult
from ..native_recipe import NativeFeature, NativeFeatureManifest, NativeMigrationRecipe
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


def materialize_native_dataworld(
    source: Any,
    db_id: str,
    *,
    event_hook: Any = None,
) -> NativeExecutionResult:
    """Build California school documents from the BIRD SQLite schema."""
    if db_id != "california_schools":
        raise ValueError(f"california_schools materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    schools = _rows(conn, "schools", ["County", "District", "CDSCode"])
    frpm_rows = _rows(conn, "frpm", ["County Name", "District Name", "CDSCode"])
    sat_rows = _rows(conn, "satscores", ["cname", "dname", "rtype", "cds"])

    frpm_by_cds = _group_by(frpm_rows, "CDSCode")
    sat_by_cds = _group_by(sat_rows, "cds")
    schools_by_district = _group_by_pair(schools, "County", "District")
    frpm_by_district = _group_by_pair(frpm_rows, "County Name", "District Name")
    sat_by_district = _group_by_pair(sat_rows, "cname", "dname")
    districts_by_county: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for county, district in sorted(schools_by_district):
        districts_by_county[county].append((county, district))

    school_docs = [
        _school_doc(
            school,
            frpm_by_cds.get(school["CDSCode"], []),
            sat_by_cds.get(school["CDSCode"], []),
        )
        for school in schools
    ]
    district_docs = [
        _district_doc(
            county,
            district,
            schools_by_district[(county, district)],
            frpm_by_district.get((county, district), []),
            sat_by_district.get((county, district), []),
        )
        for county, district in sorted(schools_by_district)
    ]
    county_docs = [
        _county_panel_doc(
            county,
            district_keys,
            schools_by_district,
            frpm_by_district,
            sat_by_district,
        )
        for county, district_keys in sorted(districts_by_county.items())
    ]

    data = {
        "county_equity_assessment_panels": county_docs,
        "district_rollups": district_docs,
        "school_profiles": school_docs,
    }
    audit = audit_database_structure(db_id, data)
    manifest = _manifest()
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "school_profiles": {
                "document_count": len(school_docs),
                "root_entity": "school profile",
                "source_tables": ["schools", "frpm", "satscores"],
            },
            "district_rollups": {
                "document_count": len(district_docs),
                "root_entity": "county/district rollup",
                "source_tables": ["schools", "frpm", "satscores"],
            },
            "county_equity_assessment_panels": {
                "document_count": len(county_docs),
                "root_entity": "county equity and assessment panel",
                "source_tables": ["schools", "frpm", "satscores"],
            },
        },
        "structure_audit": audit.to_dict(),
    }
    provenance = _provenance()
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "california_schools_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(manifest.features),
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


def _rows(conn: Any, table: str, order_by: list[str]) -> list[dict[str, Any]]:
    order_sql = ", ".join(f'"{column}"' for column in order_by)
    cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY {order_sql}')
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _group_by(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return dict(grouped)


def _group_by_pair(
    rows: list[dict[str, Any]],
    left_key: str,
    right_key: str,
) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            str(row.get(left_key) or "Unknown County"),
            str(row.get(right_key) or "Unknown District"),
        )
        grouped[key].append(row)
    return dict(grouped)


def _school_doc(
    school: dict[str, Any],
    frpm_rows: list[dict[str, Any]],
    sat_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    school_name = school.get("School") or school.get("District")
    grade_span = _grade_span_from_school(school)
    return {
        "_id": f"school:{school['CDSCode']}",
        "school_profile": {
            "identity": {
                "cds_code": school.get("CDSCode"),
                "nces": {
                    "district": {
                        "value": school.get("NCESDist"),
                        "presence_state": _presence_state(school.get("NCESDist")),
                    },
                    "school": {
                        "value": school.get("NCESSchool"),
                        "presence_state": _presence_state(school.get("NCESSchool")),
                    },
                },
                "name": {
                    "value": school_name,
                    "presence_state": _presence_state(school.get("School")),
                },
                "status": {
                    "type": school.get("StatusType"),
                    "open_date": {
                        "value": school.get("OpenDate"),
                        "presence_state": _presence_state(school.get("OpenDate")),
                    },
                    "closed_date": {
                        "value": school.get("ClosedDate"),
                        "presence_state": _presence_state(school.get("ClosedDate")),
                    },
                },
            },
            "district_context": {
                "county": school.get("County"),
                "district": school.get("District"),
                "county_key": _safe_key(school.get("County")),
                "district_key": _district_key(school.get("County"), school.get("District")),
                "doc_code": school.get("DOC"),
                "doc_type": school.get("DOCType"),
            },
            "locale": {
                "physical_address": _address_block(school, prefix=""),
                "mailing_address": _address_block(school, prefix="Mail"),
                "contact": {
                    "phone": {
                        "value": school.get("Phone"),
                        "presence_state": _presence_state(school.get("Phone")),
                    },
                    "extension": {
                        "value": school.get("Ext"),
                        "presence_state": _presence_state(school.get("Ext")),
                    },
                    "website": {
                        "value": school.get("Website"),
                        "presence_state": _presence_state(school.get("Website")),
                    },
                },
                "geo": {
                    "latitude": school.get("Latitude"),
                    "longitude": school.get("Longitude"),
                    "presence_state": "present"
                    if school.get("Latitude") is not None and school.get("Longitude") is not None
                    else "null",
                },
            },
            "grade_span": {
                "served": school.get("GSserved"),
                "offered": school.get("GSoffered"),
                "instructional_level": {
                    "code": school.get("EILCode"),
                    "name": school.get("EILName"),
                    "presence_state": _presence_state(school.get("EILName")),
                },
                "span_key": grade_span,
                "presence_state": _presence_state(
                    grade_span if grade_span != "unknown_grade_span" else None
                ),
            },
            "programs": {
                "tags": _program_tags(school),
                "charter": {
                    "flag": bool(school.get("Charter")),
                    "presence_state": _presence_state(school.get("Charter")),
                    "number": {
                        "value": school.get("CharterNum"),
                        "presence_state": _presence_state(school.get("CharterNum")),
                    },
                    "funding_type": {
                        "value": school.get("FundingType"),
                        "presence_state": _presence_state(school.get("FundingType")),
                    },
                },
                "magnet": {
                    "flag": bool(school.get("Magnet")),
                    "presence_state": _presence_state(school.get("Magnet")),
                },
                "virtual": {
                    "code": school.get("Virtual"),
                    "presence_state": _presence_state(school.get("Virtual")),
                    "delivery_mode": _virtual_label(school.get("Virtual")),
                },
                "educational_option": {
                    "code": school.get("EdOpsCode"),
                    "name": school.get("EdOpsName"),
                    "presence_state": _presence_state(school.get("EdOpsName")),
                },
            },
        },
        "equity_panels": {
            "frpm_by_academic_year": _frpm_by_year(frpm_rows, school),
            "grade_span_history": _grade_span_history(frpm_rows, school),
        },
        "assessment_panels": {
            "sat_by_record_type": _sat_by_record_type(sat_rows),
            "readiness_summary": _sat_readiness_summary(sat_rows),
        },
        "panel_views": [
            {
                "panel": "grade_span_program_equity",
                "metrics_by_grade_span": _metrics_by_grade_span(grade_span, frpm_rows, sat_rows),
            },
            {
                "panel": "school_status_program_profile",
                "metrics_by_grade_span": {
                    grade_span: {
                        "status": school.get("StatusType"),
                        "program_tags": _program_tags(school),
                        "profile_presence_state": _presence_state(school.get("School")),
                    }
                },
            },
        ],
        "schema_state": {
            "school_name": _presence_state(school.get("School")),
            "frpm_panel": _presence_state(frpm_rows),
            "sat_panel": _presence_state(sat_rows),
            "phone": _presence_state(school.get("Phone")),
            "website": _presence_state(school.get("Website")),
            "external_demographics_table": "missing",
        },
        "_provenance": {
            "source_tables": ["schools", "frpm", "satscores"],
            "source_keys": {
                "CDSCode": school.get("CDSCode"),
                "county": school.get("County"),
                "district": school.get("District"),
            },
        },
    }


def _district_doc(
    county: str,
    district: str,
    schools: list[dict[str, Any]],
    frpm_rows: list[dict[str, Any]],
    sat_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "_id": f"district:{_district_key(county, district)}",
        "district_identity": {
            "county": county,
            "district": district,
            "district_key": _district_key(county, district),
            "source_state": "CA",
        },
        "schools_by_grade_span": _schools_by_grade_span(schools, frpm_rows),
        "program_mix": _program_mix(schools),
        "equity_by_academic_year": _district_equity_by_year(frpm_rows),
        "assessment_by_record_type": _sat_by_record_type(sat_rows),
        "panel_views": [
            {
                "panel": "district_grade_span_equity",
                "metrics_by_grade_span": _district_metrics_by_grade_span(
                    schools,
                    frpm_rows,
                    sat_rows,
                ),
            }
        ],
        "schema_state": {
            "schools": _presence_state(schools),
            "frpm": _presence_state(frpm_rows),
            "sat": _presence_state(sat_rows),
            "student_level_demographics": "missing",
        },
    }


def _county_panel_doc(
    county: str,
    district_keys: list[tuple[str, str]],
    schools_by_district: dict[tuple[str, str], list[dict[str, Any]]],
    frpm_by_district: dict[tuple[str, str], list[dict[str, Any]]],
    sat_by_district: dict[tuple[str, str], list[dict[str, Any]]],
) -> dict[str, Any]:
    districts_by_district_key: dict[str, dict[str, Any]] = {}
    sat_readiness_by_record_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key in district_keys:
        district_schools = schools_by_district.get(key, [])
        frpm_rows = frpm_by_district.get(key, [])
        sat_rows = sat_by_district.get(key, [])
        district_key = _district_key(*key)
        districts_by_district_key[district_key] = {
            "district": key[1],
            "school_count": len(district_schools),
            "active_school_count": sum(
                1 for school in district_schools if school.get("StatusType") == "Active"
            ),
            "equity_by_academic_year": _district_equity_by_year(frpm_rows),
            "assessment_summary": _sat_readiness_summary(sat_rows),
        }
        for rtype, rows in _group_by(sat_rows, "rtype").items():
            sat_readiness_by_record_type[_safe_key(rtype)].append(
                {
                    "district_key": district_key,
                    "district": key[1],
                    "tested": sum(int(row.get("NumTstTakr") or 0) for row in rows),
                    "ge_1500": sum(int(row.get("NumGE1500") or 0) for row in rows),
                    "presence_state": _presence_state(rows),
                }
            )
    return {
        "_id": f"county:{_safe_key(county)}",
        "county": county,
        "districts_by_district_key": districts_by_district_key,
        "sat_frpm_readiness_by_record_type": dict(sorted(sat_readiness_by_record_type.items())),
        "panel_views": [
            {
                "panel": "county_sat_frpm_readiness",
                "metrics_by_grade_span": _county_metrics_by_grade_span(
                    [
                        school
                        for key in district_keys
                        for school in schools_by_district.get(key, [])
                    ],
                    [row for key in district_keys for row in frpm_by_district.get(key, [])],
                    [row for key in district_keys for row in sat_by_district.get(key, [])],
                ),
            }
        ],
        "schema_state": {
            "districts": _presence_state(district_keys),
            "sat": _presence_state(
                [row for key in district_keys for row in sat_by_district.get(key, [])]
            ),
            "county_locale_panel": "present",
            "student_level_demographics": "missing",
        },
    }


def _frpm_by_year(
    frpm_rows: list[dict[str, Any]],
    school: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for year, rows in sorted(_group_by(frpm_rows, "Academic Year").items()):
        grade_spans = []
        for row in rows:
            grade_spans.extend(_frpm_grade_span_metrics(row))
        out[_safe_key(year)] = {
            "presence_state": _presence_state(rows),
            "county": rows[0].get("County Name") if rows else school.get("County"),
            "district": rows[0].get("District Name") if rows else school.get("District"),
            "school_snapshots": [
                {
                    "school_name": row.get("School Name"),
                    "school_type": row.get("School Type"),
                    "district_type": row.get("District Type"),
                    "nslp_provision_status": {
                        "value": row.get("NSLP Provision Status"),
                        "presence_state": _presence_state(row.get("NSLP Provision Status")),
                    },
                    "calpads_certified": bool(
                        row.get("2013-14 CALPADS Fall 1 Certification Status")
                    ),
                }
                for row in rows
            ],
            "grade_spans": grade_spans,
        }
    return out


def _frpm_grade_span_metrics(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "grade_span": "K-12",
            "low_grade": row.get("Low Grade"),
            "high_grade": row.get("High Grade"),
            "enrollment": row.get("Enrollment (K-12)"),
            "meal_programs": {
                "free_meal_count": row.get("Free Meal Count (K-12)"),
                "free_pct": row.get("Percent (%) Eligible Free (K-12)"),
                "frpm_count": row.get("FRPM Count (K-12)"),
                "frpm_pct": row.get("Percent (%) Eligible FRPM (K-12)"),
                "frpm_presence_state": _presence_state(row.get("FRPM Count (K-12)")),
            },
        },
        {
            "grade_span": "Ages 5-17",
            "low_grade": row.get("Low Grade"),
            "high_grade": row.get("High Grade"),
            "enrollment": row.get("Enrollment (Ages 5-17)"),
            "meal_programs": {
                "free_meal_count": row.get("Free Meal Count (Ages 5-17)"),
                "free_pct": row.get("Percent (%) Eligible Free (Ages 5-17)"),
                "frpm_count": row.get("FRPM Count (Ages 5-17)"),
                "frpm_pct": row.get("Percent (%) Eligible FRPM (Ages 5-17)"),
                "frpm_presence_state": _presence_state(row.get("FRPM Count (Ages 5-17)")),
            },
        },
    ]


def _grade_span_history(
    frpm_rows: list[dict[str, Any]],
    school: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if frpm_rows:
        for row in frpm_rows:
            grouped[_safe_key(_frpm_grade_span_key(row))].append(row)
    else:
        grouped[_grade_span_from_school(school)] = []
    return {
        span: [
            {
                "academic_year": row.get("Academic Year"),
                "school_name": row.get("School Name"),
                "enrollment_k12": row.get("Enrollment (K-12)"),
                "frpm_pct_k12": row.get("Percent (%) Eligible FRPM (K-12)"),
                "presence_state": _presence_state(row.get("Enrollment (K-12)")),
            }
            for row in rows
        ]
        for span, rows in sorted(grouped.items())
    }


def _sat_by_record_type(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for rtype, group in sorted(_group_by(rows, "rtype").items()):
        out[_safe_key(rtype)] = [
            {
                "cds": row.get("cds"),
                "record_type": row.get("rtype"),
                "school_name": {
                    "value": row.get("sname"),
                    "presence_state": _presence_state(row.get("sname")),
                },
                "district_name": row.get("dname"),
                "county_name": row.get("cname"),
                "enroll12": row.get("enroll12"),
                "tested": row.get("NumTstTakr"),
                "scores": {
                    "reading": row.get("AvgScrRead"),
                    "math": row.get("AvgScrMath"),
                    "writing": row.get("AvgScrWrite"),
                    "score_presence_state": _presence_state(row.get("AvgScrRead")),
                },
                "readiness": {
                    "num_ge_1500": row.get("NumGE1500"),
                    "ge_1500_rate": _ratio(row.get("NumGE1500"), row.get("NumTstTakr")),
                },
            }
            for row in group
        ]
    return out


def _sat_readiness_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tested = sum(int(row.get("NumTstTakr") or 0) for row in rows)
    ge_1500 = sum(int(row.get("NumGE1500") or 0) for row in rows)
    return {
        "record_count": len(rows),
        "tested": tested,
        "ge_1500": ge_1500,
        "ge_1500_rate": _ratio(ge_1500, tested),
        "presence_state": _presence_state(rows),
    }


def _metrics_by_grade_span(
    grade_span: str,
    frpm_rows: list[dict[str, Any]],
    sat_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        grade_span: {
            "frpm": _frpm_summary(frpm_rows),
            "sat": _sat_readiness_summary(sat_rows),
            "panel_presence_state": _presence_state(frpm_rows or sat_rows),
        }
    }


def _district_metrics_by_grade_span(
    schools: list[dict[str, Any]],
    frpm_rows: list[dict[str, Any]],
    sat_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    sat_summary = _sat_readiness_summary(sat_rows)
    out: dict[str, dict[str, Any]] = {}
    frpm_by_span = defaultdict(list)
    for row in frpm_rows:
        frpm_by_span[_safe_key(_frpm_grade_span_key(row))].append(row)
    school_by_span = defaultdict(list)
    for school in schools:
        school_by_span[_grade_span_from_school(school)].append(school)
    for span in sorted(set(frpm_by_span) | set(school_by_span)):
        out[span] = {
            "school_count": len(school_by_span.get(span, [])),
            "active_school_count": sum(
                1 for school in school_by_span.get(span, []) if school.get("StatusType") == "Active"
            ),
            "frpm": _frpm_summary(frpm_by_span.get(span, [])),
            "sat": sat_summary,
            "presence_state": _presence_state(
                school_by_span.get(span, []) or frpm_by_span.get(span, [])
            ),
        }
    return out


def _county_metrics_by_grade_span(
    schools: list[dict[str, Any]],
    frpm_rows: list[dict[str, Any]],
    sat_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return _district_metrics_by_grade_span(schools, frpm_rows, sat_rows)


def _schools_by_grade_span(
    schools: list[dict[str, Any]],
    frpm_rows: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    frpm_by_cds = _group_by(frpm_rows, "CDSCode")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for school in schools:
        grouped[_grade_span_from_school(school)].append(school)
    return {
        span: [
            {
                "cds_code": school.get("CDSCode"),
                "school": school.get("School"),
                "status": school.get("StatusType"),
                "program_tags": _program_tags(school),
                "frpm": _frpm_summary(frpm_by_cds.get(school.get("CDSCode"), [])),
            }
            for school in sorted(group, key=lambda row: str(row.get("CDSCode")))
        ]
        for span, group in sorted(grouped.items())
    }


def _district_equity_by_year(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        _safe_key(year): _frpm_summary(group)
        for year, group in sorted(_group_by(rows, "Academic Year").items())
    }


def _frpm_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    enrollment = _sum(rows, "Enrollment (K-12)")
    frpm_count = _sum(rows, "FRPM Count (K-12)")
    return {
        "school_count": len(rows),
        "enrollment_k12": enrollment,
        "free_meal_count_k12": _sum(rows, "Free Meal Count (K-12)"),
        "frpm_count_k12": frpm_count,
        "frpm_pct_k12": _ratio(frpm_count, enrollment),
        "presence_state": _presence_state(rows),
    }


def _program_mix(schools: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "charter_count": sum(1 for school in schools if school.get("Charter")),
        "magnet_count": sum(1 for school in schools if school.get("Magnet")),
        "virtual_count": sum(
            1 for school in schools if school.get("Virtual") not in (None, "", "N")
        ),
        "closed_count": sum(1 for school in schools if school.get("ClosedDate") is not None),
        "presence_state": _presence_state(schools),
    }


def _address_block(row: dict[str, Any], *, prefix: str) -> dict[str, Any]:
    street_key = f"{prefix}Street" if prefix else "Street"
    street_abbr_key = "MailStrAbr" if prefix else "StreetAbr"
    city_key = f"{prefix}City" if prefix else "City"
    zip_key = f"{prefix}Zip" if prefix else "Zip"
    state_key = f"{prefix}State" if prefix else "State"
    return {
        "street": row.get(street_key),
        "street_abbreviation": row.get(street_abbr_key),
        "city": row.get(city_key),
        "zip": row.get(zip_key),
        "state": row.get(state_key),
        "presence_state": _presence_state(row.get(street_key)),
    }


def _grade_span_from_school(school: dict[str, Any]) -> str:
    return _safe_key(
        school.get("GSserved")
        or school.get("GSoffered")
        or school.get("EILName")
        or "unknown_grade_span"
    )


def _frpm_grade_span_key(row: dict[str, Any]) -> str:
    low = row.get("Low Grade")
    high = row.get("High Grade")
    if low and high:
        return f"{low}-{high}"
    return str(row.get("School Type") or "unknown_grade_span")


def _program_tags(school: dict[str, Any]) -> list[str]:
    tags = []
    if school.get("Charter"):
        tags.append("charter")
    if school.get("Magnet"):
        tags.append("magnet")
    if school.get("Virtual") not in (None, "", "N"):
        tags.append("virtual_or_blended")
    if school.get("ClosedDate") is not None:
        tags.append("closed")
    if school.get("EdOpsName"):
        tags.append(_safe_key(school.get("EdOpsName")).lower())
    return tags


def _virtual_label(value: Any) -> str:
    return {
        "F": "full_virtual",
        "V": "virtual",
        "P": "primarily_classroom_with_virtual",
        "N": "not_virtual",
    }.get(str(value), "not_reported" if value is None else str(value))


def _presence_state(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str):
        return "empty" if value == "" else "present"
    if isinstance(value, (list, tuple, dict, set)):
        return "present" if len(value) > 0 else "empty"
    return "present"


def _sum(rows: list[dict[str, Any]], field: str) -> float:
    return round(sum(float(row.get(field) or 0.0) for row in rows), 4)


def _ratio(numerator: Any, denominator: Any) -> float | None:
    if denominator in (None, 0, 0.0):
        return None
    return round(float(numerator or 0.0) / float(denominator), 6)


def _safe_key(value: Any) -> str:
    text = str(value if value is not None else "unknown")
    text = text.replace(".", "_dot_").replace("$", "S")
    text = re.sub(r"\s+", " ", text).strip()
    return text or "unknown"


def _district_key(county: Any, district: Any) -> str:
    return f"{_safe_key(county)}::{_safe_key(district)}"


def _manifest() -> NativeFeatureManifest:
    return NativeFeatureManifest(
        db_id="california_schools",
        features=[
            NativeFeature(
                id="school_profiles.frpm_by_academic_year",
                type="dynamic_key_object",
                collection="school_profiles",
                field="equity_panels.frpm_by_academic_year",
                query_patterns=["school_frpm_year_trend"],
                required_constructs=["$objectToArray", "$unwind", "$project"],
                provenance_refs=[
                    "frpm.Academic Year",
                    "frpm.Enrollment (K-12)",
                    "frpm.FRPM Count (K-12)",
                    "schools.CDSCode",
                ],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "school_frpm_year_trend",
                            "intent": (
                                "traverse academic-year FRPM buckets for school-level "
                                "meal-program trend queries"
                            ),
                            "pipeline": [
                                {
                                    "$project": {
                                        "school": "$school_profile.identity.name.value",
                                        "district": "$school_profile.district_context.district",
                                        "years": {
                                            "$objectToArray": (
                                                "$equity_panels.frpm_by_academic_year"
                                            )
                                        },
                                    }
                                },
                                {"$unwind": "$years"},
                                {"$unwind": "$years.v.grade_spans"},
                                {
                                    "$project": {
                                        "_id": 0,
                                        "school": 1,
                                        "district": 1,
                                        "academic_year": "$years.k",
                                        "grade_span": "$years.v.grade_spans.grade_span",
                                        "frpm_pct": "$years.v.grade_spans.meal_programs.frpm_pct",
                                    }
                                },
                                {"$sort": {"district": 1, "school": 1, "academic_year": 1}},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="district_rollups.schools_by_grade_span",
                type="dynamic_key_object",
                collection="district_rollups",
                field="schools_by_grade_span",
                query_patterns=["district_grade_span_equity_comparison"],
                required_constructs=["$objectToArray", "$unwind", "$group"],
                provenance_refs=[
                    "schools.GSserved",
                    "schools.GSoffered",
                    "frpm.Low Grade",
                    "frpm.High Grade",
                    "frpm.Percent (%) Eligible FRPM (K-12)",
                ],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "district_grade_span_equity_comparison",
                            "intent": "compare FRPM exposure across district grade-span buckets",
                            "pipeline": [
                                {
                                    "$project": {
                                        "district": "$district_identity.district",
                                        "county": "$district_identity.county",
                                        "grade_spans": {"$objectToArray": "$schools_by_grade_span"},
                                    }
                                },
                                {"$unwind": "$grade_spans"},
                                {"$unwind": "$grade_spans.v"},
                                {
                                    "$group": {
                                        "_id": {
                                            "county": "$county",
                                            "district": "$district",
                                            "grade_span": "$grade_spans.k",
                                        },
                                        "school_count": {"$sum": 1},
                                        "avg_frpm_pct": {
                                            "$avg": "$grade_spans.v.frpm.frpm_pct_k12"
                                        },
                                    }
                                },
                                {"$sort": {"avg_frpm_pct": -1, "_id.county": 1, "_id.district": 1}},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind", "$group"],
                        }
                    ]
                },
            ),
            NativeFeature(
                id="county_panels.sat_frpm_readiness_by_record_type",
                type="dynamic_key_object",
                collection="county_equity_assessment_panels",
                field="sat_frpm_readiness_by_record_type",
                query_patterns=["county_sat_frpm_readiness_panel"],
                required_constructs=["$objectToArray", "$unwind", "$group"],
                provenance_refs=[
                    "satscores.rtype",
                    "satscores.NumTstTakr",
                    "satscores.NumGE1500",
                    "frpm.FRPM Count (K-12)",
                ],
                extra={
                    "pipeline_blueprints": [
                        {
                            "query_pattern": "county_sat_frpm_readiness_panel",
                            "intent": (
                                "rank county/district readiness using SAT record-type "
                                "buckets and FRPM context"
                            ),
                            "pipeline": [
                                {
                                    "$project": {
                                        "county": 1,
                                        "record_types": {
                                            "$objectToArray": "$sat_frpm_readiness_by_record_type"
                                        },
                                    }
                                },
                                {"$unwind": "$record_types"},
                                {"$unwind": "$record_types.v"},
                                {
                                    "$project": {
                                        "_id": 0,
                                        "county": 1,
                                        "record_type": "$record_types.k",
                                        "district": "$record_types.v.district",
                                        "tested": "$record_types.v.tested",
                                        "ge_1500": "$record_types.v.ge_1500",
                                    }
                                },
                                {"$sort": {"ge_1500": -1, "tested": -1, "county": 1}},
                            ],
                            "mongo_native_constructs": ["$objectToArray", "$unwind"],
                        }
                    ]
                },
            ),
        ],
    )


def _provenance() -> dict[str, Any]:
    return {
        "db_id": "california_schools",
        "conversion_code_ref": (
            "tend.construct.native_designs.california_schools."
            "materialize_native_dataworld"
        ),
        "entries": {
            "school_profiles.frpm_by_academic_year": {
                "source_tables": ["schools", "frpm"],
                "provenance_refs": ["schools.CDSCode", "frpm.CDSCode", "frpm.Academic Year"],
            },
            "district_rollups.schools_by_grade_span": {
                "source_tables": ["schools", "frpm", "satscores"],
                "provenance_refs": ["schools.County", "schools.District", "schools.GSserved"],
            },
            "county_panels.sat_frpm_readiness_by_record_type": {
                "source_tables": ["schools", "frpm", "satscores"],
                "provenance_refs": ["satscores.cname", "satscores.rtype", "satscores.NumGE1500"],
            },
        },
    }
