from __future__ import annotations

from tend.construct.native_audit import audit_database_structure, validate_structure_gate


def test_structure_audit_rejects_shallow_top_level_native_shapes() -> None:
    data = {
        "bank_account_activity": [
            {
                "_id": 1,
                "transaction_totals_by_month": {
                    "1995-03": {"credits": 1000, "withdrawals": 0}
                },
                "transaction_events": [
                    {"event_type": "credit", "event_time": "1995-03-01", "amount": 1000}
                ],
                "account_tags": ["monthly_issuance"],
            }
        ]
    }

    audit = audit_database_structure("financial", data)
    result = validate_structure_gate(audit)

    assert audit.max_depth < 4
    assert result.ok is False
    assert "object -> dynamic key -> array -> object" in "\n".join(result.errors)
    assert "array -> object -> dynamic key" in "\n".join(result.errors)


def test_structure_audit_accepts_deep_dynamic_array_object_shapes() -> None:
    data = {
        "patient_clinical_profiles": [
            {
                "_id": 27654,
                "clinical_timeline": {
                    "by_year": {
                        "1991": {
                            "encounters": [
                                {
                                    "kind": "laboratory",
                                    "lab_panels": [
                                        {
                                            "panel_name": "core_labs",
                                            "measurement_groups": [
                                                {
                                                    "group": "hepatic",
                                                    "measurements_by_code": {
                                                        "LDH": {
                                                            "value": 567,
                                                            "status": "high",
                                                        },
                                                        "GOT": {
                                                            "value": 34,
                                                            "status": "normal",
                                                        },
                                                    },
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        },
                        "1992": {"encounters": []},
                    }
                },
                "schema_state": {
                    "examinations": "present",
                    "lab_panels": "present",
                    "optional_notes": "missing",
                    "empty_aliases": "empty",
                },
                "derived": {"flags_by_rule": {"ever_ldh_high": {"value": True}}},
            }
        ]
    }

    audit = audit_database_structure("thrombosis_prediction", data)
    result = validate_structure_gate(audit)

    assert result.ok is True
    assert audit.max_depth >= 8
    assert any("clinical_timeline.by_year.*.encounters[]" in path for path in audit.dynamic_array_object_paths)
    assert any(
        "measurement_groups[].measurements_by_code.*" in path
        for path in audit.array_object_dynamic_paths
    )
    assert audit.presence_state_counts["present"] == 2
    assert audit.presence_state_counts["missing"] == 1
    assert audit.presence_state_counts["empty"] == 1


def test_structure_audit_reports_dynamic_key_samples_and_array_lengths() -> None:
    data = {
        "club_events": [
            {
                "_id": "event:1",
                "budget": {
                    "by_category": {
                        "Food": {
                            "lines": [
                                {"budget_id": "b1", "expenses": [{"expense_id": "e1"}]},
                                {"budget_id": "b2", "expenses": []},
                            ]
                        },
                        "Advertisement": {"lines": []},
                    }
                },
                "attendance": {
                    "roster": [
                        {
                            "member_id": "m1",
                            "member_snapshot": {
                                "facets_by_dimension": {
                                    "shirt_size": {"value": "Medium"},
                                    "major": {"value": "Business"},
                                }
                            },
                        }
                    ]
                },
            }
        ]
    }

    audit = audit_database_structure("student_club", data)

    by_category = next(item for item in audit.dynamic_key_paths if item.path == "budget.by_category")
    assert by_category.sample_keys == ["Advertisement", "Food"]
    assert audit.array_lengths["budget.by_category.*.lines[]"]["max"] == 2
    assert audit.array_lengths["attendance.roster[]"]["max"] == 1
