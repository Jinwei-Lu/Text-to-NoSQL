from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace


class FakeWorkflow:
    def __init__(self, source) -> None:
        self.events: list[tuple[str, dict]] = []
        self.ctx = SimpleNamespace(
            source=source,
            progress=None,
            log=SimpleNamespace(info=lambda event, **fields: self.events.append((event, fields))),
        )
        self.phases: list[str] = []

    def phase(self, name: str) -> None:
        self.phases.append(name)

    async def parallel(self, thunks, *, isolate: bool = True):
        assert isolate is True
        return [await thunk() for thunk in thunks]


def test_run_native_phase_a_uses_registry_materializer(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import tend.construction.phase_a as native_construction

    calls: list[tuple[str, str]] = []
    validation = SimpleNamespace(ok=True, errors=[], native_feature_count=2)
    recipe = SimpleNamespace(
        db_id="financial",
        design_goal="Build native financial documents.",
        to_dict=lambda: {"db_id": "financial", "recipe_version": 1},
    )
    manifest = SimpleNamespace(
        to_dict=lambda: {
            "db_id": "financial",
            "features": [{"id": "activity_by_month", "type": "dynamic_key_object"}],
        }
    )
    execution = SimpleNamespace(
        schema={"collections": {"account_activity": {"_id": "integer"}}},
        data={"account_activity": [{"_id": 1}]},
        manifest=manifest,
        provenance={"activity_by_month": {"source_columns": ["account.account_id"]}},
        world_signature="world-1",
        validation=validation,
        migration_recipe=recipe,
    )
    schema = SimpleNamespace(
        db_id="financial",
        domain="finance",
        sqlite_path=tmp_path / "financial.sqlite",
        table_count=4,
    )

    class FakeSource:
        def schema(self, db_id: str):
            calls.append(("schema", db_id))
            return schema

        def workload(self, db_id: str):
            calls.append(("workload", db_id))
            return [{"query": "select 1"}]

    def fake_materialize(source, db_id: str, **kwargs):
        calls.append(("materialize", db_id))
        assert isinstance(source, FakeSource)
        assert callable(kwargs["event_hook"])
        kwargs["event_hook"]("recipe_materialized", db_id=db_id, document_count=1)
        return execution

    monkeypatch.setattr(
        native_construction,
        "_materialize_native_dataworld_for_db",
        fake_materialize,
    )

    wf = FakeWorkflow(FakeSource())

    artifacts = asyncio.run(native_construction.run_native_phase_a(wf, ["financial"]))

    assert wf.phases == ["A.native"]
    assert calls == [
        ("schema", "financial"),
        ("materialize", "financial"),
        ("workload", "financial"),
    ]
    artifact = artifacts["financial"]
    assert wf.events == [
        ("recipe_materialized", {"db_id": "financial", "document_count": 1})
    ]
    assert artifact.mongodb_schema == execution.schema
    assert artifact.mongodb_data == execution.data
    assert artifact.migration_recipe is recipe
    assert artifact.native_feature_manifest is manifest
    assert artifact.provenance == {
        "db_id": "financial",
        "entries": execution.provenance,
        "conversion_code_ref": "tend.construction.designs.financial",
    }
    assert artifact.domain_id == "finance"
    assert artifact.sqlite_path == str(tmp_path / "financial.sqlite")
    assert artifact.table_count == 4
    assert artifact.query_count == 1
    assert artifact.conversion_code_ref == "tend.construction.designs.financial"
