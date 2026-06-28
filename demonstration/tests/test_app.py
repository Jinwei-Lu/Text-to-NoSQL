from __future__ import annotations

from pathlib import Path

import pytest

from demonstration import app as demo


@pytest.fixture(autouse=True)
def shutdown_solver_service():
    yield
    demo.SOLVER_SERVICE.shutdown()


def test_demo_uses_release_dataset_without_copied_legacy_payloads():
    assert str(demo._layout().test_path).endswith(
        "release/tend-native-mongodb-v1/data/TEND.json"
    )
    assert len(demo._records()) == 1210
    assert len(demo._db_ids()) == 11

    demo_dir = Path(demo.__file__).resolve().parent
    assert not (demo_dir / "mongodb_data").exists()
    assert not (demo_dir / "mongodb_schema").exists()
    assert not (demo_dir / "schemas").exists()


def test_metadata_schema_and_legacy_read_routes():
    with demo.app.test_client() as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.get_json()["database_count"] == 11

        databases = client.get("/api/databases")
        assert databases.status_code == 200
        payload = databases.get_json()
        assert payload["dataset_dir"] == "tend-native-mongodb-v1"
        assert len(payload["databases"]) == 11

        examples = client.get("/api/examples/california_schools")
        assert examples.status_code == 200
        assert examples.get_json()["examples"]

        schema = client.get("/api/schema/california_schools")
        assert schema.status_code == 200
        schema_payload = schema.get_json()["schema"]
        assert schema_payload["db_id"] == "california_schools"
        assert schema_payload["collections"]
        first_collection = schema_payload["collections"][0]
        assert first_collection["top_level_fields"]
        assert first_collection["document_shape"]["kind"] == "object"
        assert any(
            "{" in dynamic_map["value_path"]
            for dynamic_map in first_collection["dynamic_maps"]
        )

        legacy = client.get("/get_schema/california_schools")
        assert legacy.status_code == 200
        assert legacy.get_json()["schema"]["db_id"] == "california_schools"


def test_stub_solve_uses_real_solver_path():
    example = demo._examples_for_db("california_schools")[0]
    with demo.app.test_client() as client:
        response = client.post(
            "/api/solve",
            json={
                "database": "california_schools",
                "record_id": example["record_id"],
                "query": example["NLQ"],
                "mode": "stub",
                "fastMode": True,
                "execute": False,
            },
        )
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["status"] == "success"
    assert payload["mode"] == "stub"
    assert payload["result"]["result_type"] == "solver_prediction"
    assert payload["result"]["MQL"].startswith("db.")
    assert "run_dir" not in payload
    assert payload["run_id"]


def test_solver_option_validation_rejects_unbounded_or_malformed_values():
    example = demo._examples_for_db("california_schools")[0]
    with demo.app.test_client() as client:
        too_large = client.post(
            "/api/solve",
            json={
                "database": "california_schools",
                "record_id": example["record_id"],
                "query": example["NLQ"],
                "mode": "stub",
                "solverOptions": {"k_consistency": 999},
            },
        )
        malformed = client.post(
            "/api/solve",
            json={
                "database": "california_schools",
                "record_id": example["record_id"],
                "query": example["NLQ"],
                "mode": "stub",
                "fastMode": "definitely",
            },
        )
        unknown = client.post(
            "/api/solve",
            json={
                "database": "california_schools",
                "record_id": example["record_id"],
                "query": example["NLQ"],
                "mode": "stub",
                "solverOptions": {"not_a_policy_knob": 1},
            },
        )

    assert too_large.status_code == 400
    assert too_large.get_json()["status"] == "error"
    assert malformed.status_code == 400
    assert malformed.get_json()["status"] == "error"
    assert unknown.status_code == 400
    assert unknown.get_json()["status"] == "error"


def test_live_mode_does_not_inherit_ambient_stub(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEND_LLM_STUB", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "your-placeholder")
    monkeypatch.setenv("OPENAI_BASE_URL", "your-placeholder")
    example = demo._examples_for_db("california_schools")[0]

    with demo.app.test_client() as client:
        response = client.post(
            "/api/solve",
            json={
                "database": "california_schools",
                "record_id": example["record_id"],
                "query": example["NLQ"],
                "mode": "live",
                "fastMode": True,
                "execute": False,
            },
        )

    payload = response.get_json()
    assert response.status_code == 500
    assert payload["status"] == "error"
    assert "OPENAI_API_KEY" in payload["message"]


def test_live_settings_force_stub_off_without_network(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEND_LLM_STUB", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-demo-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")

    settings = demo._settings_for_mode("live")

    assert settings.stub is False


def test_legacy_query_rejects_non_object_json():
    with demo.app.test_client() as client:
        response = client.post("/query", json=["not", "an", "object"])
    assert response.status_code == 400
    assert response.get_json()["status"] == "error"
