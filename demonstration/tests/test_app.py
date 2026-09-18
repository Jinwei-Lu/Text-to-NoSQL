from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from demonstration import app as demo


def _release_data_available() -> bool:
    try:
        demo._layout()
    except demo.DemoError:
        return False
    return True


needs_release_data = pytest.mark.skipif(
    not _release_data_available(),
    reason="needs the TEND release data restored from Google Drive (see README)",
)


@pytest.fixture(autouse=True)
def shutdown_solver_service():
    yield
    demo.SOLVER_SERVICE.shutdown()


@needs_release_data
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


@needs_release_data
def test_metadata_schema_and_legacy_read_routes():
    with demo.app.test_client() as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        health_payload = health.get_json()
        assert health_payload["database_count"] == 11
        assert health_payload["policy_defaults"]["fast"]["k_consistency"] == 1
        assert health_payload["policy_limits"]["k_consistency"] == [1, 3]

        databases = client.get("/api/databases")
        assert databases.status_code == 200
        payload = databases.get_json()
        assert payload["dataset_dir"] == "tend-native-mongodb-v1"
        assert len(payload["databases"]) == 11
        summary = next(
            row for row in payload["databases"] if row["db_id"] == "california_schools"
        )
        assert summary["witness_bytes"] > 0
        assert summary["dynamic_key_path_count"] > 0

        examples = client.get("/api/examples/california_schools")
        assert examples.status_code == 200
        assert examples.get_json()["examples"]

        schema = client.get("/api/schema/california_schools")
        assert schema.status_code == 200
        schema_payload = schema.get_json()["schema"]
        assert schema_payload["db_id"] == "california_schools"
        assert schema_payload["sample_source"] in {"mongodb", "witness_file"}
        assert schema_payload["sample_limit"] == demo.MAX_FIELD_SHAPE_DOCS_PER_COLLECTION
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


@needs_release_data
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


def test_site_sag_profile_is_fixed_and_input_is_gold_free(monkeypatch):
    policy = demo._site_sag_policy()
    assert policy.arm == "v3"
    assert policy.effective_k == 3
    assert policy.max_repair_rounds == 2
    assert policy.sample_docs == 80
    assert policy.card_cap == 260
    assert policy.use_gate is True
    assert policy.use_value_witnesses is True
    assert policy.use_bisection is True

    with pytest.raises(demo.DemoError):
        demo._site_workflow_input(
            {
                "database": "california_schools",
                "query": "Count schools.",
                "clientRequestId": "a" * 32,
                "collection": "gold_hint_must_not_cross",
            }
        )

    captured: dict[str, object] = {}

    class _Mongo:
        @staticmethod
        def available() -> bool:
            return True

    async def fake_solve(_workflow, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            to_json=lambda: {
                "result_type": "solver_failure",
                "error_code": "LLM_ERROR",
                "message": "stubbed",
                "disclosure": {"uses_gold_mql": False},
            }
        )

    runtime = SimpleNamespace(
        settings=SimpleNamespace(
            use_existing_mongo_dbs=True,
            llm=SimpleNamespace(model="test-model"),
        ),
        mongo=_Mongo(),
        workflow=object(),
    )
    public_index_cache = object()
    bundle = SimpleNamespace(
        runtime=runtime,
        index_cache=object(),
        site_index_cache=lambda: public_index_cache,
    )
    monkeypatch.setattr(demo, "_db_ids", lambda: ["california_schools"])
    monkeypatch.setattr(demo.SOLVER_SERVICE, "runtime_for_mode", lambda mode: bundle)
    monkeypatch.setattr(demo, "sag_solve_nlq_db", fake_solve)

    response = asyncio.run(
        demo._solve_site_workflow(
            {
                "database": "california_schools",
                "query": "Count schools.",
                "clientRequestId": "b" * 32,
            }
        )
    )
    assert captured["db_id"] == "california_schools"
    assert captured["nlq"] == "Count schools."
    assert captured["record_id"] == "b" * 32
    assert captured["local_data"] is None
    assert captured["index_cache"] is public_index_cache
    assert captured["policy"].solver_variant == "sag_v3_querycraft_live_k3_r2"
    assert response["policy"]["k_consistency"] == 3


@needs_release_data
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


@needs_release_data
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


@needs_release_data
def test_execute_route_validates_request_before_touching_mongo():
    with demo.app.test_client() as client:
        missing_mql = client.post("/api/execute", json={"database": "california_schools"})
        unknown_db = client.post(
            "/api/execute",
            json={"database": "nope", "mql": "db.x.aggregate([])"},
        )
        not_an_object = client.post("/api/execute", json=["nope"])

    assert missing_mql.status_code == 400
    assert missing_mql.get_json()["status"] == "error"
    assert unknown_db.status_code == 404
    assert not_an_object.status_code == 400


@needs_release_data
def test_execute_route_reports_unrunnable_pipelines_without_raising():
    """Banned operators and unparseable text come back as execution errors."""
    with demo.app.test_client() as client:
        banned = client.post(
            "/api/execute",
            json={
                "database": "california_schools",
                "mql": 'db.school_profiles.aggregate([{"$sample": {"size": 3}}])',
                "mode": "stub",
            },
        )
        unparseable = client.post(
            "/api/execute",
            json={"database": "california_schools", "mql": "select * from schools", "mode": "stub"},
        )

    assert banned.status_code == 200
    banned_execution = banned.get_json()["execution"]
    assert banned_execution["status"] == "error"
    assert banned_execution["error_type"] == "DisabledOperatorError"

    assert unparseable.status_code == 200
    assert unparseable.get_json()["execution"]["status"] == "error"


def test_row_limit_stays_bounded():
    assert demo._row_limit(None) == demo.MAX_EXECUTION_ROWS
    assert demo._row_limit(True) == demo.MAX_EXECUTION_ROWS
    assert demo._row_limit(5) == 5
    assert demo._row_limit(10_000) == demo.MAX_EXECUTION_ROW_LIMIT
    assert demo._row_limit(0) == 1
    with pytest.raises(demo.DemoError):
        demo._row_limit("many")
