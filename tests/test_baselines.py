from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

import tend.baselines.workflow as baseline_workflow
from tend.agents import AgentContext
from tend.baselines import BASELINE_IDS, run_baseline_record, run_baseline_suite
from tend.baselines.boundary import (
    sanitize_public_local_data,
    sanitize_public_record,
    sanitize_public_schema,
)
from tend.baselines.strategies import (
    BaselinePromptContext,
    resolve_baselines,
    _plan_messages,
    _sql_messages,
    _react_mql_messages,
    _draft_messages,
    _repair_messages,
)
from tend.baselines.workflow import (
    BaselinePrediction,
    BaselineStepTrace,
    _baseline_disclosure,
    _extract_mql,
)
from tend.config import Settings
from tend.errors import PromptAnomalyError, SourceError
from tend.llm import LLMClient
from tend.observability import setup_logging
from tend.solver.inputs import _canonical_nlq, load_solver_release_inputs
from tend.stubs import stub_fn
from tend.workflow import Workflow


def _settings() -> Settings:
    return Settings.from_env(
        overrides={"TEND_LLM_STUB": "1"},
        run_id="baseline-test",
        require_bird=False,
    )


def _workflow(settings: Settings, run_dir: Path) -> tuple[Workflow, object]:
    log = setup_logging(run_dir, console=False)
    client = LLMClient(settings, log)
    client.set_stub(stub_fn)
    ctx = AgentContext(settings=settings, llm=client, log=log)
    return Workflow(ctx), log


def _nlq_hash(nlq: str) -> str:
    return "sha256:" + hashlib.sha256(nlq.encode("utf-8")).hexdigest()


class _SnapshotMongo:
    def __init__(self, docs: dict[str, list[dict[str, Any]]]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, int]] = []

    def snapshot_database(self, db_id: str, sample_size: int) -> dict[str, list[dict[str, Any]]]:
        self.calls.append((db_id, sample_size))
        return {name: rows[:sample_size] for name, rows in self.docs.items()}


def _manual_native_docs() -> dict[str, list[dict[str, Any]]]:
    return {
        "race_weekends_v2": [
            {
                "_id": "race:1",
                "calendar": {"race_name": "Australian GP"},
                "sessions": {
                    "race": {
                        "results_by_status": {
                            "Finished": {"count": 2, "entries": []},
                            "Accident": {"count": 1, "entries": []},
                        }
                    }
                },
                "schema_state": {"race_results": "present", "pit_stops": "missing"},
            }
        ]
    }


def test_baseline_registry_has_six_constrained_strategies() -> None:
    assert BASELINE_IDS == (
        "direct",
        "schema_direct",
        "sql_pivot",
        "plan_then_mql",
        "react_lite",
        "static_self_debug",
    )
    specs = resolve_baselines("all")
    assert len(specs) == 6
    assert all(spec.steps for spec in specs)
    assert all(spec.limitations for spec in specs)


def test_baseline_selection_errors_are_typed_source_errors() -> None:
    with pytest.raises(SourceError) as empty:
        resolve_baselines(" , ")
    assert empty.value.message == "empty baseline selection"
    assert empty.value.context["known_baseline_ids"] == list(BASELINE_IDS)

    with pytest.raises(SourceError) as unknown:
        resolve_baselines(["direct", "missing_baseline"])
    assert unknown.value.message == "unknown baseline selection"
    assert unknown.value.context["requested_baseline_ids"] == ["missing_baseline"]
    assert unknown.value.context["known_baseline_ids"] == list(BASELINE_IDS)


def test_baseline_suite_stub_logs_markdown_transcripts(tmp_path: Path) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"

    try:
        outputs = asyncio.run(
            run_baseline_suite(
                wf,
                dataset_dir=dataset_dir,
                baseline_selection="all",
                db_id="financial",
                record_id=1001,
                limit=1,
            )
        )
    finally:
        log.close()

    run_dir = tmp_path / "run"
    assert len(outputs) == 6
    assert {item["baseline_id"] for item in outputs} == set(BASELINE_IDS)
    assert all(item["status"] == "ok" for item in outputs)
    assert all(item["disclosure"]["uses_gold_mql"] is False for item in outputs)
    assert all("disjointness_ok" in item["disclosure"] for item in outputs)
    assert len({item["agent_session_ref"] for item in outputs}) == 6
    for item in outputs:
        assert item["disclosure"]["json_repair_retries"] == 0
        assert item["disclosure"]["retry_contract"]["json_repair_retries"] == 0
        assert item["input_mode"] == "release"
        assert item["nlq_track"] == "record"
        assert item["evaluation_skip_reason"] is None
        assert item["nlq_hash"] == _nlq_hash(
            "For each account attach loan_to_credit_ratio: if it has a loan, loan amount "
            "over sum of credit transactions, else 0; keep every account."
        )
        for step in item["steps"]:
            assert step["json_repair_retries"] == 0
            assert step["llm_attempts"] == 1
            assert step["transport_retries"] == 0
        session_text = _assert_baseline_session_markdown(run_dir, item)
        assert "Record ID: 1001" in session_text
        assert "loan_to_credit_ratio" in session_text

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    llm_ok = [event for event in events if event["event"] == "llm_call_ok"]
    assert len(llm_ok) == 10
    session_refs = {item["agent_session_ref"] for item in outputs}
    for event in llm_ok:
        assert event["baseline_id"]
        assert event["baseline_step"]
        assert event["agent_session_ref"] in session_refs
        transcript_ref = event["transcript_ref"]
        diagnostics_ref = event["diagnostics_ref"]
        assert transcript_ref.endswith(".md")
        assert transcript_ref == event["agent_session_ref"]
        assert diagnostics_ref.endswith(".diagnostics.json")
        assert diagnostics_ref.startswith("baseline/sessions/")
        assert "/diagnostics/" in diagnostics_ref
        diagnostics = json.loads((run_dir / diagnostics_ref).read_text(encoding="utf-8"))
        assert diagnostics["markdown_transcript_enabled"] is False
        assert not (run_dir / diagnostics_ref.replace(".diagnostics.json", ".md")).exists()
        assert diagnostics["agent_session_ref"] == event["agent_session_ref"]
        assert diagnostics["transcript_ref"] == event["agent_session_ref"]
        assert diagnostics["baseline_id"] == event["baseline_id"]
        assert diagnostics["baseline_step"] == event["baseline_step"]
        prompt_text = "\n".join(message["content"] for message in diagnostics["messages"])
        assert "canonical_form_set" not in prompt_text
        assert "shape_policy" not in prompt_text
        assert "agent_design_rationale_ref" not in prompt_text
        assert diagnostics["json_repair_retries"] == 0
        assert len(diagnostics["attempts"]) == 1


def test_baseline_suite_nlq_db_only_derives_context_and_skips_release_loader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    mongo = _SnapshotMongo(_manual_native_docs())
    wf.ctx.mongo = mongo
    captured: list[dict[str, Any]] = []

    monkeypatch.setattr(
        baseline_workflow,
        "load_solver_release_inputs",
        lambda *_args, **_kwargs: pytest.fail("NLQ+DB baseline must not read release inputs"),
    )

    async def fake_run_baseline_record(
        _wf: Workflow,
        spec: Any,
        record: dict[str, Any],
        schema: dict[str, Any],
        *,
        local_data: dict[str, list[dict[str, Any]]] | None = None,
        witness_k: int,
            batch_index: int | None,
            input_mode: str,
            nlq_track: str,
            evaluation_skip_reason: str | None,
        ) -> Any:
        captured.append(
            {
                "baseline_id": spec.id,
                "record": record,
                "schema": schema,
                "local_data": local_data,
                "witness_k": witness_k,
                "batch_index": batch_index,
                    "input_mode": input_mode,
                    "nlq_track": nlq_track,
                    "evaluation_skip_reason": evaluation_skip_reason,
                }
            )

        class _Result:
            def to_json(self) -> dict[str, Any]:
                return {
                    "baseline_id": spec.id,
                    "record_id": record.get("record_id"),
                    "db_id": record.get("db_id"),
                    "status": "ok",
                    "result_type": "baseline_prediction",
                    "MQL": "db.race_weekends_v2.aggregate([])",
                }

        return _Result()

    monkeypatch.setattr(baseline_workflow, "run_baseline_record", fake_run_baseline_record)

    try:
        outputs = asyncio.run(
            run_baseline_suite(
                wf,
                dataset_dir=tmp_path,
                baseline_selection="all",
                db_id="manual_formula",
                record_id=42,
                limit=999,
                witness_k=2,
                nlq="List race weekends that have a Finished result-status bucket.",
            )
        )
    finally:
        log.close()

    assert mongo.calls == [("manual_formula", 2)]
    assert [item["baseline_id"] for item in outputs] == list(BASELINE_IDS)
    assert [item["batch_index"] for item in outputs] == list(range(len(BASELINE_IDS)))
    assert len(captured) == len(BASELINE_IDS)
    for item in captured:
        assert item["record"] == {
            "db_id": "manual_formula",
            "record_id": 42,
            "nl_queries": {
                "canonical": "List race weekends that have a Finished result-status bucket."
            },
        }
        assert "MQL" not in item["record"]
        assert "shape_policy" not in item["record"]
        assert item["schema"]["collections"]["race_weekends_v2"]["schema_flex"] == "native_deep"
        assert "sessions.race.results_by_status" in (
            item["schema"]["collections"]["race_weekends_v2"]["dynamic_key_paths"]
        )
        assert item["local_data"] == _manual_native_docs()
        assert item["witness_k"] == 2
        assert item["input_mode"] == "nlq_db"
        assert item["nlq_track"] == "canonical"
        assert item["evaluation_skip_reason"] == "no_release_dataset"


def test_baseline_record_requires_canonical_nlq(tmp_path: Path) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    record, schema, data = load_solver_release_inputs(
        dataset_dir,
        db_id="financial",
        record_id=1001,
        limit=1,
    )[0]
    record = {
        "record_id": record["record_id"],
        "db_id": record["db_id"],
        "nl_queries": {"colloquial": "Do the same task casually."},
    }
    spec = resolve_baselines("direct")[0]

    try:
        result = asyncio.run(run_baseline_record(wf, spec, record, schema, local_data=data))
    finally:
        log.close()

    payload = result.to_json()
    assert payload["result_type"] == "baseline_failure"
    assert payload["status"] == "failed"
    assert payload["error_code"] == "prompt_malformed"
    anomalies = [
        json.loads(line)
        for line in (tmp_path / "run" / "anomalies.jsonl").read_text().splitlines()
    ]
    assert anomalies[0]["anomaly"] == "prompt_malformed"
    assert anomalies[0]["baseline_id"] == "direct"


def test_baseline_invalid_json_fails_without_repair_retry(tmp_path: Path) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    wf.ctx.llm.set_stub(lambda _agent, _messages, _schema: '{"MQL": ')
    dataset_dir = settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
    record, schema, data = load_solver_release_inputs(
        dataset_dir,
        db_id="financial",
        record_id=1001,
        limit=1,
    )[0]
    spec = resolve_baselines("direct")[0]

    try:
        result = asyncio.run(run_baseline_record(wf, spec, record, schema, local_data=data))
    finally:
        log.close()

    payload = result.to_json()
    assert payload["result_type"] == "baseline_failure"
    assert payload["status"] == "failed"
    assert payload["error_code"] == "parse_error"
    assert payload["disclosure"]["json_repair_retries"] == 0
    assert payload["disclosure"]["retry_contract"]["json_repair_retries"] == 0
    assert payload["steps"] == []
    assert payload["agent_session_ref"].endswith(".md")
    assert len(payload["transcript_refs"]) == 1
    assert len(payload["diagnostics_refs"]) == 1

    run_dir = tmp_path / "run"
    assert payload["diagnostics_refs"][0].startswith("baseline/sessions/")
    assert "/diagnostics/" in payload["diagnostics_refs"][0]
    diagnostics_path = run_dir / payload["diagnostics_refs"][0]
    assert diagnostics_path.exists()
    diagnostics = json.loads(diagnostics_path.read_text(encoding="utf-8"))
    assert diagnostics["failed"] is True
    assert diagnostics["json_repair_retries"] == 0
    assert len(diagnostics["attempts"]) == 1
    assert diagnostics["attempts"][0]["validation_error"]["anomaly"] == "parse_error"
    assert len(diagnostics["messages"]) == 2
    session_text = (run_dir / payload["agent_session_ref"]).read_text(encoding="utf-8")
    assert "## Errors" in session_text
    assert "parse_error" in session_text
    assert payload["diagnostics_refs"][0] in session_text

    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert not [event for event in events if event["event"] == "llm_repair_retry"]
    anomalies = [event for event in events if event["event"] == "anomaly"]
    assert any(
        event["anomaly"] == "parse_error"
        and event.get("agent_session_ref") == payload["agent_session_ref"]
        and event.get("diagnostics_ref") == payload["diagnostics_refs"][0]
        for event in anomalies
    )


# ---------------------------------------------------------------------------
# Regression tests for Phase-1 production fixes
# ---------------------------------------------------------------------------

def _make_prompt_ctx(
    nlq: str = "Show all accounts.",
    witness_digest: dict | None = None,
) -> BaselinePromptContext:
    """Minimal BaselinePromptContext for message-builder unit tests."""
    schema = {
        "collections": {
            "account": {
                "fields": {"_id": "objectid", "balance": "double"},
                "embeds": [],
                "foreign_keys": [],
            }
        }
    }
    schema_summary = {
        "collections": {
            "account": {
                "fields": ["_id", "balance"],
                "embeds": [],
                "foreign_keys": [],
            }
        }
    }
    record = {"db_id": "financial", "record_id": 1001}
    return BaselinePromptContext(
        record=record,
        schema=schema,
        witness_digest=witness_digest or {},
        schema_summary=schema_summary,
        nlq=nlq,
    )


def _all_message_text(messages: list[dict[str, Any]]) -> str:
    return "\n".join(str(message.get("content", "")) for message in messages)


def _assert_no_private_sentinel(payload: Any, *sentinels: str) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    for sentinel in sentinels:
        assert sentinel not in text


def _assert_baseline_session_markdown(run_dir: Path, payload: dict[str, Any]) -> str:
    assert payload["agent_session_ref"].endswith(".md")
    assert payload["agent_session_ref"].startswith("baseline/sessions/")
    assert payload["transcript_refs"] == [payload["agent_session_ref"]]
    assert all(
        step["transcript_ref"] == payload["agent_session_ref"]
        for step in payload.get("steps", [])
    )
    assert payload["diagnostics_refs"] == [
        step["diagnostics_ref"] for step in payload.get("steps", [])
    ]
    session_path = run_dir / payload["agent_session_ref"]
    assert session_path.exists()
    session_text = session_path.read_text(encoding="utf-8")
    assert session_text.startswith("# Baseline Session")
    assert "Stage: BASELINE" in session_text
    assert f"Baseline ID: {payload['baseline_id']}" in session_text
    assert f"DB ID: {payload['db_id']}" in session_text
    assert "## Public Input Summary" in session_text
    assert "## Steps" in session_text
    assert "### Messages" in session_text
    assert "### Model Output" in session_text
    assert "### Parsed Output" in session_text
    assert "### Diagnostics" in session_text
    assert "## Static Feedback" in session_text
    assert "## Final Outcome" in session_text
    assert ".diagnostics.json" in session_text
    return session_text


def test_public_schema_sanitizer_strips_release_financial_audit_fields() -> None:
    settings = _settings()
    schema_path = (
        settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
        / "mongodb_schema" / "financial.json"
    )
    raw_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    raw_schema["structure_audit"] = {"private": "SECRET_SCHEMA_AUDIT_SENTINEL"}
    raw_schema["structure_gate"] = {"private": "SECRET_SCHEMA_GATE_SENTINEL"}
    raw_schema["account"]["source_tables"] = ["account", "loan"]
    raw_schema["account"]["_provenance"] = {"source": "SECRET_PROVENANCE_SENTINEL"}
    raw_schema["account"]["native_verification"] = {"ok": True}
    raw_schema["account"]["native_metadata"] = {"compiler": "SECRET_COMPILER_SENTINEL"}
    raw_schema["account"]["MQL"] = "SECRET_GOLD_MQL_SENTINEL"
    raw_schema["account"]["mql_skeleton_signature"] = "SECRET_MQL_SKELETON_SENTINEL"
    raw_schema["account"]["anti_sql_transfer_target"] = "SECRET_ANTI_SQL_SENTINEL"
    raw_schema["account"]["surgical_hint"] = "SECRET_SURGICAL_SENTINEL"
    raw_schema["account"]["sample_keys"] = ["SECRET_SAMPLE_KEY_SENTINEL"]
    raw_schema["account"]["dynamic_key_samples"] = {"loan": ["SECRET_DYNAMIC_SAMPLE"]}
    raw_schema["account"]["array_lengths"] = {"loan": [1]}
    raw_schema["account"]["presence_state_counts"] = {"loan": {"present": 1}}
    raw_schema["account"]["collection_counts"] = {"account": 4500}

    sanitized = sanitize_public_schema(raw_schema)

    assert sanitized.value["public_schema_version"] == "baseline_public_schema_v1"
    assert "account" in sanitized.value["collections"]
    account = sanitized.value["collections"]["account"]
    assert account["fields"]["account_id"] == "INT"
    assert "variants" in account
    assert "__variants" not in account
    assert "coverage" not in account["variants"][0]
    assert "source_signal" not in account["variants"][0]
    assert set(account["variants"][0]) == {"discriminator", "fields"}
    assert any(path.endswith("structure_audit") for path in sanitized.stripped_fields)
    assert any(path.endswith("account.source_tables") for path in sanitized.stripped_fields)
    assert any(path.endswith("account.__variants[0].coverage") for path in sanitized.stripped_fields)
    _assert_no_private_sentinel(
        sanitized.value,
        "SECRET_SCHEMA_AUDIT_SENTINEL",
        "SECRET_SCHEMA_GATE_SENTINEL",
        "SECRET_PROVENANCE_SENTINEL",
        "SECRET_COMPILER_SENTINEL",
        "SECRET_GOLD_MQL_SENTINEL",
        "SECRET_MQL_SKELETON_SENTINEL",
        "SECRET_ANTI_SQL_SENTINEL",
        "SECRET_SURGICAL_SENTINEL",
        "SECRET_SAMPLE_KEY_SENTINEL",
        "SECRET_DYNAMIC_SAMPLE",
    )


def test_public_schema_sanitizer_preserves_toxicology_native_shape() -> None:
    raw_schema = {
        "db_id": "toxicology",
        "collections": {
            "toxicity_profiles": {
                "document_count": 12,
                "doc_count": 12,
                "root_entity": "compound",
                "native_shape": "compound -> assays[] -> endpoints[]",
                "schema_flex": "native_deep",
                "fields": {
                    "_id": "objectid",
                    "compound_id": "string",
                    "assays.endpoints.score": "double",
                },
                "embeds": ["assays"],
                "foreign_keys": [],
                "dynamic_key_paths": ["assays.by_species"],
                "array_paths": ["assays", "assays.endpoints"],
                "dynamic_array_object_paths": ["assays.by_species.*.endpoints"],
                "array_object_dynamic_paths": ["assays.*.endpoints.by_lab"],
                "variants": [
                    {
                        "discriminator": {"assay_type": "ames"},
                        "fields": {"assay_type": "string", "endpoints": "array"},
                        "coverage": 0.25,
                    }
                ],
                "native_metadata": {"compiler": "SECRET_TOX_COMPILER"},
                "dynamic_key_samples": {"assays.by_species": ["rat"]},
                "presence_state_counts": {"assays": {"present": 12}},
            }
        },
    }

    sanitized = sanitize_public_schema(raw_schema)
    collection = sanitized.value["collections"]["toxicity_profiles"]

    assert collection["document_count"] == 12
    assert collection["doc_count"] == 12
    assert collection["root_entity"] == "compound"
    assert collection["native_shape"] == "compound -> assays[] -> endpoints[]"
    assert collection["schema_flex"] == "native_deep"
    assert collection["fields"]["assays.endpoints.score"] == "double"
    assert collection["embeds"] == ["assays"]
    assert collection["foreign_keys"] == []
    assert collection["dynamic_key_paths"] == ["assays.by_species"]
    assert collection["array_paths"] == ["assays", "assays.endpoints"]
    assert collection["dynamic_array_object_paths"] == ["assays.by_species.*.endpoints"]
    assert collection["array_object_dynamic_paths"] == ["assays.*.endpoints.by_lab"]
    assert collection["variants"] == [
        {
            "discriminator": {"assay_type": "ames"},
            "fields": {"assay_type": "string", "endpoints": "array"},
        }
    ]
    _assert_no_private_sentinel(sanitized.value, "SECRET_TOX_COMPILER", "rat")


def test_public_schema_sanitizer_normalizes_smoke_fixture_variants() -> None:
    settings = _settings()
    schema_path = (
        settings.paths.repo_root / "tests" / "fixtures" / "smoke_release"
        / "mongodb_schema" / "financial.json"
    )
    raw_schema = json.loads(schema_path.read_text(encoding="utf-8"))

    sanitized = sanitize_public_schema(raw_schema)
    account = sanitized.value["collections"]["account"]

    assert "variants" in account
    assert "__variants" not in account
    assert account["variants"] == [
        {
            "discriminator": {"loan": "present"},
            "fields": {"loan": "OBJECT"},
        },
        {
            "discriminator": {"loan": "missing"},
            "fields": {"loan": "OBJECT"},
        },
    ]
    assert "coverage" not in json.dumps(account, sort_keys=True)
    assert "source_signal" not in json.dumps(account, sort_keys=True)


def test_public_record_sanitizer_allows_only_public_record_boundary() -> None:
    raw_record = {
        "db_id": "financial",
        "record_id": 1001,
        "nl_queries": {
            "canonical": "Show all accounts.",
            "colloquial": "SECRET_COLLOQUIAL_SENTINEL",
        },
        "MQL": "SECRET_GOLD_MQL_SENTINEL",
        "canonical_form_set": {"must_contain": ["SECRET_CANONICAL_FORM_SENTINEL"]},
        "shape_policy": "SECRET_SHAPE_POLICY_SENTINEL",
        "native_metadata": {"compiler": "SECRET_NATIVE_METADATA_SENTINEL"},
        "native_verification": {"ok": True, "trace": "SECRET_NATIVE_VERIFICATION"},
        "provenance_refs": ["SECRET_PROVENANCE_REF_SENTINEL"],
        "migration_recipe_ref": "SECRET_MIGRATION_RECIPE_SENTINEL",
        "world_signature": "SECRET_WORLD_SIGNATURE_SENTINEL",
    }

    sanitized = sanitize_public_record(raw_record)

    assert sanitized.value == {
        "db_id": "financial",
        "record_id": 1001,
        "nl_queries": {"canonical": "Show all accounts."},
    }
    assert "MQL" in sanitized.stripped_fields
    assert "nl_queries.colloquial" in sanitized.stripped_fields
    _assert_no_private_sentinel(
        sanitized.value,
        "SECRET_COLLOQUIAL_SENTINEL",
        "SECRET_GOLD_MQL_SENTINEL",
        "SECRET_CANONICAL_FORM_SENTINEL",
        "SECRET_SHAPE_POLICY_SENTINEL",
        "SECRET_NATIVE_METADATA_SENTINEL",
        "SECRET_NATIVE_VERIFICATION",
        "SECRET_PROVENANCE_REF_SENTINEL",
        "SECRET_MIGRATION_RECIPE_SENTINEL",
        "SECRET_WORLD_SIGNATURE_SENTINEL",
    )


def test_public_local_data_sanitizer_recursively_strips_forbidden_fields() -> None:
    local_data = {
        "account": [
            {
                "account_id": 1,
                "status": "active",
                "MQL": "SECRET_LOCAL_MQL_SENTINEL",
                "shape_policy": "SECRET_LOCAL_SHAPE_POLICY",
                "nested": {
                    "amount": 10,
                    "canonical_form_set": ["SECRET_LOCAL_CANONICAL_FORM"],
                    "provenance_refs": ["SECRET_LOCAL_PROVENANCE_REF"],
                },
                "events": [
                    {
                        "kind": "deposit",
                        "native_metadata": {"compiler": "SECRET_LOCAL_COMPILER"},
                    }
                ],
            }
        ]
    }

    sanitized = sanitize_public_local_data(local_data)

    assert sanitized.value == {
        "account": [
            {
                "account_id": 1,
                "status": "active",
                "nested": {"amount": 10},
                "events": [{"kind": "deposit"}],
            }
        ]
    }
    assert sanitized.stripped_fields == [
        "account[0].MQL",
        "account[0].shape_policy",
        "account[0].nested.canonical_form_set",
        "account[0].nested.provenance_refs",
        "account[0].events[0].native_metadata",
    ]
    _assert_no_private_sentinel(
        sanitized.value,
        "SECRET_LOCAL_MQL_SENTINEL",
        "SECRET_LOCAL_SHAPE_POLICY",
        "SECRET_LOCAL_CANONICAL_FORM",
        "SECRET_LOCAL_PROVENANCE_REF",
        "SECRET_LOCAL_COMPILER",
    )


def test_prompt_builders_do_not_render_private_schema_or_record_sentinels(
    tmp_path: Path,
) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    spec = resolve_baselines("schema_direct")[0]
    raw_record = {
        "db_id": "financial",
        "record_id": 1001,
        "nl_queries": {
            "canonical": "Show all accounts.",
            "colloquial": "SECRET_PROMPT_RECORD_COLLOQUIAL",
        },
        "MQL": "SECRET_PROMPT_RECORD_MQL",
        "canonical_form_set": {"must_contain": ["SECRET_PROMPT_CANONICAL_FORM"]},
        "shape_policy": "SECRET_PROMPT_SHAPE_POLICY",
        "native_metadata": {"compiler": "SECRET_PROMPT_RECORD_NATIVE_METADATA"},
        "native_verification": {"ok": True, "trace": "SECRET_PROMPT_RECORD_VERIFY"},
        "provenance_refs": ["SECRET_PROMPT_PROVENANCE_REF"],
        "migration_recipe_ref": "SECRET_PROMPT_MIGRATION_RECIPE",
        "world_signature": "SECRET_PROMPT_WORLD_SIGNATURE",
    }
    raw_schema = {
        "collections": {
            "account": {
                "fields": {"_id": "objectid", "balance": "double"},
                "embeds": [],
                "foreign_keys": [],
                "native_shape": "public account document",
                "source_tables": ["SECRET_PROMPT_SOURCE_TABLE"],
                "native_metadata": {"compiler": "SECRET_PROMPT_SCHEMA_COMPILER"},
                "dynamic_key_samples": {"balance": ["SECRET_PROMPT_DYNAMIC_SAMPLE"]},
                "variants": [
                    {
                        "discriminator": {"loan": "present"},
                        "fields": {"loan": "object"},
                        "coverage": 1.0,
                        "source_signal": "SECRET_PROMPT_SOURCE_SIGNAL",
                    }
                ],
            }
        },
        "structure_audit": {"private": "SECRET_PROMPT_STRUCTURE_AUDIT"},
    }

    try:
        result = asyncio.run(
            run_baseline_record(wf, spec, raw_record, raw_schema, local_data=None)
        )
    finally:
        log.close()

    payload = result.to_json()
    assert payload["status"] == "ok"
    run_dir = tmp_path / "run"
    diagnostics_paths = [run_dir / ref for ref in payload["diagnostics_refs"]]
    assert diagnostics_paths
    rendered = "\n".join(
        _all_message_text(json.loads(path.read_text(encoding="utf-8"))["messages"])
        for path in diagnostics_paths
    )
    for sentinel in (
        "SECRET_PROMPT_RECORD_COLLOQUIAL",
        "SECRET_PROMPT_RECORD_MQL",
        "SECRET_PROMPT_CANONICAL_FORM",
        "SECRET_PROMPT_SHAPE_POLICY",
        "SECRET_PROMPT_RECORD_NATIVE_METADATA",
        "SECRET_PROMPT_RECORD_VERIFY",
        "SECRET_PROMPT_PROVENANCE_REF",
        "SECRET_PROMPT_MIGRATION_RECIPE",
        "SECRET_PROMPT_WORLD_SIGNATURE",
        "SECRET_PROMPT_SOURCE_TABLE",
        "SECRET_PROMPT_SCHEMA_COMPILER",
        "SECRET_PROMPT_DYNAMIC_SAMPLE",
        "SECRET_PROMPT_SOURCE_SIGNAL",
        "SECRET_PROMPT_STRUCTURE_AUDIT",
    ):
        assert sentinel not in rendered
    assert "public account document" in rendered


def test_prompt_witness_digest_does_not_render_local_data_forbidden_sentinels(
    tmp_path: Path,
) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    spec = resolve_baselines("react_lite")[0]
    raw_record = {
        "db_id": "financial",
        "record_id": 1001,
        "nl_queries": {"canonical": "Show active accounts."},
    }
    raw_schema = {
        "collections": {
            "account": {
                "fields": {"account_id": "int", "status": "string"},
                "embeds": [],
                "foreign_keys": [],
            }
        }
    }
    local_data = {
        "account": [
            {
                "account_id": 1,
                "status": "PUBLIC_LOCAL_STATUS_SENTINEL",
                "MQL": "SECRET_PROMPT_LOCAL_MQL",
                "shape_policy": "SECRET_PROMPT_LOCAL_SHAPE_POLICY",
                "nested": {
                    "canonical_form_set": ["SECRET_PROMPT_LOCAL_CANONICAL_FORM"],
                    "amount": 10,
                },
                "events": [
                    {
                        "kind": "public",
                        "native_metadata": {"compiler": "SECRET_PROMPT_LOCAL_COMPILER"},
                    }
                ],
            }
        ]
    }

    try:
        result = asyncio.run(
            run_baseline_record(
                wf,
                spec,
                raw_record,
                raw_schema,
                local_data=local_data,
                witness_k=1,
            )
        )
    finally:
        log.close()

    payload = result.to_json()
    assert payload["status"] == "ok"
    assert payload["disclosure"]["local_data_sanitizer_applied"] is True
    assert payload["disclosure"]["local_data_stripped_fields"] == [
        "account[0].MQL",
        "account[0].shape_policy",
        "account[0].nested.canonical_form_set",
        "account[0].events[0].native_metadata",
    ]

    run_dir = tmp_path / "run"
    diagnostics_paths = [run_dir / ref for ref in payload["diagnostics_refs"]]
    assert len(diagnostics_paths) == 2
    rendered = "\n".join(
        _all_message_text(json.loads(path.read_text(encoding="utf-8"))["messages"])
        for path in diagnostics_paths
    )
    assert "PUBLIC_LOCAL_STATUS_SENTINEL" in rendered
    for sentinel in (
        "SECRET_PROMPT_LOCAL_MQL",
        "SECRET_PROMPT_LOCAL_SHAPE_POLICY",
        "SECRET_PROMPT_LOCAL_CANONICAL_FORM",
        "SECRET_PROMPT_LOCAL_COMPILER",
    ):
        assert sentinel not in rendered


def test_baseline_disclosure_declares_sanitizer_and_retry_contract(
    tmp_path: Path,
) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    spec = resolve_baselines("direct")[0]
    try:
        disclosure = _baseline_disclosure(
            wf,
            spec,
            witness_k=5,
            schema_stripped_fields=["structure_audit", "account.source_tables"],
            record_stripped_fields=["MQL", "canonical_form_set"],
            local_data_stripped_fields=["account[0].MQL"],
            schema_public_shape={
                "format": "collections",
                "collection_total": 1,
                "collections": ["account"],
            },
        )
    finally:
        log.close()

    assert disclosure["public_schema_version"] == "baseline_public_schema_v1"
    assert disclosure["schema_sanitizer_applied"] is True
    assert disclosure["record_sanitizer_applied"] is True
    assert disclosure["local_data_sanitizer_applied"] is True
    assert disclosure["schema_stripped_fields"] == ["structure_audit", "account.source_tables"]
    assert disclosure["record_stripped_fields"] == ["MQL", "canonical_form_set"]
    assert disclosure["local_data_stripped_fields"] == ["account[0].MQL"]
    assert disclosure["schema_public_shape"] == {
        "format": "collections",
        "collection_total": 1,
        "collections": ["account"],
    }
    assert disclosure["uses_public_witness_digest"] is True
    assert disclosure["semantic_retry_budget"] == 0
    assert disclosure["json_repair_retries"] == 0
    assert disclosure["r_max"] == 0
    assert disclosure["uses_execution_feedback"] is False
    assert disclosure["uses_gold_mql"] is False
    assert disclosure["retry_contract"] == {
        "semantic_retry_budget": 0,
        "json_repair_retries": 0,
        "format_transport_retries_are_semantic_retries": False,
        "format_transport_retry_scope": "LLM client JSON/transport only",
    }


def test_baseline_prediction_payload_includes_nlq_db_provenance_fields(
    tmp_path: Path,
) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    spec = resolve_baselines("direct")[0]
    nlq = "List all active accounts."
    record = {
        "db_id": "financial",
        "record_id": 42,
        "nl_queries": {"canonical": nlq},
    }
    schema = {
        "collections": {
            "account": {
                "fields": {"account_id": "int", "status": "string"},
                "embeds": [],
                "foreign_keys": [],
            }
        }
    }

    try:
        result = asyncio.run(
            run_baseline_record(
                wf,
                spec,
                record,
                schema,
                local_data={"account": [{"account_id": 1, "status": "active"}]},
                witness_k=1,
                input_mode="nlq_db",
                nlq_track="canonical",
            )
        )
    finally:
        log.close()

    payload = result.to_json()
    assert payload["result_type"] == "baseline_prediction"
    assert payload["input_mode"] == "nlq_db"
    assert payload["nlq_track"] == "canonical"
    assert payload["nlq_hash"] == _nlq_hash(nlq)
    assert payload["evaluation_skip_reason"] is None
    assert "nlq" not in payload
    assert payload["witness_k"] == 1


def test_failure_row_preserves_disclosure_and_public_safe_step_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    spec = resolve_baselines("schema_direct")[0]
    raw_record = {
        "db_id": "financial",
        "record_id": 1001,
        "nl_queries": {"canonical": "Show all accounts."},
        "MQL": "SECRET_FAILURE_RECORD_MQL",
        "canonical_form_set": {"must_contain": ["SECRET_FAILURE_CANONICAL_FORM"]},
        "shape_policy": "SECRET_FAILURE_SHAPE_POLICY",
        "native_metadata": {"compiler": "SECRET_FAILURE_RECORD_NATIVE_METADATA"},
        "native_verification": {"ok": True, "trace": "SECRET_FAILURE_RECORD_VERIFY"},
        "provenance_refs": ["SECRET_FAILURE_PROVENANCE_REF"],
        "migration_recipe_ref": "SECRET_FAILURE_MIGRATION_RECIPE",
        "world_signature": "SECRET_FAILURE_WORLD_SIGNATURE",
    }
    raw_schema = {
        "collections": {
            "account": {
                "fields": {"_id": "objectid", "balance": "double"},
                "embeds": [],
                "foreign_keys": [],
                "native_shape": "public account document",
                "source_tables": ["SECRET_FAILURE_SOURCE_TABLE"],
                "native_metadata": {"compiler": "SECRET_FAILURE_SCHEMA_COMPILER"},
                "dynamic_key_samples": {"balance": ["SECRET_FAILURE_DYNAMIC_SAMPLE"]},
            }
        },
        "structure_audit": {"private": "SECRET_FAILURE_STRUCTURE_AUDIT"},
    }

    def forced_static_error(_mql: str) -> list[dict[str, Any]]:
        return [{"severity": "error", "code": "FORCED_STATIC", "message": "forced"}]

    monkeypatch.setattr(baseline_workflow, "static_mql_feedback", forced_static_error)

    try:
        result = asyncio.run(
            run_baseline_record(wf, spec, raw_record, raw_schema, local_data=None)
        )
    finally:
        log.close()

    payload = result.to_json()
    assert payload["result_type"] == "baseline_failure"
    assert payload["status"] == "failed"
    assert payload["input_mode"] == "release"
    assert payload["nlq_track"] == "record"
    assert payload["nlq_hash"] == _nlq_hash("Show all accounts.")
    assert payload["evaluation_skip_reason"] is None
    assert payload["disclosure"]["schema_sanitizer_applied"] is True
    assert payload["disclosure"]["record_sanitizer_applied"] is True
    assert payload["disclosure"]["local_data_sanitizer_applied"] is True
    assert payload["disclosure"]["uses_gold_mql"] is False
    assert payload["disclosure"]["uses_execution_feedback"] is False
    assert payload["disclosure"]["semantic_retry_budget"] == 0
    assert payload["disclosure"]["json_repair_retries"] == 0
    assert payload["steps"]
    assert payload["steps"][0]["transcript_ref"]
    assert payload["steps"][0]["diagnostics_ref"]
    run_dir = tmp_path / "run"
    session_text = _assert_baseline_session_markdown(run_dir, payload)
    assert "## Errors" in session_text
    assert "FORCED_STATIC" in session_text
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    static_anomaly = next(
        event for event in events
        if event["event"] == "anomaly"
        and event.get("message") == "baseline produced statically invalid MQL"
    )
    assert static_anomaly["agent_session_ref"] == payload["agent_session_ref"]
    assert static_anomaly["transcript_ref"] == payload["transcript_refs"][-1]
    assert static_anomaly["diagnostics_ref"] == payload["diagnostics_refs"][-1]
    _assert_no_private_sentinel(
        payload,
        "SECRET_FAILURE_RECORD_MQL",
        "SECRET_FAILURE_CANONICAL_FORM",
        "SECRET_FAILURE_SHAPE_POLICY",
        "SECRET_FAILURE_RECORD_NATIVE_METADATA",
        "SECRET_FAILURE_RECORD_VERIFY",
        "SECRET_FAILURE_PROVENANCE_REF",
        "SECRET_FAILURE_MIGRATION_RECIPE",
        "SECRET_FAILURE_WORLD_SIGNATURE",
        "SECRET_FAILURE_SOURCE_TABLE",
        "SECRET_FAILURE_SCHEMA_COMPILER",
        "SECRET_FAILURE_DYNAMIC_SAMPLE",
        "SECRET_FAILURE_STRUCTURE_AUDIT",
    )


# [H8] sql_pivot step-1 messages contain 'SQL' and 'notes'.
# plan_then_mql step-1 messages contain 'target_collection', 'steps', 'risks'.
def test_sql_pivot_step1_messages_contain_sql_and_notes_fields() -> None:
    ctx = _make_prompt_ctx()
    messages = _sql_messages(ctx, {})
    all_text = "\n".join(str(m.get("content", "")) for m in messages)
    assert "SQL" in all_text, "step-1 sql_pivot prompt must mention the 'SQL' field"
    assert "notes" in all_text, "step-1 sql_pivot prompt must mention the 'notes' field"


def test_plan_then_mql_step1_messages_contain_plan_schema_fields() -> None:
    ctx = _make_prompt_ctx()
    messages = _plan_messages(ctx, {})
    all_text = "\n".join(str(m.get("content", "")) for m in messages)
    assert "target_collection" in all_text
    assert "steps" in all_text
    assert "risks" in all_text


# [H9] A record whose nl_queries has blank canonical but populated colloquial:
# _canonical_nlq(use_colloquial=False) raises PromptAnomalyError (canonical-only baseline
# behavior).
def test_canonical_nlq_blank_canonical_populated_colloquial_raises() -> None:
    record = {
        "db_id": "financial",
        "record_id": 1001,
        "nl_queries": {
            "canonical": "",  # blank
            "colloquial": "Show me the accounts.",
        },
    }
    with pytest.raises(PromptAnomalyError):
        _canonical_nlq(record, use_colloquial=False)


def test_canonical_nlq_blank_canonical_colloquial_allowed_when_use_colloquial_true() -> None:
    record = {
        "db_id": "financial",
        "record_id": 1001,
        "nl_queries": {
            "canonical": "",
            "colloquial": "Show me the accounts.",
        },
    }
    result = _canonical_nlq(record, use_colloquial=True)
    assert result == "Show me the accounts."


# [CF2] _baseline_disclosure and BaselinePrediction.to_json include 'witness_k' and 'r_max'.
def test_baseline_disclosure_contains_witness_k_and_r_max(tmp_path: Path) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    spec = resolve_baselines("direct")[0]
    try:
        disclosure = _baseline_disclosure(wf, spec, witness_k=5)
    finally:
        log.close()
    assert "witness_k" in disclosure, "_baseline_disclosure must include 'witness_k'"
    assert "r_max" in disclosure, "_baseline_disclosure must include 'r_max'"
    assert disclosure["witness_k"] == 5
    assert disclosure["r_max"] == 0


def test_baseline_prediction_to_json_contains_witness_k_and_r_max(tmp_path: Path) -> None:
    settings = _settings()
    wf, log = _workflow(settings, tmp_path / "run")
    spec = resolve_baselines("direct")[0]
    try:
        disclosure = _baseline_disclosure(wf, spec, witness_k=3)
    finally:
        log.close()
    dummy_trace = BaselineStepTrace(
        step_id="mql",
        agent="baseline_direct_mql",
        title="direct MQL",
        transcript_ref="run/t.md",
        diagnostics_ref="run/t.diagnostics.json",
        output={"MQL": "db.account.aggregate([])", "rationale": "stub"},
    )
    prediction = BaselinePrediction(
        baseline_id="direct",
        baseline_title="Direct NL-to-MQL",
        record_id=1001,
        db_id="financial",
        MQL='db.account.aggregate([{"$match": {}}])',
        disclosure=disclosure,
        witness_k=3,
        r_max=0,
        steps=[dummy_trace],
    )
    payload = prediction.to_json()
    assert "witness_k" in payload, "BaselinePrediction.to_json must include 'witness_k'"
    assert "r_max" in payload, "BaselinePrediction.to_json must include 'r_max'"
    assert payload["witness_k"] == 3
    assert payload["r_max"] == 0


# [CF7] static_self_debug: when the draft step returns lowercase {'mql':...}, the repair step
# static_feedback is NOT a false EMPTY_MQL (because _extract_mql handles the lowercase key).
def test_extract_mql_handles_lowercase_key() -> None:
    state_lowercase = {"mql": 'db.account.aggregate([{"$match": {}}])'}
    result = _extract_mql(state_lowercase)
    assert result == 'db.account.aggregate([{"$match": {}}])'


def test_extract_mql_prefers_uppercase_over_lowercase() -> None:
    state_both = {
        "MQL": 'db.account.aggregate([{"$match": {}}])',
        "mql": "db.other.aggregate([])",
    }
    result = _extract_mql(state_both)
    assert result == 'db.account.aggregate([{"$match": {}}])'


def test_static_self_debug_repair_no_false_empty_mql_with_lowercase_draft(
    tmp_path: Path,
) -> None:
    """When draft step returns lowercase {'mql':...}, _extract_mql picks it up and the repair
    step does NOT see EMPTY_MQL static_feedback (regression for CF7)."""
    from tend.execution.ast_check import static_mql_feedback

    lowercase_mql = 'db.account.aggregate([{"$match": {"balance": {"$gt": 0}}}])'
    state = {"mql": lowercase_mql}  # lowercase, as a buggy draft might produce
    extracted = _extract_mql(state)
    assert extracted == lowercase_mql, "_extract_mql must resolve lowercase 'mql' key"
    feedback = static_mql_feedback(extracted)
    codes = [item["code"] for item in feedback]
    assert "EMPTY_MQL" not in codes, (
        "static_feedback must not contain EMPTY_MQL when draft returns lowercase mql"
    )


# [F4] react_lite step-2 prompt does not contain the witness digest twice.
def test_react_lite_step2_witness_digest_appears_exactly_once() -> None:
    sentinel_value = "UNIQUE_SENTINEL_WITNESS_TOKEN_XYZ"
    witness_digest = {"sentinel_key": sentinel_value}
    ctx = _make_prompt_ctx(witness_digest=witness_digest)
    # Simulate a non-empty step-1 state (thoughts from the think step)
    state = {
        "thoughts": ["Consider the schema structure."],
        "needed_observations": ["sample documents"],
    }
    messages = _react_mql_messages(ctx, state)
    all_text = "\n".join(str(m.get("content", "")) for m in messages)
    count = all_text.count(sentinel_value)
    assert count == 1, (
        f"Witness digest payload must appear exactly once in react_lite step-2 messages, "
        f"but found {count} occurrence(s)"
    )
