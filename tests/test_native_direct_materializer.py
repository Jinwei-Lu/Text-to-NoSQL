from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tend.construction.executor import NativeExecutionResult
from tend.construction.recipe import NativeFeature, NativeFeatureManifest
from tend.source import DbSchema


class _SchemaOnlySource:
    def schema(self, db_id: str) -> DbSchema:
        return DbSchema(
            db_id=db_id,
            domain="fixture",
            tables=[],
            columns=[],
            foreign_keys=[],
            primary_keys={},
            sqlite_path=Path(":memory:"),
        )

    def workload(self, db_id: str) -> list[Any]:
        return []


class _Workflow:
    def __init__(self) -> None:
        self.ctx = types.SimpleNamespace(source=_SchemaOnlySource(), log=None, progress=None)


def _direct_result(db_id: str) -> NativeExecutionResult:
    return NativeExecutionResult(
        data={"semantic_docs": [{"_id": "doc:1", "schema_state": {"source": "present"}}]},
        schema={"collections": {"semantic_docs": {"document_count": 1}}},
        manifest=NativeFeatureManifest(
            db_id=db_id,
            features=[
                NativeFeature(
                    id="semantic_docs.deep",
                    type="direct_semantic_materialization",
                    collection="semantic_docs",
                    field="nested",
                    query_patterns=["semantic direct test"],
                    required_constructs=["direct_materializer"],
                )
            ],
        ),
        provenance={"semantic_docs.deep": {"source_tables": ["fixture"]}},
        world_signature="fixture-signature",
        validation=None,
    )


def test_registry_prefers_direct_materializer_when_design_module_exposes_one(monkeypatch: pytest.MonkeyPatch) -> None:
    from tend.construction.designs import registry

    module = types.ModuleType("tests.fake_direct_native_design")

    def materialize_native_dataworld(source: Any, db_id: str, *, event_hook: Any = None) -> NativeExecutionResult:
        assert isinstance(source, _SchemaOnlySource)
        if event_hook is not None:
            event_hook("direct_called", db_id=db_id)
        return _direct_result(db_id)

    module.materialize_native_dataworld = materialize_native_dataworld  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(registry.NATIVE_DESIGN_MODULES, "fixture_db", module.__name__)

    events: list[tuple[str, dict[str, Any]]] = []
    result = registry.materialize_native_dataworld_for_db(
        _SchemaOnlySource(),
        "fixture_db",
        event_hook=lambda event, **fields: events.append((event, fields)),
    )

    assert result.data["semantic_docs"][0]["_id"] == "doc:1"
    assert result.manifest.features[0].type == "direct_semantic_materialization"
    assert events == [("direct_called", {"db_id": "fixture_db"})]


def test_native_phase_a_accepts_direct_materializer_without_recipe_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    import tend.construction.phase_a as native_construction

    async def parallel(tasks: list[Any], *, isolate: bool) -> list[Any]:
        assert isolate is True
        return [await task() for task in tasks]

    wf = _Workflow()
    wf.phase = lambda phase: None
    wf.parallel = parallel

    def materialize(source: Any, db_id: str, *, event_hook: Any = None) -> NativeExecutionResult:
        return _direct_result(db_id)

    def fail_recipe(_source: Any, _db_id: str) -> Any:
        raise AssertionError("direct materializer should not build or verify a recipe first")

    monkeypatch.setattr(native_construction, "_materialize_native_dataworld_for_db", materialize)
    monkeypatch.setattr(native_construction, "_build_native_recipe_for_db", fail_recipe)

    artifacts = asyncio.run(native_construction.run_native_phase_a(wf, ["fixture_db"]))

    artifact = artifacts["fixture_db"]
    assert artifact.mongodb_data["semantic_docs"][0]["_id"] == "doc:1"
    assert artifact.migration_recipe["direct_conversion"] is True
    assert artifact.rationale["construction_mode"] == "direct_materializer"
    assert artifact.native_feature_manifest.features[0].id == "semantic_docs.deep"
    assert artifact.validation is None
