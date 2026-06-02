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
