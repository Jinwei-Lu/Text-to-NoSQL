from __future__ import annotations

from collections import defaultdict
from typing import Any

from ...execution import world_signature as compute_world_signature
from ..audit import audit_database_structure, validate_structure_gate
from ..executor import NativeExecutionResult
from ..recipe import NativeFeature, NativeFeatureManifest, NativeMigrationRecipe
from .common import collection, expr, join, recipe, transform

DESIGN_VERSION = 1
MODULE_REF = __name__
LAB_MEASUREMENT_CODES = [
    "GOT",
    "GPT",
    "LDH",
    "PLT",
    "PT",
    "APTT",
    "FG",
    "PIC",
    "TAT",
    "CRP",
    "DNA",
    "DNA-II",
    "WBC",
    "RBC",
    "HGB",
    "C3",
    "C4",
    "RNP",
]
EXAM_EVIDENCE_CODES = ["aCL IgG", "aCL IgM", "aCL IgA", "ANA", "KCT", "RVVT", "LAC"]


def build_native_recipe(source: Any, db_id: str) -> NativeMigrationRecipe:
    source.schema(db_id)
    return recipe(
        db_id,
        version=DESIGN_VERSION,
        design_goal=(
            "Represent thrombosis patients with date-keyed laboratory panels, "
            "examination event streams, and patient risk tags."
        ),
        collections=[
            collection(
                "patient_clinical_profiles",
                purpose="Patient documents with dynamic laboratory panels by date.",
                source_tables=["Patient", "Laboratory", "Examination"],
                transforms=[
                    transform(
                        "lab_panel_by_date",
                        "dynamic_key_object",
                        module_ref=MODULE_REF,
                        parent_table="Patient",
                        child_table="Laboratory",
                        join=join("Patient.ID", "Laboratory.ID"),
                        target_field="lab_panel_by_date",
                        key=expr("Laboratory.Date", "Laboratory.Date"),
                        values={
                            "got": expr("last(Laboratory.GOT)", "Laboratory.GOT"),
                            "gpt": expr("last(Laboratory.GPT)", "Laboratory.GPT"),
                            "platelets": expr("last(Laboratory.PLT)", "Laboratory.PLT"),
                            "crp": expr("last(Laboratory.CRP)", "Laboratory.CRP"),
                            "dna": expr("last(Laboratory.DNA)", "Laboratory.DNA"),
                        },
                    ),
                    transform(
                        "examination_events",
                        "nested_event_stream",
                        module_ref=MODULE_REF,
                        parent_table="Patient",
                        event_source_table="Examination",
                        join=join("Patient.ID", "Examination.ID"),
                        target_field="examinations",
                        event_type_field="Examination.Diagnosis",
                        event_time_field="Examination.Examination Date",
                        event_payload={
                            "ana": "Examination.ANA",
                            "ana_pattern": "Examination.ANA Pattern",
                            "symptoms": "Examination.Symptoms",
                            "thrombosis": "Examination.Thrombosis",
                        },
                    ),
                    transform(
                        "patient_risk_tags",
                        "derived_tag_array",
                        module_ref=MODULE_REF,
                        target_field="patient_tags",
                        tags={
                            "sle_diagnosis": {
                                "condition": "Patient.Diagnosis == 'SLE'",
                                "provenance": ["Patient.Diagnosis"],
                            },
                            "inpatient": {
                                "condition": "Patient.Admission == '+'",
                                "provenance": ["Patient.Admission"],
                            },
                            "female_patient": {
                                "condition": "Patient.SEX == 'F'",
                                "provenance": ["Patient.SEX"],
                            },
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
    """Build a patient-centered thrombosis prediction MongoDB dataworld."""
    if db_id != "thrombosis_prediction":
        raise ValueError(f"thrombosis materializer received db_id={db_id!r}")
    schema = source.schema(db_id)
    conn = source.connection(db_id)

    patients = _rows(conn, "Patient", ["ID"])
    laboratories = _rows(conn, "Laboratory", ["ID", "Date"])
    examinations = _rows(conn, "Examination", ["ID", "Examination Date"])
    labs_by_patient = _group(laboratories, "ID")
    exams_by_patient = _group(examinations, "ID")

    patient_docs = [
        _patient_profile_doc(
            patient,
            labs_by_patient.get(patient.get("ID"), []),
            exams_by_patient.get(patient.get("ID"), []),
            db_id,
        )
        for patient in patients
    ]
    diagnosis_docs = _diagnosis_risk_panels(patient_docs)
    measurement_docs = _measurement_code_bags(laboratories, examinations)
    data = {
        "patient_clinical_profiles": patient_docs,
        "diagnosis_risk_panels": diagnosis_docs,
        "measurement_code_bags": measurement_docs,
    }

    audit = audit_database_structure(db_id, data)
    features = _native_features()
    manifest = NativeFeatureManifest(db_id=db_id, features=features)
    native_schema = {
        "db_id": db_id,
        "source_tables": list(schema.tables),
        "collections": {
            "patient_clinical_profiles": {
                "document_count": len(patient_docs),
                "root_entity": "patient clinical thrombosis profile",
                "source_tables": ["Patient", "Laboratory", "Examination"],
            },
            "diagnosis_risk_panels": {
                "document_count": len(diagnosis_docs),
                "root_entity": "diagnosis and thrombosis risk panel",
                "source_tables": ["Patient", "Examination"],
            },
            "measurement_code_bags": {
                "document_count": len(measurement_docs),
                "root_entity": "laboratory and examination measurement code bag",
                "source_tables": ["Laboratory", "Examination"],
            },
        },
        "structure_audit": audit.to_dict(),
        "structure_gate": validate_structure_gate(audit).to_dict(),
    }
    provenance = {
        "db_id": db_id,
        "conversion_code_ref": f"{MODULE_REF}.materialize_native_dataworld",
        "entries": {
            feature.id: {
                "source_tables": _source_tables_from_refs(feature.provenance_refs),
                "provenance_refs": list(feature.provenance_refs),
                "field": feature.field,
            }
            for feature in features
        },
    }
    signature = compute_world_signature(data)
    if event_hook is not None:
        event_hook(
            "thrombosis_prediction_native_materialized",
            db_id=db_id,
            collection_count=len(data),
            document_count=sum(len(docs) for docs in data.values()),
            native_feature_count=len(features),
            max_depth=audit.max_depth,
            gate_ok=validate_structure_gate(audit).ok,
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
    order_sql = ", ".join(_quote_ident(name) for name in order_by)
    cursor = conn.execute(f"SELECT * FROM {_quote_ident(table)} ORDER BY {order_sql}")
    columns = [str(item[0]) for item in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _group(rows: list[dict[str, Any]], key: str) -> dict[Any, list[dict[str, Any]]]:
    grouped: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row.get(key)].append(row)
    return dict(grouped)


def _patient_profile_doc(
    patient: dict[str, Any],
    labs: list[dict[str, Any]],
    exams: list[dict[str, Any]],
    db_id: str,
) -> dict[str, Any]:
    patient_id = patient.get("ID")
    lab_panels_by_year = _lab_panels_by_year(labs)
    events = [_examination_event(row) for row in exams]
    risk_tags = _risk_tags(patient, labs, exams)
    return {
        "_id": f"patient:{patient_id}",
        "identity": {
            "source_db": db_id,
            "patient_id": patient_id,
            "sex": _value_state(patient.get("SEX")),
            "admission": _value_state(patient.get("Admission")),
            "profile_dates": {
                "birthday": _value_state(patient.get("Birthday")),
                "description_date": _value_state(patient.get("Description")),
                "first_clinical_date": _value_state(patient.get("First Date")),
            },
        },
        "diagnosis": {
            "patient_diagnosis": _value_state(patient.get("Diagnosis")),
            "diagnosis_group": _diagnosis_group(patient.get("Diagnosis")),
            "exam_diagnoses_by_state": _exam_diagnoses_by_state(events),
        },
        "timeline": {
            "lab_panels_by_year": lab_panels_by_year,
            "events": events,
            "timeline_presence": {
                "laboratory": _presence_state(labs),
                "examination": _presence_state(exams),
                "external_medication_history": "missing",
            },
        },
        "risk_profile": {
            "clinical_risk_tags": risk_tags,
            "risk_group": _risk_group(patient, labs, exams),
            "thrombosis_state": _thrombosis_state(exams),
            "measurement_group_state_buckets": _measurement_group_state_buckets(labs),
        },
        "provenance": {
            "source_tables": ["Patient", "Laboratory", "Examination"],
            "source_columns": [
                "Patient.ID",
                "Patient.SEX",
                "Patient.Birthday",
                "Patient.Description",
                "Patient.First Date",
                "Patient.Admission",
                "Patient.Diagnosis",
                "Laboratory.ID",
                "Laboratory.Date",
                *[f"Laboratory.{code}" for code in LAB_MEASUREMENT_CODES],
                "Examination.ID",
                "Examination.Examination Date",
                "Examination.Diagnosis",
                "Examination.Thrombosis",
            ],
        },
    }


def _lab_panels_by_year(labs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lab in labs:
        grouped[_year_key(lab.get("Date"))].append(lab)
    return {
        year: {
            "presence_state": _presence_state(rows),
            "panel_count": len(rows),
            "panels": [_lab_panel(row) for row in rows],
            "measurement_state_counts": _measurement_state_counts(rows),
        }
        for year, rows in sorted(grouped.items())
    }


def _lab_panel(lab: dict[str, Any]) -> dict[str, Any]:
    measurements = {
        code: _measurement_doc(code, lab.get(code))
        for code in LAB_MEASUREMENT_CODES
    }
    present_codes = [
        code for code, item in measurements.items()
        if item["presence_state"] == "present"
    ]
    return {
        "panel_id": f"{lab.get('ID')}:{lab.get('Date')}",
        "event_time": lab.get("Date"),
        "year": _year_key(lab.get("Date")),
        "panel_state": {
            "presence_state": "present",
            "present_code_count": len(present_codes),
            "missing_code_count": len(measurements) - len(present_codes),
        },
        "measurements_by_code": measurements,
        "measurement_groups_by_state": _measurement_groups_by_state(measurements),
    }


def _measurement_doc(code: str, value: Any) -> dict[str, Any]:
    return {
        "code": code,
        "value": value,
        "presence_state": _presence_state(value),
        "semantic_group": _measurement_group(code),
        "state_bucket": _measurement_state_bucket(code, value),
    }


def _examination_event(exam: dict[str, Any]) -> dict[str, Any]:
    evidence = {
        _dynamic_key(code, "unknown_exam_code"): {
            "code": code,
            "value": exam.get(code),
            "presence_state": _presence_state(exam.get(code)),
            "semantic_group": "antibody_coagulation_evidence",
        }
        for code in EXAM_EVIDENCE_CODES
    }
    thrombosis = exam.get("Thrombosis")
    return {
        "event_id": f"exam:{exam.get('ID')}:{exam.get('Examination Date')}",
        "event_type": _diagnosis_group(exam.get("Diagnosis")),
        "event_time": exam.get("Examination Date"),
        "diagnosis": _value_state(exam.get("Diagnosis")),
        "symptoms": _value_state(exam.get("Symptoms")),
        "thrombosis": {
            "value": thrombosis,
            "presence_state": _presence_state(thrombosis),
            "state": "positive" if thrombosis == 1 else "negative" if thrombosis == 0 else "unknown",
        },
        "evidence_by_code": evidence,
        "evidence_state_buckets": _evidence_state_buckets(evidence),
    }


def _diagnosis_risk_panels(
    patient_docs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for doc in patient_docs:
        grouped[doc["diagnosis"]["diagnosis_group"]].append(doc)
    panels: list[dict[str, Any]] = []
    for diagnosis_group, docs in sorted(grouped.items()):
        by_risk: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_thrombosis: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for doc in docs:
            summary = _patient_summary(doc)
            by_risk[doc["risk_profile"]["risk_group"]].append(summary)
            by_thrombosis[doc["risk_profile"]["thrombosis_state"]].append(summary)
        panels.append(
            {
                "_id": f"diagnosis:{diagnosis_group}",
                "diagnosis_group": diagnosis_group,
                "patient_count": len(docs),
                "patients_by_risk_group": {
                    key: sorted(values, key=lambda item: str(item["patient_id"]))
                    for key, values in sorted(by_risk.items())
                },
                "patients_by_thrombosis_state": {
                    key: sorted(values, key=lambda item: str(item["patient_id"]))
                    for key, values in sorted(by_thrombosis.items())
                },
                "panel_state": {
                    "presence_state": "present",
                    "external_outcome_followup": "missing",
                },
            }
        )
    return panels


def _measurement_code_bags(
    labs: list[dict[str, Any]],
    exams: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    for code in LAB_MEASUREMENT_CODES:
        rows = [row for row in labs if row.get(code) is not None and row.get(code) != ""]
        by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_year[_year_key(row.get("Date"))].append(row)
        docs.append(
            {
                "_id": f"lab_code:{_dynamic_key(code, 'unknown')}",
                "code": code,
                "source_table": "Laboratory",
                "semantic_group": _measurement_group(code),
                "values_by_year": {
                    year: {
                        "reading_count": len(items),
                        "readings": [
                            {
                                "patient_id": item.get("ID"),
                                "date": item.get("Date"),
                                "value": item.get(code),
                                "presence_state": _presence_state(item.get(code)),
                                "state_bucket": _measurement_state_bucket(code, item.get(code)),
                            }
                            for item in items[:40]
                        ],
                    }
                    for year, items in sorted(by_year.items())
                },
                "bag_state": {
                    "presence_state": _presence_state(rows),
                    "supplemental_reference_range": "missing",
                },
            }
        )
    for code in EXAM_EVIDENCE_CODES:
        rows = [row for row in exams if row.get(code) is not None and row.get(code) != ""]
        by_year: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_year[_year_key(row.get("Examination Date"))].append(row)
        docs.append(
            {
                "_id": f"exam_code:{_dynamic_key(code, 'unknown')}",
                "code": code,
                "source_table": "Examination",
                "semantic_group": "antibody_coagulation_evidence",
                "values_by_year": {
                    year: {
                        "reading_count": len(items),
                        "readings": [
                            {
                                "patient_id": item.get("ID"),
                                "date": item.get("Examination Date"),
                                "value": item.get(code),
                                "presence_state": _presence_state(item.get(code)),
                            }
                            for item in items[:40]
                        ],
                    }
                    for year, items in sorted(by_year.items())
                },
                "bag_state": {
                    "presence_state": _presence_state(rows),
                    "supplemental_reference_range": "missing",
                },
            }
        )
    return docs


def _native_features() -> list[NativeFeature]:
    return [
        NativeFeature(
            id="patient_clinical_profiles.lab_timeline_year_matrix",
            type="dynamic_key_object",
            collection="patient_clinical_profiles",
            field="timeline.lab_panels_by_year",
            query_patterns=["lab_year_panel_code_matrix"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$sum", "$size"],
            provenance_refs=["Laboratory.Date", *[f"Laboratory.{code}" for code in LAB_MEASUREMENT_CODES]],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "lab_year_panel_code_matrix",
                        "intent": "roll up year-keyed lab panel buckets and measurement-code bags",
                        "pipeline": [
                            {
                                "$project": {
                                    "patient_id": "$identity.patient_id",
                                    "years": {"$objectToArray": "$timeline.lab_panels_by_year"},
                                }
                            },
                            {"$unwind": "$years"},
                            {"$unwind": "$years.v.panels"},
                            {
                                "$project": {
                                    "year": "$years.k",
                                    "patient_id": 1,
                                    "present_code_count": "$years.v.panels.panel_state.present_code_count",
                                    "codes": {"$objectToArray": "$years.v.panels.measurements_by_code"},
                                }
                            },
                            {"$unwind": "$codes"},
                            {
                                "$group": {
                                    "_id": {
                                        "year": "$year",
                                        "code": "$codes.k",
                                        "state": "$codes.v.presence_state",
                                    },
                                    "reading_count": {"$sum": 1},
                                    "patient_ids": {"$addToSet": "$patient_id"},
                                    "present_code_total": {"$sum": "$present_code_count"},
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "year": "$_id.year",
                                    "code": "$_id.code",
                                    "state": "$_id.state",
                                    "reading_count": 1,
                                    "patient_count": {"$size": "$patient_ids"},
                                    "present_code_total": 1,
                                }
                            },
                            {"$sort": {"year": 1, "code": 1, "state": 1}},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum", "$size"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="patient_clinical_profiles.thrombosis_diagnosis_events",
            type="nested_event_stream",
            collection="patient_clinical_profiles",
            field="timeline.events",
            query_patterns=["thrombosis_event_evidence_filter"],
            required_constructs=["$filter", "$ifNull", "$objectToArray", "$size"],
            provenance_refs=[
                "Examination.Examination Date",
                "Examination.Diagnosis",
                "Examination.Thrombosis",
                *[f"Examination.{code}" for code in EXAM_EVIDENCE_CODES],
            ],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "thrombosis_event_evidence_filter",
                        "intent": "filter patient event streams for thrombosis-positive diagnosis evidence",
                        "pipeline": [
                            {
                                "$project": {
                                    "patient_id": "$identity.patient_id",
                                    "matching_events": {
                                        "$filter": {
                                            "input": {"$ifNull": ["$timeline.events", []]},
                                            "as": "event",
                                            "cond": {
                                                "$eq": ["$$event.thrombosis.state", "positive"]
                                            },
                                        }
                                    },
                                }
                            },
                            {
                                "$match": {
                                    "$expr": {"$gt": [{"$size": "$matching_events"}, 0]}
                                }
                            },
                            {"$project": {"patient_id": 1, "matching_events": 1}},
                        ],
                        "mongo_native_constructs": ["$filter", "$ifNull", "$size"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="diagnosis_risk_panels.risk_group_patient_matrix",
            type="dynamic_key_object",
            collection="diagnosis_risk_panels",
            field="patients_by_risk_group",
            query_patterns=["diagnosis_risk_group_patient_matrix"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$sum"],
            provenance_refs=["Patient.Diagnosis", "Patient.Admission", "Examination.Thrombosis"],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "diagnosis_risk_group_patient_matrix",
                        "intent": "compare diagnosis panels through dynamic risk-group patient buckets",
                        "pipeline": [
                            {
                                "$project": {
                                    "diagnosis_group": 1,
                                    "risk_groups": {"$objectToArray": "$patients_by_risk_group"},
                                }
                            },
                            {"$unwind": "$risk_groups"},
                            {"$unwind": "$risk_groups.v"},
                            {
                                "$group": {
                                    "_id": {
                                        "diagnosis_group": "$diagnosis_group",
                                        "risk_group": "$risk_groups.k",
                                    },
                                    "patient_count": {"$sum": 1},
                                }
                            },
                            {"$sort": {"patient_count": -1, "_id.diagnosis_group": 1}},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum"],
                    }
                ]
            },
        ),
        NativeFeature(
            id="measurement_code_bags.yearly_measurement_distribution",
            type="dynamic_key_object",
            collection="measurement_code_bags",
            field="values_by_year",
            query_patterns=["measurement_code_year_distribution"],
            required_constructs=["$objectToArray", "$unwind", "$group", "$sum"],
            provenance_refs=[
                "Laboratory.Date",
                *[f"Laboratory.{code}" for code in LAB_MEASUREMENT_CODES],
                "Examination.Examination Date",
                *[f"Examination.{code}" for code in EXAM_EVIDENCE_CODES],
            ],
            extra={
                "pipeline_blueprints": [
                    {
                        "query_pattern": "measurement_code_year_distribution",
                        "intent": "fold code-bag readings through dynamic year buckets",
                        "pipeline": [
                            {
                                "$project": {
                                    "code": 1,
                                    "semantic_group": 1,
                                    "years": {"$objectToArray": "$values_by_year"},
                                }
                            },
                            {"$unwind": "$years"},
                            {
                                "$group": {
                                    "_id": {
                                        "code": "$code",
                                        "year": "$years.k",
                                        "group": "$semantic_group",
                                    },
                                    "reading_count": {"$sum": "$years.v.reading_count"},
                                }
                            },
                            {"$sort": {"reading_count": -1, "_id.code": 1}},
                        ],
                        "mongo_native_constructs": ["$objectToArray", "$unwind", "$group", "$sum"],
                    }
                ]
            },
        ),
    ]


def _patient_summary(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "patient_id": doc["identity"]["patient_id"],
        "sex": doc["identity"]["sex"],
        "risk_group": doc["risk_profile"]["risk_group"],
        "thrombosis_state": doc["risk_profile"]["thrombosis_state"],
        "clinical_risk_tags": list(doc["risk_profile"]["clinical_risk_tags"]),
    }


def _risk_tags(
    patient: dict[str, Any],
    labs: list[dict[str, Any]],
    exams: list[dict[str, Any]],
) -> list[str]:
    tags: list[str] = []
    diagnosis_group = _diagnosis_group(patient.get("Diagnosis"))
    if diagnosis_group != "diagnosis_unknown":
        tags.append(f"diagnosis:{diagnosis_group}")
    if patient.get("Admission") == "+":
        tags.append("admitted_profile")
    if patient.get("SEX") == "F":
        tags.append("female_patient")
    if any(exam.get("Thrombosis") == 1 for exam in exams):
        tags.append("observed_thrombosis")
    if any(_numeric(lab.get("PLT")) is not None and _numeric(lab.get("PLT")) < 150 for lab in labs):
        tags.append("low_platelet_signal")
    if any(_numeric(lab.get("CRP")) is not None and _numeric(lab.get("CRP")) > 1 for lab in labs):
        tags.append("elevated_crp_signal")
    return sorted(set(tags))


def _risk_group(
    patient: dict[str, Any],
    labs: list[dict[str, Any]],
    exams: list[dict[str, Any]],
) -> str:
    if any(exam.get("Thrombosis") == 1 for exam in exams):
        return "thrombosis_observed"
    tags = _risk_tags(patient, labs, exams)
    if "low_platelet_signal" in tags or "elevated_crp_signal" in tags:
        return "lab_signal_watch"
    if patient.get("Admission") == "+":
        return "admitted_baseline"
    return "routine_profile"


def _thrombosis_state(exams: list[dict[str, Any]]) -> str:
    if any(exam.get("Thrombosis") == 1 for exam in exams):
        return "positive"
    if any(exam.get("Thrombosis") == 0 for exam in exams):
        return "negative"
    return "missing"


def _exam_diagnoses_by_state(events: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        grouped[event["diagnosis"]["presence_state"]].append(
            {
                "event_time": event["event_time"],
                "event_type": event["event_type"],
                "diagnosis": event["diagnosis"]["value"],
            }
        )
    return {key: values for key, values in sorted(grouped.items())}


def _measurement_state_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        for code in LAB_MEASUREMENT_CODES:
            counts[_presence_state(row.get(code))] += 1
    return dict(sorted(counts.items()))


def _measurement_groups_by_state(
    measurements: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for code, item in measurements.items():
        grouped[item["presence_state"]].append(
            {
                "code": code,
                "semantic_group": item["semantic_group"],
                "state_bucket": item["state_bucket"],
            }
        )
    return {key: values for key, values in sorted(grouped.items())}


def _measurement_group_state_buckets(labs: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    buckets: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for lab in labs:
        for code in LAB_MEASUREMENT_CODES:
            buckets[_measurement_group(code)][_measurement_state_bucket(code, lab.get(code))] += 1
    return {group: dict(sorted(states.items())) for group, states in sorted(buckets.items())}


def _evidence_state_buckets(
    evidence: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, item in evidence.items():
        grouped[item["presence_state"]].append({"code": key, "value": item["value"]})
    return {key: values for key, values in sorted(grouped.items())}


def _measurement_group(code: str) -> str:
    if code in {"PLT", "PT", "APTT", "FG", "PIC", "TAT"}:
        return "coagulation_panel"
    if code in {"WBC", "RBC", "HGB"}:
        return "blood_count_panel"
    if code in {"CRP", "C3", "C4", "RNP", "DNA", "DNA-II"}:
        return "immune_inflammation_panel"
    return "chemistry_panel"


def _measurement_state_bucket(code: str, value: Any) -> str:
    if _presence_state(value) != "present":
        return _presence_state(value)
    number = _numeric(value)
    if number is None:
        return "qualitative_present"
    if code == "PLT" and number < 150:
        return "low"
    if code == "CRP" and number > 1:
        return "elevated"
    if code == "WBC" and number > 10:
        return "elevated"
    return "observed"


def _diagnosis_group(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return "diagnosis_unknown"
    if "SLE" in text:
        return "sle"
    if "MCTD" in text:
        return "mctd"
    if "PSS" in text:
        return "pss"
    if "RA" in text:
        return "ra"
    if "AMI" in text:
        return "ami"
    return _dynamic_key(text.lower(), "diagnosis_other")


def _value_state(value: Any) -> dict[str, Any]:
    return {"value": value, "presence_state": _presence_state(value)}


def _presence_state(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, str) and not value.strip():
        return "empty"
    if isinstance(value, (list, dict, tuple, set)) and not value:
        return "empty"
    return "present"


def _numeric(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _year_key(value: Any) -> str:
    if value is None:
        return "unknown_year"
    text = str(value)
    return text[:4] if len(text) >= 4 else "unknown_year"


def _dynamic_key(value: Any, fallback: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return fallback
    out = "".join(char if char.isalnum() else "_" for char in text)
    while "__" in out:
        out = out.replace("__", "_")
    return out.strip("_") or fallback


def _source_tables_from_refs(refs: list[str]) -> list[str]:
    return sorted({ref.split(".", 1)[0] for ref in refs if "." in ref})


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'
