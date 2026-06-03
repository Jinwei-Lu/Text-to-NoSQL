from __future__ import annotations

from tend.construction.audit import audit_database_structure, validate_structure_gate


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


def test_structure_audit_isolates_dynamic_key_paths_per_collection() -> None:
    """[H4] audit_database_structure must expose per_collection_paths keyed by collection,
    where each collection carries only its OWN paths. Two collections must not share each
    other's dynamic_key_paths."""
    data = {
        "club_budgets": [
            {
                "_id": "b1",
                "budget": {
                    "by_category": {
                        "Food": {"amount": 10},
                        "Advertisement": {"amount": 5},
                    }
                },
            }
        ],
        "club_rosters": [
            {
                "_id": "r1",
                "roster": {
                    "by_gender": {
                        "F": {"count": 3},
                        "M": {"count": 4},
                    }
                },
            }
        ],
    }

    audit = audit_database_structure("student_club", data)
    per = audit.per_collection_paths

    assert set(per) == {"club_budgets", "club_rosters"}

    budgets_dynamic = per["club_budgets"]["dynamic_key_paths"]
    rosters_dynamic = per["club_rosters"]["dynamic_key_paths"]

    # Each collection carries only its own dynamic-key paths.
    assert budgets_dynamic == ["budget.by_category"]
    assert rosters_dynamic == ["roster.by_gender"]

    # The two collections must not leak each other's dynamic-key paths.
    assert "roster.by_gender" not in budgets_dynamic
    assert "budget.by_category" not in rosters_dynamic
    assert set(budgets_dynamic).isdisjoint(rosters_dynamic)

    # to_dict round-trips the additive field per-collection too.
    serialized = audit.to_dict()["per_collection_paths"]
    assert serialized["club_budgets"]["dynamic_key_paths"] == ["budget.by_category"]
    assert serialized["club_rosters"]["dynamic_key_paths"] == ["roster.by_gender"]
